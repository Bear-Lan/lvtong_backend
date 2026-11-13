"""检测中草稿同步接口；全部需要登录，无任何设备动作。"""
from flask import Blueprint, jsonify, request
from app.extensions.auth import login_required
from app.services.inspection_session import get_store, broadcast, SessionConflict

session_api = Blueprint('inspection_session', __name__, url_prefix='/api/inspection-session')


def init_session_sync(app, store):
    from app.services.inspection_session import configure_store, record_event
    configure_store(store)
    snapshot = store.snapshot()
    if snapshot['phase'] in ('booking', 'detecting', 'submitted'):
        from app.api.booking import set_booking_state
        # 只恢复业务数据，绝不在启动或换设备时重发采集、抬杆或射线命令。
        restored = dict(snapshot.get('booking') or {})
        restored.update(is_detection=snapshot['phase'] in ('detecting', 'submitted'),
                        check_step=snapshot.get('workflow', {}).get('step', restored.get('check_step', 0)))
        set_booking_state(**restored)

    @app.before_request
    def check_inspection_session():
        if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return None
        if request.path not in ('/api/booking/open', '/api/booking/accept', '/api/booking/reject', '/api/booking/reset', '/api/inspection/submit'):
            return None
        try:
            store.assert_current(request.headers.get('X-Inspection-Session'))
            if request.path == '/api/booking/accept' and store.snapshot()['phase'] in ('detecting', 'submitted'):
                raise SessionConflict('本次预约已受理，请恢复当前检测内容')
        except SessionConflict as error:
            return jsonify(code=409, message=str(error), data=None), 409

    @app.after_request
    def persist_booking_session(response):
        if request.method == 'POST' and response.status_code < 300:
            event = {'/api/booking/open': 'booking_open', '/api/booking/accept': 'booking_accepted',
                     '/api/booking/reject': 'booking_rejected', '/api/booking/reset': 'booking_reset',
                     '/api/booking/btn-press': 'booking_state'}.get(request.path)
            if event:
                if event == 'booking_accepted':
                    body = request.get_json(silent=True) or {}
                    ref = body.get('radarHeadImageUrl')
                    if ref:
                        try:
                            snap = store.snapshot()
                            store.patch(snap['sessionId'], snap['revision'], [{'path': ['radarHeadImageUrl'], 'value': ref}])
                        except ValueError as error:
                            app.logger.warning('受理保留，但预约雷达图同步失败：%s', error)
                record_event(event, {'activeClientId': request.headers.get('X-Inspection-Client', '')})
        return response


@session_api.route('/current', methods=['GET', 'PATCH'])
@login_required
def current_session():
    store = get_store()
    if store is None:
        return jsonify(code=503, message='检测同步尚未初始化', data=None), 503
    try:
        if request.method == 'GET':
            snapshot = store.snapshot()
        else:
            body = request.get_json(silent=True) or {}
            snapshot = store.patch(body.get('sessionId'), body.get('revision'), body.get('changes'), request.gc_user['username'], request.headers.get('X-Inspection-Client', ''))
            broadcast(snapshot)
        return jsonify(code=0, message='success', data=snapshot)
    except SessionConflict as e:
        return jsonify(code=409, message=str(e), data=store.snapshot()), 409
    except (ValueError, TypeError, KeyError) as e:
        return jsonify(code=400, message=str(e), data=None), 400
