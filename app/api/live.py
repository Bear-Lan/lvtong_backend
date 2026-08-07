"""内置实时视频检测 API（复现 VisualSurveillance WebStreamDemo）。"""
from flask import Blueprint, Response, jsonify, request

from app.extensions.auth import login_required
from app.services.live_detect import (
    get_alarms,
    get_latest_jpeg,
    get_status,
    is_running,
    load_zones,
    mjpeg_generator,
    reconnect,
    save_zones,
    start_live_detect,
)

live_api = Blueprint('live', __name__, url_prefix='/api/live')


def ok(data=None, message='ok'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(message: str, code: int = 1, http_status: int = 400):
    return jsonify({'code': code, 'message': message, 'data': None}), http_status


@live_api.route('/status', methods=['GET'])
@login_required
def live_status():
    return ok(get_status())


@live_api.route('/ensure', methods=['POST'])
@login_required
def live_ensure():
    if is_running():
        return ok(get_status(), message='already running')
    started = start_live_detect()
    if not started:
        st = get_status()
        return fail(st.get('error') or '启动失败', http_status=500)
    return ok(get_status(), message='started')


@live_api.route('/reconnect', methods=['POST'])
@login_required
def live_reconnect():
    # 限制 body 大小（与 demo 413 语义对齐）
    if request.content_length and request.content_length > 4096:
        return fail('body too large', http_status=413)
    success, status, msg = reconnect()
    if success:
        return jsonify({'accepted': True, 'message': msg, 'data': get_status()}), 202
    if status == 409:
        return fail(msg, http_status=409)
    return fail(msg, http_status=status if status >= 400 else 500)


@live_api.route('/frame.jpg', methods=['GET'])
def live_frame():
    """单帧 JPEG：不自动启动检测，未运行时返回 503。"""
    if not is_running():
        return fail('检测未启动，请先调用 POST /api/live/ensure', http_status=503)
    jpeg = get_latest_jpeg()
    if not jpeg:
        return fail('暂无画面', http_status=503)
    return Response(
        jpeg,
        mimetype='image/jpeg',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


@live_api.route('/stream.mjpg', methods=['GET'])
def live_stream():
    """MJPEG 流：无登录（供 <img> 直连）；不自动启动检测。"""
    if not is_running():
        return fail('检测未启动，请先调用 POST /api/live/ensure', http_status=503)
    return Response(
        mjpeg_generator(),
        mimetype='multipart/x-mixed-replace; boundary=live-frame',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


@live_api.route('/zones', methods=['GET'])
@login_required
def live_zones():
    return ok(load_zones())


@live_api.route('/zones', methods=['POST'])
@login_required
def live_zones_save():
    """保存区域配置（网页画区）。worker 通过 mtime 热重载。"""
    payload = request.get_json(silent=True) or {}
    try:
        saved = save_zones(payload)
    except Exception as e:
        return fail(f'保存失败：{e}', http_status=500)
    return ok(saved, message='已保存')


@live_api.route('/alarms', methods=['GET'])
@login_required
def live_alarms():
    try:
        limit = int(request.args.get('limit', '20'))
    except (TypeError, ValueError):
        limit = 20
    return ok(get_alarms(limit))
