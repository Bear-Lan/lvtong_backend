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

    特殊处理：booking/book_button 在转给前端的同时入栈到 _booking_state。

    调试期日志：每条 emit 都打一行摘要，便于排查 mock_back → flask → 前端链路。
    """
    if not _is_local_request():
        return jsonify({'code': 403, 'message': '仅允许本机访问', 'data': None}), 403

    body = request.get_json(silent=True) or {}
    msg_type = body.get('type')
    data = body.get('data', {})
    if not msg_type:
        return jsonify({'code': 400, 'message': '缺少 type 字段', 'data': None}), 400

    # 调试日志：每条 emit 都打印（摘要形式，避免打爆控制台）
    _summarize = {
        'booking': lambda d: f"action={d.get('action')!r} btnPressTime={d.get('btnPressTime')!r}",
        'radar_distance': lambda d: f"distance={d.get('distance')} mode={d.get('mode')}",
        'detection_step': lambda d: f"step={d.get('step')} msg={d.get('message')!r}",
        'image_ready': lambda d: f"type={d.get('imageType')!r} group={d.get('group')} urls#={len(d.get('urls') or {})}",
        'device_status': lambda d: f"device={d.get('device_id', d.get('deviceId', d.get('id')))} ok={d.get('ok')}",
        'mobile_upload': lambda d: f"session={d.get('sessionId')} plate_gc={d.get('plate_number_gc')!r}",
        'mobile_passcode': lambda d: f"valid={d.get('valid')} vehicleId={d.get('vehicleId')!r}",
    }
    summarizer = _summarize.get(msg_type)
    if summarizer:
        try:
            summary = summarizer(data if isinstance(data, dict) else {})
        except Exception:
            summary = repr(data)[:80]
    else:
        summary = repr(data)[:80] if data else ''
    logger.info(f'[INTERNAL.EMIT] type={msg_type!r} {summary}')

    # 司机按键预约 — 直接入栈（避免 round-trip 到 /api/booking/btn-press）
    if msg_type == 'booking' and isinstance(data, dict) and data.get('action') == 'book_button':
        try:
            from app.api.booking import _book_button_stack_push
            btn_time = _book_button_stack_push()
            logger.info(f'  └ book_button 入栈: {btn_time}')
        except Exception as e:
            logger.warning(f'  └ book_button 入栈失败: {e}')

    # 调试日志：socketio emit 之后打一下确认成功 + 在线客户端数
    try:
        from app.websocket.handler import get_socketio_clients_count
        client_count = get_socketio_clients_count()
    except Exception:
        client_count = -1
    logger.info(f'  └ socketio.emit → ws clients={client_count}')

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