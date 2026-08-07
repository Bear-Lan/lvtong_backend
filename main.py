"""绿通快检系统 - Flask 后端 API 入口

前后端分离架构：
- 前端: Vue3 + Vite (端口 5173)
- 后端: Flask REST API (HTTP 8080 + HTTPS 8082)
- WebSocket: /ws
"""
import logging
import signal
import sys

from gevent import monkey
monkey.patch_all()

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
from app.api.internal import internal_api
from app.api.mobile import mobile_api
from app.api.file import file_api
from app.api.debug import debug_api
from app.api.capture import capture_api
from app.api.live import live_api

# WebSocket
from app.websocket.handler import init_socketio


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
    app.register_blueprint(internal_api)
    app.register_blueprint(mobile_api)
    app.register_blueprint(file_api)
    app.register_blueprint(debug_api)

    # 图像文件服务：接收任意本地绝对路径，返回文件内容
    @app.route('/api/image')
    def serve_image():
        """GET /api/image?path=<磁盘绝对路径>（保留兼容，新代码请用 /api/images/<rel>）"""
        import os
        from urllib.parse import unquote
        path = unquote(request.args.get('path', ''))
        if not path or not os.path.isfile(path):
            return jsonify({'code': 404, 'message': '文件不存在', 'data': None}), 404
        return send_file(path)

    # 图像文件服务（新）：/api/images/<relative>，绑定 IMAGE_STORAGE_ROOT
    @app.route('/api/images/<path:rel>')
    def serve_storage_image(rel: str):
        """GET /api/images/<rel> — 把 IMAGE_STORAGE_ROOT 下的相对路径作为静态文件返回。

        强制沙箱：拒绝任何越级访问（.. 或绝对路径）。
        """
        import os
        from config import IMAGE_STORAGE_ROOT
        if not rel or '..' in rel.split('/'):
            return jsonify({'code': 400, 'message': '非法的相对路径', 'data': None}), 400
        norm_root = os.path.normpath(IMAGE_STORAGE_ROOT)
        target = os.path.normpath(os.path.join(norm_root, rel))
        # 再次确保在根目录之内（防 ../ 与 symlink）
        try:
            if os.path.commonpath([target, norm_root]) != norm_root:
                return jsonify({'code': 403, 'message': '访问被拒绝', 'data': None}), 403
        except ValueError:
            return jsonify({'code': 403, 'message': '访问被拒绝', 'data': None}), 403
        if not os.path.isfile(target):
            return jsonify({'code': 404, 'message': '文件不存在', 'data': None}), 404
        return send_file(target)

    # 注册图像采集蓝图
    app.register_blueprint(capture_api)
    # 内置实时视频检测（MJPEG + YOLO）
    app.register_blueprint(live_api)

    return app


flask_app = create_app()
socketio = init_socketio(flask_app)

# gevent + flask-socketio 默认不能干净处理 Ctrl+C（raise KeyboardInterrupt in gevent hub）。
# 注册 SIGINT/SIGTERM 让 Ctrl+C 能立即终止进程。
def _shutdown_handler(signum, frame):
    print(f'\n[SHUTDOWN] 收到信号 {signum}，正在关闭…', flush=True)
    try:
        from app.services.live_detect import stop_live_detect
        stop_live_detect()
    except Exception:
        pass
    try:
        from app.services.mediamtx_launcher import stop_mediamtx
        stop_mediamtx()
    except Exception:
        pass
    try:
        from app.services.scheduler import DistanceScheduler
        DistanceScheduler().stop()
    except Exception:
        pass
    try:
        from app.services.mobile_watcher import stop_mobile_watcher
        stop_mobile_watcher()
    except Exception:
        pass
    import os
    os._exit(0)  # 强制退出，跳过 gevent 清理

signal.signal(signal.SIGINT, _shutdown_handler)
signal.signal(signal.SIGTERM, _shutdown_handler)


# 屏蔽健康检测端点的 access log（Android 移动 app + mock_back 探测刷屏）
class _HealthCheckFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return not ('/api/mobile/health' in msg or '/api/internal/health' in msg)


# 给所有可能输出 access log 的 logger 都加过滤器
for _name in ('werkzeug', 'gevent.pywsgi', 'gevent.server', 'gevent'):
    logging.getLogger(_name).addFilter(_HealthCheckFilter())
