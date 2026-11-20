"""主子码流隔离及手动拍照边界；禁止连接真实设备。"""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
import yaml
from app.api import capture as api
from app.extensions.auth import create_token
from app.services import capture_service as capture, mediamtx_launcher as mtx
from app.services.capture_guard import CaptureBlockedError
from app.services.video_transcode import build_transcode_config


class CameraStreamSplitTest(unittest.TestCase):
    def test_same_video_channel_main_for_photo_sub_for_preview(self):
        for cfg, channel in [({}, 1), ({'channelId': 3, 'streamType': 2}, 3),
                             ({'channel_id': 2}, 2), ({'rtsp_channel': 202}, 2),
                             ({'rtspChannel': '301'}, 3), ({'rtsp_channel': 2}, 1),
                             ('{"channelId": 4}', 4), ([], 1),
                             ({'channelId': 'bad', 'streamType': 2}, 1)]:
            with self.subTest(cfg=cfg), patch.dict('os.environ', {}, clear=True):
                row = {'ip_address': 'test.invalid', 'config': cfg}
                self.assertEqual(capture._resolve_rtsp_channel(row), channel * 100 + 1)
                self.assertTrue(mtx.build_hik_rtsp_from_device(row, stream_type=2).endswith(f'/{channel * 100 + 2}'))

    def test_legacy_environment_cannot_turn_photo_into_substream(self):
        with patch.dict('os.environ', {'HIK_RTSP_CHANNEL': '202'}):
            self.assertEqual(capture._resolve_rtsp_channel({}), 201)

    def test_all_six_preview_paths_use_substream_even_with_legacy_main_setting(self):
        with patch.object(mtx, '_load_device_by_device_id', return_value={'ip_address': 'test.invalid'}), \
             patch.object(mtx, '_write_path_source', return_value=True) as write, \
             patch.dict('os.environ', {'LIVE_VIDEO_STREAM_TYPE': '1'}):
            for n in (1, 2, 3, 4, 5, 6):
                mtx._sync_path_from_device_id(f'cam{n}', 'test')
                self.assertTrue(write.call_args.args[1].endswith('/102'))
            mtx._sync_path_from_device_id('cam7', 'test')
            self.assertTrue(write.call_args.args[1].endswith('/101'))

    def test_perimeter_left_substream_preserves_video_channel_and_photo_mainstream(self):
        row = {'ip_address': 'test.invalid', 'config': {'rtsp_channel': 301}}
        with patch.object(mtx, '_load_device_by_device_id', return_value=row), \
             patch.object(mtx, '_write_path_source', return_value=True) as write, \
             patch.dict('os.environ', {'LIVE_VIDEO_STREAM_TYPE': '1'}):
            mtx.sync_cam3_source_from_db()
            self.assertEqual(write.call_args.args[0], 'cam3')
            self.assertTrue(write.call_args.args[1].endswith('/302'))
            self.assertEqual(capture._resolve_rtsp_channel(row), 301)

    def test_perimeter_sides_use_independent_database_rows(self):
        rows = {'cam_safe': {'ip_address': '192.0.2.10'},
                'cam_safe_right': {'ip_address': '192.0.2.11'}}
        with patch.object(mtx, '_load_device_by_device_id', side_effect=rows.get), \
             patch.object(mtx, '_write_path_source', return_value=True) as write, \
             patch.dict('os.environ', {}, clear=True):
            mtx.sync_cam3_source_from_db()
            mtx.sync_cam7_source_from_db()
            calls = write.call_args_list
            self.assertEqual([c.args[0] for c in calls], ['cam3', 'cam7'])
            self.assertIn('@192.0.2.10:', calls[0].args[1])
            self.assertIn('@192.0.2.11:', calls[1].args[1])
            self.assertTrue(calls[0].args[1].endswith('/102'))
            self.assertTrue(calls[1].args[1].endswith('/101'))

    def test_left_sync_changes_only_left_preview_in_generated_runtime_config(self):
        base = {'rtspAddress': ':8554', 'paths': {
            f'cam{i}': {'source': f'rtsp://preview{i}.invalid/Streaming/Channels/101',
                       'rtspTransport': 'tcp'} for i in range(1, 8)}}
        row = {'ip_address': 'left.invalid', 'config': {'channelId': 1}}
        with tempfile.TemporaryDirectory() as dirname:
            config = Path(dirname) / 'mediamtx.yml'
            config.write_text(yaml.safe_dump(base), encoding='utf8')
            with patch.object(mtx, '_cfg_path', return_value=config), \
                 patch.object(mtx, '_load_device_by_device_id', return_value=row), \
                 patch.dict('os.environ', {'LIVE_VIDEO_STREAM_TYPE': '1'}):
                mtx.sync_cam3_source_from_db()
            after = yaml.safe_load(config.read_text(encoding='utf8'))
        expected = build_transcode_config(base, 'ffmpeg', 'libx264')
        actual = build_transcode_config(after, 'ffmpeg', 'libx264')
        left = actual['paths']['cam3_input']['source']
        self.assertTrue(left.endswith('/102'))
        expected['paths']['cam3_input']['source'] = left
        self.assertEqual(actual, expected)

    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(api.capture_api)
        self.client = app.test_client()
        self.headers = {'Authorization': 'Bearer ' + create_token({'username': 'capture-test'})}

    def post(self, device, **kwargs):
        return self.client.post('/api/capture/camera/' + device, headers=self.headers, **kwargs)

    def test_selected_camera_is_independent_of_photo_category_and_roof_crop_is_preserved(self):
        with patch.object(api, 'ensure_manual_capture_allowed', return_value='session') as guard, \
             patch.object(api, 'snapshot_from_camera', return_value=b'\xff\xd8photo') as shot, \
             patch.object(capture, '_push_image_ready') as broadcast:
            for device in ('cam_road', 'cam_head', 'cam_lane', 'cam_ptz', 'cam_booking'):
                response = self.post(device)
                self.assertEqual(response.status_code, 200)
                result = response.json['data']
                self.assertEqual(base64.b64decode(result['dataUrl'].split(',')[1]), b'\xff\xd8photo')
                self.assertEqual(result['deviceId'], device)
                self.assertEqual(result['streamType'], 1)
                shot.assert_called_with(device, right_two_thirds=device == 'cam_road', manual=True)
                guard.assert_called_with('session')
            broadcast.assert_not_called()

    def test_auth_allowlist_and_old_session_block_before_camera_io(self):
        with patch.object(api, 'snapshot_from_camera') as shot:
            self.assertEqual(self.client.post('/api/capture/camera/cam_head').status_code, 401)
            self.assertEqual(self.post('cam_safe').status_code, 400)
            with patch.object(api, 'ensure_manual_capture_allowed', side_effect=CaptureBlockedError('session changed')):
                self.assertEqual(self.post('cam_head').status_code, 409)
            shot.assert_not_called()

    def test_late_photo_is_discarded_and_connection_errors_do_not_return_preview(self):
        with patch.object(api, 'ensure_manual_capture_allowed', side_effect=['session', CaptureBlockedError('changed')]), \
             patch.object(api, 'snapshot_from_camera', return_value=b'\xff\xd8late'):
            self.assertEqual(self.post('cam_head').status_code, 409)
        with patch.object(api, 'ensure_manual_capture_allowed', return_value='session'), \
             patch.object(api, 'snapshot_from_camera', side_effect=RuntimeError('secret connection')):
            response = self.post('cam_head')
            self.assertEqual(response.status_code, 502)
            self.assertNotIn('secret', json.dumps(response.json))
            self.assertIsNone(response.json['data'])
