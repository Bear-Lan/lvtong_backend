"""WebSocket 处理器

使用 flask-socketio 提供 WebSocket 服务，路径为 /ws。
前端使用的消息格式: { type: string, timestamp?: number, data?: T }
"""
import time
from flask_socketio import SocketIO, emit

# SocketIO 实例（在 main.py 中初始化）
socketio = SocketIO()


def init_socketio(app):
    """初始化 SocketIO，绑定 Flask 应用"""
    socketio.init_app(
        app,
        cors_allowed_origins='*',
        async_mode='threading',  # Windows 兼容；Linux 可改为 gevent/eventlet
        ping_timeout=60,
        ping_interval=25,
    )
    register_handlers()
    return socketio


def register_handlers():
    """注册 WebSocket 事件处理器"""

    @socketio.on('connect')
    def handle_connect():
        print(f'[WS] 客户端连接: {id(handle_connect)}')
        emit('message', {
            'type': 'connect_ack',
            'timestamp': int(time.time() * 1000),
            'data': {'message': '连接成功'},
        })

    @socketio.on('disconnect')
    def handle_disconnect():
        print('[WS] 客户端断开')

    @socketio.on('message')
    def handle_message(payload):
        """处理通用消息"""
        msg_type = payload.get('type', '') if isinstance(payload, dict) else ''

        if msg_type == 'ping':
            # 心跳响应
            emit('message', {
                'type': 'pong',
                'timestamp': int(time.time() * 1000),
                'data': {'server_time': int(time.time() * 1000)},
            })
        elif msg_type == 'subscribe':
            # 订阅消息类型
            topics = payload.get('data', {}).get('topics', [])
            print(f'[WS] 客户端订阅: {topics}')
            emit('message', {
                'type': 'subscribe_ack',
                'timestamp': int(time.time() * 1000),
                'data': {'topics': topics},
            })
        else:
            print(f'[WS] 收到未知消息类型: {msg_type}')

    @socketio.on('ping')
    def handle_ping(data=None):
        """独立 ping 事件处理"""
        emit('pong', {
            'type': 'pong',
            'timestamp': int(time.time() * 1000),
            'data': {'server_time': int(time.time() * 1000)},
        })

