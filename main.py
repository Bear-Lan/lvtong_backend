"""绿通快检系统 - Flask 后端 API 入口

前后端分离架构：
- 前端: Vue3 + Vite (端口 5173)
- 后端: Flask REST API (端口 8080)
- WebSocket: /ws
"""
from flask import Flask
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

    socketio.run(flask_app, host=HOST, port=PORT, debug=DEBUG, allow_unsafe_werkzeug=True)
