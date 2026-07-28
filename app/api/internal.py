"""内部事件转发端点（仅供 mock_back / 设备 agent 调试用）

设备 agent / mock_back 通过 POST 把 WS 事件转给 flask_backend 推送给前端。

生产环境设备 agent 应直接集成 flask-socketio，无需走这个端点。
"""
import logging
import socket

from flask import Blueprint, jsonify, request

from app.websocket.handler import socketio

internal_api = Blueprint('internal', __name__, url_prefix='/api/internal')
logger = logging.getLogger('internal_emit')

# 仅允许本机访问（mock_back 是同进程或同机部署）
_ALLOWED_HOSTS = {'127.0.0.1', 'localhost', '::1'}


def _is_local_request() -> bool:
    return request.remote_addr in _ALLOWED_HOSTS


@internal_api.route('/emit', methods=['POST'])
def emit_event():
    """POST /api/internal/emit  Body: {"type": "...", "data": {...}}

    把消息推给前端（通过主项目 socketio）。
    """
    if not _is_local_request():
        return jsonify({'code': 403, 'message': '仅允许本机访问', 'data': None}), 403

    body = request.get_json(silent=True) or {}
    msg_type = body.get('type')
    data = body.get('data', {})
    if not msg_type:
        return jsonify({'code': 400, 'message': '缺少 type 字段', 'data': None}), 400

    socketio.emit('message', {
        'type': msg_type,
        'timestamp': body.get('timestamp'),
        'data': data,
    })
    return jsonify({'code': 0, 'message': 'pushed', 'data': None})


@internal_api.route('/health')
def health():
    """健康检查，供 mock_back 启动时探测 flask_backend 是否就绪"""
    if not _is_local_request():
        return jsonify({'code': 403, 'message': '仅允许本机访问', 'data': None}), 403
    return jsonify({'code': 0, 'message': 'ok', 'data': None})