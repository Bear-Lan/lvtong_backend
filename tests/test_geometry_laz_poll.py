"""LAZ 生命周期及真实压缩文件链路验证；所有控制命令拦截，不访问现场。"""
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch, Mock

from requests import Response
from flask import Flask

from app.services import geometry_capture as capture, body_radar_projection as projection
from app.services.capture_guard import CaptureBlockedError
from app.services.scheduler import DistanceScheduler
from app.services.threshold import DistanceThreshold
from app.api.device import device_api
from app.extensions.auth import create_token
from test_radar_point_cloud import cloud_bytes


def response(raw=b'', finish=None, status=200):
    result = Response()
    result.status_code, result._content = status, raw
    if finish is not None:
        result.headers['Capture-Finish'] = finish
    return result


class GeometryLazPollTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.poll_interval = capture.POLL_INTERVAL_SEC
        for name, value in (('_generation', 0), ('_armed', False), ('_started', False),
                            ('_poll_requested', False), ('_poll_started', False),
                            ('_result', None), ('_state', 'idle'), ('_error', ''), ('_frame', 0),
                            ('_stop', threading.Event()), ('POLL_INTERVAL_SEC', 0)):
            item = patch.object(capture, name, value)
            item.start(); self.addCleanup(item.stop)
        for item in (patch.object(capture, '_notify', side_effect=lambda: self.events.append(capture._snapshot()['capture'])),
                     patch.object(capture, 'send_geomcap', return_value=True),
                     patch.object(capture, 'ensure_capture_allowed', return_value=7),
                     patch.object(projection, '_run_in_pool', side_effect=lambda fn: fn())):
            item.start(); self.addCleanup(item.stop)

    def arm(self):
        generation = capture.prepare_geometry_capture()
        self.assertTrue(capture.start_geometry_capture(generation))
        return generation

    def run_responses(self, responses):
        generation = self.arm()
        with patch.object(capture, 'middleware_get_payload', side_effect=responses) as fetch:
            capture._poll_laz(generation, capture._stop)
        return fetch

    def test_waits_through_empty_http_error_and_timeout_until_header(self):
        fetch = self.run_responses([response(), response(b'error', status=503),
                                   TimeoutError('test timeout'), response(b'not json'),
                                   response(cloud_bytes(), 'false'), response(b'', ' TrUe ')])
        snapshot = capture.get_geometry_snapshot()
        self.assertEqual(fetch.call_count, 6)
        self.assertEqual(snapshot['capture']['state'], 'finished')
        self.assertEqual(snapshot['capture']['frame'], 1)
        self.assertEqual(snapshot['geometry']['totalPointCount'], 4)
        self.assertEqual([call.args[0] for call in capture.send_geomcap.call_args_list], [True, False])

    def test_final_laz_is_decoded_before_finish_notification(self):
        self.run_responses([response(cloud_bytes(), 'true')])
        result = capture.get_geometry_snapshot()
        self.assertEqual(result['source'], 'live')
        self.assertEqual(result['geometry']['dimensionsMeters'], {'length': 4., 'width': 2., 'height': 2.})
        self.assertTrue(self.events[-1]['hasData'])
        self.assertEqual(self.events[-1]['state'], 'finished')
        self.assertEqual(set(result['views']), {'top', 'side'})

    def test_repeated_file_decodes_once_before_finish(self):
        raw = cloud_bytes()
        with patch.object(capture, 'project_body_response', wraps=capture.project_body_response) as decode:
            fetch = self.run_responses([response(raw, 'false'), response(raw, 'false'), response(raw, 'true')])
        self.assertEqual(fetch.call_count, 3)
        decode.assert_called_once()
        self.assertEqual(capture.get_geometry_snapshot()['capture']['frame'], 1)

    def test_corrupt_laz_does_not_replace_valid_cloud_or_stop_early(self):
        self.run_responses([response(cloud_bytes()), response(b'LASFbad'), response(b'', 'true')])
        result = capture.get_geometry_snapshot()
        self.assertEqual(result['capture']['frame'], 1)
        self.assertTrue(result['capture']['hasData'])
        self.assertEqual(result['capture']['state'], 'finished')

    def test_finish_without_any_data_reports_empty_not_demo(self):
        self.run_responses([response(b'{"status":"false"}', 'true')])
        result = capture.get_geometry_snapshot()
        self.assertNotIn('geometry', result)
        self.assertIn('未返回有效点云', result['capture']['error'])

    def test_timeout_preserves_last_cloud_and_sends_stop(self):
        generation = self.arm()
        with patch.object(capture, 'time', SimpleNamespace(monotonic=Mock(side_effect=[0., 0., 181.]))), \
             patch.object(capture, 'middleware_get_payload', return_value=response(cloud_bytes())) as fetch:
            capture._poll_laz(generation, capture._stop)
        fetch.assert_called_once()
        self.assertTrue(capture.get_geometry_snapshot()['capture']['hasData'])
        self.assertEqual(capture.get_geometry_snapshot()['capture']['state'], 'timeout')
        self.assertEqual([call.args[0] for call in capture.send_geomcap.call_args_list], [True, False])

    def test_reset_during_get_discards_late_response(self):
        generation = self.arm()
        old_stop = capture._stop
        def late(*args, **kwargs):
            capture.stop_geometry_capture()
            return response(cloud_bytes(), 'true')
        with patch.object(capture, 'middleware_get_payload', side_effect=late):
            capture._poll_laz(generation, old_stop)
        self.assertNotIn('geometry', capture.get_geometry_snapshot())
        self.assertEqual(capture.get_geometry_snapshot()['capture']['state'], 'stopped')

    def test_new_vehicle_during_decode_discards_old_cloud(self):
        generation = self.arm()
        old_stop = capture._stop
        original = capture.project_body_response
        def decode(*args):
            result = original(*args)
            capture.prepare_geometry_capture()
            return result
        with patch.object(capture, 'middleware_get_payload', return_value=response(cloud_bytes(), 'true')), \
             patch.object(capture, 'project_body_response', side_effect=decode):
            capture._poll_laz(generation, old_stop)
        self.assertFalse(capture.get_geometry_snapshot()['capture']['hasData'])
        self.assertEqual(capture.get_geometry_snapshot()['capture']['state'], 'armed')

    def test_lane_block_during_get_discards_frame_and_resumes(self):
        generation = self.arm()
        with patch.object(capture, 'ensure_capture_allowed', side_effect=[7, CaptureBlockedError(), 8, 8, 8]), \
             patch.object(capture, 'middleware_get_payload', side_effect=[response(cloud_bytes()), response(cloud_bytes(), 'true')]):
            capture._poll_laz(generation, capture._stop)
        self.assertEqual(capture.get_geometry_snapshot()['capture']['frame'], 1)

    def test_blocked_lane_performs_no_http(self):
        generation = self.arm()
        with patch.object(capture, 'ensure_capture_allowed', side_effect=CaptureBlockedError()), \
             patch.object(capture, 'time', SimpleNamespace(monotonic=Mock(side_effect=[0., 0., 181.]))), \
             patch.object(capture, 'middleware_get_payload') as fetch:
            capture._poll_laz(generation, capture._stop)
        fetch.assert_not_called()

    def test_early_poll_waits_for_successful_command_and_starts_only_once(self):
        generation = capture.prepare_geometry_capture()
        with patch.object(capture.threading, 'Thread') as worker:
            capture.start_geometry_poll(generation)
            worker.assert_not_called()
            capture.send_geomcap.return_value = False
            self.assertFalse(capture.start_geometry_capture(generation))
            worker.assert_not_called()
            capture.send_geomcap.return_value = True
            self.assertTrue(capture.start_geometry_capture(generation))
            capture.start_geometry_poll(generation)
            capture.start_geometry_capture(generation)
        worker.assert_called_once()
        self.assertEqual(worker.call_args.kwargs['target'], capture._poll_laz)

    def test_old_distance_task_cannot_start_current_vehicle(self):
        old = self.arm()
        current = capture.prepare_geometry_capture()
        with patch.object(capture.threading, 'Thread') as worker:
            capture.start_geometry_poll(old)
            capture.start_geometry_capture(current)
        worker.assert_not_called()

    def test_scheduler_starts_laz_with_body_poll(self):
        generation = self.arm()
        from app.api import booking
        with patch.object(booking, 'get_booking_state', return_value={'geometry_generation': generation}), \
             patch.object(capture.threading, 'Thread', side_effect=lambda target, **kw: SimpleNamespace(start=target)), \
             patch.object(capture, 'start_geometry_poll') as laz, \
             patch('app.services.body_camera_fetch.start_body_camera_poll') as camera, \
             patch('app.services.progressive_image_fetch.start_progressive_panel_fetch') as xray:
            threshold = SimpleNamespace(id='xray_capture_trigger', status=False, device_actions={})
            DistanceScheduler()._activate_devices(threshold, 26.)
        laz.assert_called_once_with(generation)
        camera.assert_called_once()

    def test_real_local_http_laz_to_authenticated_cached_api(self):
        payload = cloud_bytes()
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Capture-Finish', 'true')
                self.end_headers(); self.wfile.write(payload)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            import config
            generation = self.arm()
            with patch.object(config, 'RADAR_POINT_CLOUD_URL', f'http://127.0.0.1:{server.server_port}/geom/laz'):
                capture._poll_laz(generation, capture._stop)
            app = Flask(__name__); app.register_blueprint(device_api)
            auth = {'Authorization': 'Bearer ' + create_token({'username': 'laz-test'})}
            result = app.test_client().post('/api/device/body-radar-image', json={'view': 'top'}, headers=auth)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json['data']['capture']['state'], 'finished')
            self.assertEqual(result.json['data']['geometry']['totalPointCount'], 4)
            self.assertEqual(calls, ['/geom/laz'])
            self.assertEqual([call.args[0] for call in capture.send_geomcap.call_args_list], [True, False])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_json_and_header_completion_match_body_camera_including_status_false(self):
        from app.services.body_camera_fetch import _is_capture_finished, POLL_INTERVAL_SEC, POLL_TIMEOUT, MAX_POLL_SEC
        cases = [response(b'{"status":"false"}'), response(b'{"status":"true"}'),
                 response(b'[]'), response(b'', 'true'), response(b'', '1'),
                 response(b'', 'yes'), response(b'', 'on'), response(b'error', status=500)]
        json_type = response(b'not json');json_type.headers['Content-Type'] = 'application/json'
        cases.append(json_type)
        for item in cases:
            finished, _ = _is_capture_finished(item)
            if not finished:continue
            with self.subTest(body=item.content, headers=item.headers):
                capture.send_geomcap.reset_mock()
                fetch = self.run_responses([item])
                fetch.assert_called_once()
                self.assertEqual(capture.get_geometry_snapshot()['capture']['state'], 'finished')
                self.assertEqual([call.args[0] for call in capture.send_geomcap.call_args_list], [True, False])
        self.assertEqual(self.poll_interval, POLL_INTERVAL_SEC)
        self.assertEqual(capture.POLL_TIMEOUT, POLL_TIMEOUT)
        self.assertEqual(capture.MAX_POLL_SEC, MAX_POLL_SEC)

    def test_json_completion_preserves_previous_cloud_and_stops(self):
        self.run_responses([response(cloud_bytes()), response(b'{"status":"false"}')])
        result = capture.get_geometry_snapshot()
        self.assertTrue(result['capture']['hasData'])
        self.assertEqual(result['capture']['state'], 'finished')
        self.assertEqual(result['geometry']['totalPointCount'], 4)

    def test_stale_poll_finalizer_never_sends_stop_to_new_vehicle(self):
        generation = self.arm()
        old_stop = capture._stop
        def next_vehicle(*args, **kwargs):
            current = capture.prepare_geometry_capture()
            capture.start_geometry_capture(current)
            return response(cloud_bytes(), 'true')
        with patch.object(capture, 'middleware_get_payload', side_effect=next_vehicle):
            capture._poll_laz(generation, old_stop)
        self.assertEqual([call.args[0] for call in capture.send_geomcap.call_args_list], [True, True])
        self.assertEqual(capture.get_geometry_snapshot()['capture']['state'], 'capturing')


if __name__ == '__main__': unittest.main()
