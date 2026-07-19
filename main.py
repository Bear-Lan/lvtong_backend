"""绿通快检系统 - Flask 后端 API 入口

前后端分离架构：
- 前端: Vue3 + Vite (端口 5173)
- 后端: Flask REST API (端口 8080)
- WebSocket: /ws
"""
from flask import Flask
from flask_cors import CORS

from config import HOST, PORT, DEBUG, CORS_ORIGINS
from app_api import app_api
from service.userservice import user_api
from booking.booking_api import booking_api
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

    # JWT 密钥（用于 auth.py 中 token 签名）
    app.config['SECRET_KEY'] = 'gcms-jwt-secret-key-2025'

    # 注册 API 蓝图
    app.register_blueprint(app_api)
    app.register_blueprint(user_api)
    app.register_blueprint(booking_api)

    return app


# 创建 Flask 应用
flask_app = create_app()

# 初始化 WebSocket（必须在 create_app 之后）
socketio = init_socketio(flask_app)

if __name__ == '__main__':
    print(f'[启动] 后端 API 服务启动于 http://{HOST}:{PORT}')
    print(f'[启动] API 文档:')
    print(f'  POST /api/auth/login          - 用户登录')
    print(f'  GET  /api/auth/me             - 获取当前用户')
    print(f'  POST /api/auth/change-password - 修改密码')
    print(f'  GET  /api/user/query          - 查询用户列表')
    print(f'  POST /api/user/update/<user>  - 创建/更新用户')
    print(f'  DELETE /api/user/delete/<user> - 删除用户')
    print(f'  GET  /api/booking/radar-image - 雷达来车图')
    print(f'  POST /api/booking/accept      - 受理预约')
    print(f'  POST /api/booking/reject      - 驳回预约')
    print(f'  WS   /ws                      - WebSocket')

    # 使用 Flask-SocketIO 启动（开发模式）
    socketio.run(flask_app, host=HOST, port=PORT, debug=DEBUG, allow_unsafe_werkzeug=True)
