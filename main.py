"""绿通快检系统 - Flask 后端 API 入口

前后端分离架构：
- 前端: Vue3 + Vite (端口 5173)
- 后端: Flask REST API (端口 8080)
- WebSocket: /ws
"""
from flask import Flask, send_file, request, jsonify
from flask_cors import CORS

from config import HOST, PORT, DEBUG, CORS_ORIGINS

# API 蓝图
from app.api.auth import app_api
from app.api.users import user_api
from app.api.booking import booking_api
from app.api.dictionary import dict_api
from app.api.inspection import inspection_api
from app.api.device import device_api
from app.api.imaging import imaging_api
from app.api.history import history_api
from app.api.capture import capture_api

# WebSocket
from ws.handler import init_socketio


def create_app() -> Flask:
    """创建并配置 Flask 应用"""
    app = Flask(__name__)

    CORS(
        app,
        origins=CORS_ORIGINS.split(','),
        supports_credentials=True,
        allow_headers=['Content-Type', 'Authorization'],
    )

    app.config['SECRET_KEY'] = 'gcms-jwt-secret-key-2025'

    from config import RADAR_HEAD_URL, XRAY_BASE_URL
    app.config['RADAR_HEAD_URL'] = RADAR_HEAD_URL
    app.config['XRAY_BASE_URL'] = XRAY_BASE_URL

    # 注册蓝图
    app.register_blueprint(app_api)
    app.register_blueprint(user_api)
    app.register_blueprint(booking_api)
    app.register_blueprint(dict_api)
    app.register_blueprint(inspection_api)
    app.register_blueprint(device_api)
    app.register_blueprint(imaging_api)
    app.register_blueprint(history_api)

    # 图像文件服务：接收任意本地绝对路径，返回文件内容
    @app.route('/api/image')
    def serve_image():
        """GET /api/image?path=<磁盘绝对路径>"""
        import os
        from urllib.parse import unquote
        path = unquote(request.args.get('path', ''))
        if not path or not os.path.isfile(path):
            return jsonify({'code': 404, 'message': '文件不存在', 'data': None}), 404
        return send_file(path)

    # 注册图像采集蓝图
    app.register_blueprint(capture_api)

    return app


flask_app = create_app()
socketio = init_socketio(flask_app)

if __name__ == '__main__':
    print(f'[启动] 绿通快检后端 API 服务启动于 http://{HOST}:{PORT}')
    print(f'[API] 已注册模块:')
    print(f'  auth       POST /api/auth/login, GET /api/auth/me')
    print(f'  user       GET /api/user/query, POST|DELETE /api/user/...')
    print(f'  booking    GET|POST /api/booking/...')
    print(f'  dict       GET /api/dict/products|truck-types|...')
    print(f'  inspection POST /api/inspection/submit, GET /api/inspection/...')
    print(f'  device     GET /api/device/status|health, POST /api/device/...')
    print(f'  imaging    GET|POST /api/imaging/...')
    print(f'  history    GET /api/history/list|export|logs')
    print(f'  ws         /ws (WebSocket)')

    # ---- 初始化设备管理器（对齐 Qt 启动流程） ----
    print('[设备] 正在初始化设备管理器...')
    from app.services.device_manager import DeviceManager
    from ws.handler import push_device_status

    mgr = DeviceManager()

    # 1. 从数据库加载设备，创建控制器实例
    loaded = mgr.load_from_db()
    print(f'[设备] 加载了 {len(loaded)} 台设备')

    # 2. 逐个初始化设备（ping 硬件中间层）
    init_results = mgr.initialize_all()
    for dev_id, ok in init_results.items():
        status = '在线' if ok else '离线'
        print(f'[设备]   {dev_id}: {status}')

    # 3. 注入 WebSocket 推送（设备状态变化时通知前端）
    mgr.set_ws_push(push_device_status)

    # 3.5 注入 PLC 状态推送（对齐 Qt UDPRadar::actionCompleted("plc_status") → LvTongPro::onPLCStatusUpdate）
    from ws.handler import push_plc_status

    def _on_plc_status(parsed: dict):
        """RadarReader 收到 $NTRMC,PLC,<hex> 包时的回调
        对齐 Qt LvTongPro::onPLCStatusUpdate() 的字段映射
        """
        push_plc_status(
            red=parsed.get('redLightCmd', False),
            yellow=parsed.get('yellowLightCmd', False),
            green=parsed.get('greenLightCmd', False),
            greatlight=parsed.get('createLightCmd', False),
            lightgate200=parsed.get('lightGate200Status', False),
            lightgate160=parsed.get('lightGate160Status', False),
            urgentstop=parsed.get('urgentStopStatus', False),
            booking=parsed.get('bookingStatus', False),
            groundsensor=parsed.get('groundSensorStatus', False),
            lightscreen=parsed.get('lightScreenStatus', False),
            lightsource200=parsed.get('lightSource200Status', False),
            lightsource160=parsed.get('lightSource160Status', False),
        )

    for radar in mgr.get_devices_by_type('udpradar'):
        radar.set_plc_status_callback(_on_plc_status)
        print(f'[设备]   {radar.device_id}: PLC 状态推送已注入')

    # 3.6 初始化距离调度器，将雷达距离数据接入调度引擎
    from app.services.scheduler import DistanceScheduler
    import os

    scheduler = DistanceScheduler()
    config_path = os.path.join(os.path.dirname(__file__), 'scheduler_config.json')
    scheduler.load_configuration_from_file(config_path)
    print(f'[调度器] 配置加载完成: {config_path}')

    # 注册雷达距离回调 → 调度器
    for radar in mgr.get_devices_by_type('udpradar'):
        def _make_distance_cb(sched=scheduler):
            def _on_distance(distance: float, mode: int):
                sched.on_radar_data(distance, mode)
            return _on_distance
        radar.set_distance_callback(_make_distance_cb())
        print(f'[设备]   {radar.device_id}: 距离调度器已注入')

    # 4. 启动 20 秒定时健康检查 + 自动重连
    mgr.start_health_check()
    print('[设备] 健康检查已启动 (间隔 20s)')

    socketio.run(flask_app, host=HOST, port=PORT, debug=DEBUG, allow_unsafe_werkzeug=True)
