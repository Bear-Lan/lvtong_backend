"""绿通快检系统 - Flask 后端 API 入口

前后端分离架构：
- 前端: Vue3 + Vite (端口 5173)
- 后端: Flask REST API (端口 8080)
- WebSocket: /ws
"""
from flask import Flask
from flask_cors import CORS

from config import HOST, PORT, DEBUG, CORS_ORIGINS

# 认证 & 用户管理
from app_api import app_api
from service.userservice import user_api

# 业务 API
from booking.booking_api import booking_api
from dictionary_api import dict_api
from vehicle_inspection_api import inspection_api
from device_api import device_api
from imaging_api import imaging_api
from history_api import history_api

# WebSocket
from ws.ws_handler import init_socketio


def create_app() -> Flask:
    """创建并配置 Flask 应用"""
    app = Flask(__name__)

    # CORS 跨域支持
    CORS(
        app,
        origins=CORS_ORIGINS.split(','),
        supports_credentials=True,
        allow_headers=['Content-Type', 'Authorization'],
    )

    # JWT 密钥
    app.config['SECRET_KEY'] = 'gcms-jwt-secret-key-2025'

    # 设备/图像源 URL 配置（从环境变量或 config.py 读取）
    from config import RADAR_HEAD_URL, XRAY_BASE_URL
    app.config['RADAR_HEAD_URL'] = RADAR_HEAD_URL
    app.config['XRAY_BASE_URL'] = XRAY_BASE_URL

    # 注册 API 蓝图
    app.register_blueprint(app_api)
    app.register_blueprint(user_api)
    app.register_blueprint(booking_api)
    app.register_blueprint(dict_api)
    app.register_blueprint(inspection_api)
    app.register_blueprint(device_api)
    app.register_blueprint(imaging_api)
    app.register_blueprint(history_api)

    return app


# 创建 Flask 应用
flask_app = create_app()

# 初始化 WebSocket
socketio = init_socketio(flask_app)

if __name__ == '__main__':
    print(f'[启动] 绿通快检后端 API 服务启动于 http://{HOST}:{PORT}')
    print(f'[API] 已注册模块:')
    print(f'  auth       POST /api/auth/login, GET /api/auth/me, POST /api/auth/change-password')
    print(f'  user       GET /api/user/query, POST /api/user/update/<user>, DELETE /api/user/delete/<user>')
    print(f'  booking    GET /api/booking/radar-image, POST /api/booking/accept|reject')
    print(f'  dict       GET /api/dict/products|truck-types|container-types|no-pass-types')
    print(f'  inspection POST /api/inspection/submit, GET /api/inspection/query|<id>|plate/<plate>')
    print(f'  device     GET /api/device/status|health, POST /api/device/<id>/control|reconnect')
    print(f'  imaging    POST /api/imaging/load-rate|ocr/driving-license|stitch')
    print(f'  history    GET /api/history/list|export|statistics|logs')
    print(f'  ws         /ws (WebSocket)')

    # 使用 Flask-SocketIO 启动
    socketio.run(flask_app, host=HOST, port=PORT, debug=DEBUG, allow_unsafe_werkzeug=True)
