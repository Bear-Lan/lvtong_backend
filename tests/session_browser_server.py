"""本机隔离浏览器验收：SQLite临时库、模拟图像，禁止调用现场设备。"""
import base64
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

temp = tempfile.TemporaryDirectory(prefix='lvtong-session-browser-')
os.environ['IMAGE_STORAGE_ROOT'] = str(Path(temp.name) / 'images')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, join_room
from sqlalchemy import create_engine
from app.api.session import session_api, init_session_sync
from app.api.inspection import inspection_api
from app.extensions.auth import create_token, decode_token
from app.services.inspection_session import InspectionSessionStore, broadcast
from app.services import temp_image_cache
from app.services.radar_point_cloud import decode_body_radar_data
from app.websocket import handler
import cv2
import numpy as np

app = Flask(__name__)
sio = SocketIO(app, async_mode='threading', cors_allowed_origins='*')
handler.socketio = sio
engine = create_engine('sqlite:///' + str(Path(temp.name) / 'session.db'))
store = InspectionSessionStore(engine, os.environ['IMAGE_STORAGE_ROOT'])
store.initialize()
app.register_blueprint(session_api)
app.register_blueprint(inspection_api)
init_session_sync(app, store)
submissions = []

def save_record(body):
    submissions.append(dict(body))
    return 321
for name, mock in (
    ('app.api.inspection.DBVehicle.addInspection', save_record),
    ('app.db.user.DBUser.getUserByUsername', lambda *a: {}),
):
    patch(name, side_effect=mock).start()

@sio.on('connect')
def connected(auth=None):
    if auth and decode_token(auth.get('token', '')):
        join_room('inspection_sessions')

@app.get('/fixture/token')
def token():
    return {'token': create_token({'username': 'sync-test', 'real_name': '同步测试'})}

@app.post('/fixture/begin')
def begin():
    submissions.clear()
    store.record('booking_reset', {})
    booking = {'is_detection': True, 'check_step': 2, 'car_height': 3., 'car_length': 1.8,
               'is_check_xray': False, 'btn_prebook_state': False, 'acceptance_time': '2026-11-13T20:00:00'}
    from app.api.booking import get_booking_state
    get_booking_state().update(booking)
    store.record('booking_accepted', {'booking': booking, 'activeClientId': request.headers.get('X-Inspection-Client', '')})
    store.record('detection_step', {'step': 3, 'message': '检测中', 'gateReleased': True, 'booking': booking})
    image = np.full((500,1200,3), 220, np.uint8)
    cv2.putText(image, 'BODY SCAN', (150,250), cv2.FONT_HERSHEY_SIMPLEX, 3, (40,130,70), 7)
    _, encoded = cv2.imencode('.png', image)
    body = temp_image_cache.put_and_url(encoded.tobytes())
    store.record('image_ready', {'imageType': 'body', 'urls': {'1': body}, 'booking': booking})
    store.record('image_ready', {'imageType': 'transparent', 'urls': {'1': body}, 'booking': booking})
    frames, geometry = decode_body_radar_data((Path(__file__).parent / 'fixtures/vehicle.laz').read_bytes())
    views = {k: {'imageUrl': temp_image_cache.put_and_url(v.image), 'metersPerPixel': .01} for k,v in frames.items()}
    store.record('geometry', {'geometry': geometry, 'views': views, 'booking': booking})
    snap = store.snapshot()
    store.patch(snap['sessionId'], snap['revision'], [
        {'path': ['form','plate'], 'value': '鄂A12345'}, {'path': ['form','goods'], 'value': '白菜'},
        {'path': ['form','goodsProductCode'], 'value': '10101'},
        {'path': ['form','truckType'], 'value': '1'}, {'path': ['form','containerType'], 'value': '1'},
        {'path': ['form','size'], 'value': '4404|1992|1810'},
        *[{'path': ['captureThumbs', key], 'value': body} for key in ('head', 'tail', 'license', 'passcode')],
        {'path': ['passcode'], 'value': {'valid': True, 'enStationId': 'EN', 'exStationId': 'EX',
          'enWeight': '1000', 'exWeight': '1234', 'mediaTypeId': 1, 'transactionId': 'TEST-TXN',
          'passId': 'TEST-PASS', 'exTime': '2026-11-13 20:00:00', 'fee': 100, 'payFee': 100,
          'vehicleSignId': 1, 'provinceCount': 1}},
        {'path': ['captureLists','goods'], 'value': [body]},
        {'path': ['radarHeadImageUrl'], 'value': body}])
    broadcast(store.snapshot())
    return jsonify(code=0, data=store.snapshot())

@app.get('/fixture/submissions')
def submitted():
    return jsonify(submissions)

@app.post('/fixture/booking')
def booking_fixture():
    from app.api.booking import get_booking_state
    booking = dict(get_booking_state(), is_detection=False, check_step=0)
    get_booking_state().update(booking)
    store.record('booking_reset', {'booking': booking})
    snap = store.record('booking_open', {'booking': booking})
    store.patch(snap['sessionId'], snap['revision'], [
        {'path': ['bookingDraft', 'linePosition'], 'value': .4},
        {'path': ['bookingDraft', 'carHeadLength'], 'value': 4.0},
        {'path': ['bookingDraft', 'vehicleType'], 'value': 'small'},
        {'path': ['bookingDraft', 'xrayEnabled'], 'value': False},
    ])
    broadcast(store.snapshot())
    return jsonify(code=0)

@app.post('/api/booking/reset')
def reset():
    # 只让after_request保存空闲快照，不执行真实reset里的设备动作。
    from app.api.booking import get_booking_state
    get_booking_state().update(is_detection=False, check_step=0)
    return jsonify(code=0, data={})

@app.get('/api/images/<path:rel>')
def image(rel):
    return send_from_directory(os.environ['IMAGE_STORAGE_ROOT'], rel)

@app.get('/api/temp-images/<tid>')
def cached(tid):
    from flask import Response
    entry = temp_image_cache.get(tid)
    return Response(entry.data, mimetype=entry.mime) if entry else ('missing', 404)

@app.route('/api/<path:path>', methods=['GET','POST'])
def fallback(path):
    if path in ('dict/truck-types', 'dict/container-types'):
        return jsonify(code=0, data=[{'type_code': '1', 'type_name': '测试车型'}])
    if path == 'user/query':
        return jsonify(code=0, data=[{'username': 'sync-test', 'real_name': '同步测试', 'phone': '13800000000', 'role': 'admin'}])
    if path.startswith('ai-') or 'ocr' in path:
        return jsonify(code=0, data={})
    if path == 'device/plc-status':
        return jsonify(code=0, data={'fresh': True, 'stickdown': False})
    if path == 'device/body-radar-image':
        return jsonify(code=0, data={'capture': {'generation': 0, 'hasData': False, 'state': 'idle'}})
    if path == 'device/status':
        return jsonify(code=0, data={'sensors': []})
    return jsonify(code=0, data=[])

if __name__ == '__main__':
    sio.run(app, host='127.0.0.1', port=5198, allow_unsafe_werkzeug=True)
