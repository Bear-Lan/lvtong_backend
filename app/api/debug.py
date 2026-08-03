"""调试端点：随时查 WS 在线客户端数 + booking 栈状态。"""
from flask import Blueprint, jsonify, request
from app.websocket.handler import get_socketio_clients_count

debug_api = Blueprint('debug', __name__, url_prefix='/api/debug')

# 本机访问（与 internal_api 一致）
_ALLOWED_HOSTS = {'127.0.0.1', 'localhost', '::1'}


def _is_local_request() -> bool:
    return request.remote_addr in _ALLOWED_HOSTS


@debug_api.route('/ws-stats')
def ws_stats():
    if not _is_local_request():
        return jsonify({'code': 403, 'message': '仅本机访问', 'data': None}), 403
    try:
        from app.api.booking import _booking_state
        return jsonify({
            'code': 0,
            'message': 'ok',
            'data': {
                'ws_clients': get_socketio_clients_count(),
                'btn_prebook_stack': list(_booking_state.get('btn_prebook_stack', [])),
                'btn_prebook_stack_len': len(_booking_state.get('btn_prebook_stack', [])),
                'last_acceptance_time': _booking_state.get('last_acceptance_time', ''),
                'btn_prebook_time': _booking_state.get('btn_prebook_time', ''),
                'acceptance_time': _booking_state.get('acceptance_time', ''),
                'opengate_time': _booking_state.get('opengate_time', ''),
                'openlightscreen_time': _booking_state.get('openlightscreen_time', ''),
                'closelightscreen_time': _booking_state.get('closelightscreen_time', ''),
                'cd_photo_time': _booking_state.get('cd_photo_time', ''),
                'is_detection': _booking_state.get('is_detection', False),
                'check_step': _booking_state.get('check_step', 0),
            },
        })
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e), 'data': None}), 500