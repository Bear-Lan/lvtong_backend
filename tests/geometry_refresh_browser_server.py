"""沿用临时会话浏览器夹具，真实刷新API/解码/持久化；中间件HTTP及命令全部拦截。"""
from pathlib import Path
from unittest.mock import patch
from requests import Response
from session_browser_server import app, sio, store
from app.api.device import body_radar_image
from app.services import geometry_capture as capture

calls = []
def fetch(url, **kwargs):
    calls.append(url)
    response = Response(); response.status_code = 200
    response._content = (Path(__file__).parent / 'fixtures/vehicle.laz').read_bytes()
    response.headers['Capture-Finish'] = 'true'
    return response

patch.object(capture, 'middleware_get_payload', side_effect=fetch).start()
patch.object(capture, 'ensure_capture_allowed', return_value=1).start()
patch.object(capture, 'send_geomcap', side_effect=AssertionError('禁止设备动作')).start()
patch('app.services.body_radar_projection._run_in_pool', side_effect=lambda fn: fn()).start()
app.add_url_rule('/api/device/body-radar-image', 'real_geometry_refresh', body_radar_image, methods=['POST'])

@app.post('/fixture/no-cloud')
def no_cloud():
    calls.clear()
    store.record('booking_reset', {})
    store.record('booking_accepted', {})
    capture._result, capture._frame, capture._state, capture._error = None, 0, 'finished', '采集已结束，但未返回有效点云'
    store.record('geometry_status', {'capture': capture.get_geometry_snapshot()['capture']})
    return {'code': 0}

@app.get('/fixture/laz-requests')
def requests(): return {'calls': len(calls)}

if __name__ == '__main__':
    sio.run(app, host='127.0.0.1', port=5198, allow_unsafe_werkzeug=True)