# 同时给 root 加一份，防止子 logger 没继承
logging.getLogger().addFilter(_HealthCheckFilter())


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
    print(f'  mobile     GET /api/mobile/health, POST /api/mobile/upload/{{image,json}}')
    print(f'  ws         /ws (WebSocket)')

    # ---- 启动移动 app 文件目录监听器（对齐 Qt JsonFolderWatcher） ----
    print('[移动] 启动移动 app 目录监听器...')
    from app.services.mobile_watcher import start_mobile_watcher
    start_mobile_watcher()

    # ---- 初始化设备管理器（对齐 Qt 启动流程） ----
    print('[设备] 正在初始化设备管理器...')
    from app.devices.manager import DeviceManager
    from app.websocket.handler import push_device_status

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
    from app.websocket.handler import push_plc_status

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
    config_path = os.path.join(os.path.dirname(__file__), 'config', 'scheduler_config.json')
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

    # 4.5 主页实时视频：MediaMTX（WHEP，预约/对讲 cam4）
    try:
        from app.services.mediamtx_launcher import start_mediamtx
        start_mediamtx()
    except Exception as e:
        print(f'[MediaMTX] 自动启动异常: {e}')

    # 注意：LiveDetect / YOLO 绝不能在 HTTP 服务起来之前同步启动。
    # torch/ultralytics 初始化会长时间占用 GIL，导致登录等接口全部 502。

    # ===== 双协议启动：HTTP 8080 + HTTPS 8082 =====
    # - HTTP 8080：前端 Vite 代理 / mock_back HTTP 调用 / curl 测试
    # - HTTPS 8082：Android 移动 app 上传（自签证书）
    # 共享同一个 Flask app + SocketIO

    from gevent import pywsgi
    import gevent

    # 关键：flask_socketio.init_app() 已自动创建 socketio.sockio_mw（_SocketIOMiddleware）
    # 它正确处理 /socket.io/* 请求（EngineIO 协议）+ 其他请求转发给 Flask app
    # 不能用 WSGIApp(socketio, flask_app) 或 socketio 本身（会触发 handle_request 错误）
    wsgi_app = socketio.sockio_mw

    # HTTPS 证书路径（gevent 用 ssl_args 传 certfile/keyfile）
    cert_path = None
    key_path = None
    try:
        from app.services.tls_cert import _ensure_cert
        cert_path, key_path = _ensure_cert()
        print(f'[HTTPS] 自签证书加载完成: {cert_path}')
    except Exception as e:
        print(f'[HTTPS] 加载证书失败: {e}')

    http_port = PORT  # 8080
    # HTTPS 端口（避开 mock_back 的 8081）：默认 8082
    https_port = int(os.environ.get('HTTPS_PORT', '8082'))

    # gevent.pywsgi 默认 log=sys.stderr（不走 logging），要让 filter 生效必须显式传 logger
    gevent_logger = logging.getLogger('gevent.pywsgi')
    http_server = pywsgi.WSGIServer(
        (HOST, http_port), wsgi_app,
        log=gevent_logger,
    )
    if cert_path and key_path:
        https_server = pywsgi.WSGIServer(
            (HOST, https_port), wsgi_app,
            certfile=cert_path,
            keyfile=key_path,
            log=gevent_logger,
        )
    else:
        https_server = pywsgi.WSGIServer(
            (HOST, https_port), wsgi_app,
            log=gevent_logger,
        )  # 回退 HTTP

    print(f'[SERVER] HTTP  启动 http://{HOST}:{http_port}')
    print(f'[SERVER] HTTPS 启动 https://{HOST}:{https_port}')
    # LiveDetect 不自动启动：需前端 POST /api/live/ensure 手动拉起

    try:
        gevent.joinall([
            gevent.spawn(http_server.serve_forever),
            gevent.spawn(https_server.serve_forever),
        ])
    except KeyboardInterrupt:
        print('[SHUTDOWN] 收到 Ctrl+C，正在关闭…')
    finally:
        try:
            from app.services.live_detect import stop_live_detect
            stop_live_detect()
        except Exception:
            pass
        try:
            from app.services.mediamtx_launcher import stop_mediamtx
            stop_mediamtx()
        except Exception:
            pass
        http_server.stop()
        https_server.stop()
