"""手动补读真实LAZ、持久恢复及迟到响应隔离；不发设备命令。"""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from flask import Flask
from sqlalchemy import create_engine
from requests import Response
from app.api.device import device_api
from app.extensions.auth import create_token
from app.services import geometry_capture as capture, body_radar_projection as projection
from app.services.inspection_session import InspectionSessionStore, configure_store
from app.services.capture_guard import CaptureBlockedError
from test_radar_point_cloud import cloud_bytes


class GeometryRefreshTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.engine = create_engine('sqlite:///' + str(Path(temp.name) / 'session.db'))
        self.addCleanup(self.engine.dispose)
        self.store = InspectionSessionStore(self.engine, Path(temp.name) / 'images')
        self.store.initialize()
        self.store.record('booking_accepted', {})
        configure_store(self.store)
        self.addCleanup(lambda: configure_store(None))
        self.session_id = self.store.snapshot()['sessionId']
        self.headers = {'Authorization': 'Bearer ' + create_token({'username': 'refresh-test'}), 'X-Inspection-Session': self.session_id}
        app = Flask(__name__); app.register_blueprint(device_api)
        self.client = app.test_client()
        self.response = Response(); self.response.status_code = 200
        self.response._content = cloud_bytes(); self.response.headers['Capture-Finish'] = 'true'
        for item in [patch.object(capture, '_generation', 5), patch.object(capture, '_frame', 0),
                     patch.object(capture, '_armed', False), patch.object(capture, '_started', False),
                     patch.object(capture, '_poll_requested', False), patch.object(capture, '_poll_started', False),
                     patch.object(capture, '_result', None), patch.object(capture, '_state', 'finished'),
                     patch.object(capture, '_error', '采集已结束，但未返回有效点云'),
                     patch.object(capture, '_stop', threading.Event()), patch.object(capture, '_notify'),
                     patch.object(capture, 'send_geomcap', side_effect=AssertionError('禁止设备命令')),
                     patch.object(capture, 'ensure_capture_allowed', return_value=7),
                     patch.object(projection, '_run_in_pool', side_effect=lambda fn: fn()),
                     patch('app.services.inspection_session.broadcast'),
                     patch('socket.socket', side_effect=AssertionError('禁止网络'))]:
            item.start(); self.addCleanup(item.stop)

    def refresh(self, **body):
        return self.client.post('/api/device/body-radar-image', json={'view': 'top', 'refresh': True, **body}, headers=self.headers)

    def test_refresh_decodes_and_persists_same_session_without_commands(self):
        with patch.object(capture, 'middleware_get_payload', return_value=self.response) as fetch:
            result = self.refresh()
        self.assertEqual(result.status_code, 200)
        fetch.assert_called_once()
        self.assertEqual(result.json['data']['geometry']['totalPointCount'], 4)
        snapshot = InspectionSessionStore(self.engine, self.store.image_root).snapshot()
        self.assertEqual(snapshot['sessionId'], self.session_id)
        self.assertTrue(snapshot['geometryCapture']['hasData'])
        self.assertEqual(snapshot['geometryCapture']['error'], '')
        saved = self.store.image_root / snapshot['geometryUrl'].split('/api/images/')[1]
        doc = json.loads(saved.read_text())
        self.assertEqual(doc['geometry']['totalPointCount'], 4)
        for view in ('top', 'side'):
            self.assertTrue((self.store.image_root / doc['views'][view]['imageUrl'].split('/api/images/')[1]).is_file())

    def test_cache_read_and_invalid_options_never_fetch(self):
        with patch.object(capture, 'middleware_get_payload') as fetch:
            self.assertEqual(self.refresh(refresh=False).status_code, 200)
            self.assertEqual(self.refresh(refresh='true').status_code, 400)
            self.headers['X-Inspection-Session'] = 'old-session'
            self.assertEqual(self.refresh().status_code, 409)
        fetch.assert_not_called()

    def test_empty_response_keeps_previous_valid_cloud(self):
        with patch.object(capture, 'middleware_get_payload', return_value=self.response):
            self.assertEqual(self.refresh().status_code, 200)
        previous = self.store.snapshot()['geometryUrl']
        self.response._content = b'{"status":false}'
        with patch.object(capture, 'middleware_get_payload', return_value=self.response):
            self.assertEqual(self.refresh().status_code, 502)
        self.assertEqual(self.store.snapshot()['geometryUrl'], previous)
        self.assertTrue(capture.get_geometry_snapshot()['capture']['hasData'])

    def test_session_reset_during_request_discards_result(self):
        def fetch(*a, **kw):
            self.store.record('booking_reset', {})
            self.store.record('booking_accepted', {})
            return self.response
        with patch.object(capture, 'middleware_get_payload', side_effect=fetch):
            self.assertEqual(self.refresh().status_code, 409)
        self.assertEqual(self.store.snapshot()['geometryUrl'], '')
        self.assertFalse(capture.get_geometry_snapshot()['capture']['hasData'])

    def test_capture_generation_change_during_request_discards_result(self):
        def fetch(*a, **kw):
            capture.prepare_geometry_capture()
            return self.response
        with patch.object(capture, 'middleware_get_payload', side_effect=fetch):
            self.assertEqual(self.refresh().status_code, 409)
        self.assertEqual(self.store.snapshot()['geometryUrl'], '')

    def test_lane_change_during_request_discards_result(self):
        with patch.object(capture, 'middleware_get_payload', return_value=self.response), \
             patch.object(capture, 'ensure_capture_allowed', side_effect=[7, CaptureBlockedError('占用')]):
            self.assertEqual(self.refresh().status_code, 409)
        self.assertEqual(self.store.snapshot()['geometryUrl'], '')


if __name__ == '__main__': unittest.main()
