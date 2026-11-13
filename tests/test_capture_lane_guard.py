"""占用时禁止自动/手动抓拍和车身拉图；测试不连接数据库、相机或中间件。"""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask

from app.api.capture import capture_api
from app.extensions.auth import create_token
from app.services import body_camera_fetch as body
from app.services import capture_service as capture
from app.services.capture_guard import CaptureBlockedError, ensure_capture_allowed
from app.services.device_action import execute_device_action
from app.services.scheduler import DistanceScheduler


class CaptureLaneGuardTest(unittest.TestCase):
    def setUp(self):
        singleton = patch.object(DistanceScheduler, '_instance', None)
        singleton.start()
        self.addCleanup(singleton.stop)
        self.scheduler = DistanceScheduler()
        config = json.loads((Path(__file__).parents[1] / 'config/scheduler_config.json').read_text(encoding='utf8'))
        self.scheduler.load_configuration_from_json(config)
        self.scheduler.set_detection_state(True)
        self.scheduler.start()
        for item in (
            patch('socket.socket', side_effect=AssertionError('network forbidden')),
            patch('app.api.booking.get_booking_state', return_value={'check_step': 1}),
            patch('app.api.booking.set_booking_state'),
            patch('app.services.scheduler.push_radar_distance'),
            patch('app.services.scheduler.push_lane_occupied'),
            patch('app.services.scheduler.push_detection_step'),
            patch('app.services.scheduler.threading.Thread', side_effect=lambda target, **kw: SimpleNamespace(start=target)),
        ):
            item.start()
            self.addCleanup(item.stop)

    def occupy(self):
        for _ in range(3):
            self.scheduler._try_release_after_accept(1, 20.5)

    def test_first_occupied_frame_prevents_top_photo_before_ui_debounce(self):
        with patch('app.services.device_action.execute_device_action') as actions:
            self.scheduler.on_radar_data(20.5, object_count=1)
            self.assertTrue(self.scheduler.capture_blocked)
            self.assertFalse(self.scheduler._lane_occupied_active)
            for _ in range(2):
                self.scheduler.on_radar_data(20.5, object_count=1)
        self.assertTrue(self.scheduler._lane_occupied_active)
        actions.assert_not_called()
        top = next(t for t in self.scheduler._distance_thresholds if t.id == 'auto_capture_top')
        self.assertFalse(top.status)

    def test_clear_three_frames_allows_deferred_top_once_at_original_distance(self):
        self.occupy()
        with patch('app.services.device_action.execute_device_action', return_value=True) as actions:
            for _ in range(2):
                self.scheduler.on_radar_data(21.5, object_count=1)
            actions.assert_not_called()
            self.scheduler.on_radar_data(21.5, object_count=1)
            actions.assert_called_once_with('camera_001', 'capture', {'category': 'top'})
            self.scheduler.on_radar_data(21.5, object_count=1)
            self.assertEqual(actions.call_count, 1)

    def test_head_body_and_tail_remain_blocked_until_confirmed_clear(self):
        self.occupy()
        self.scheduler._tail_anchor_x = 40
        with patch('app.services.device_action.execute_device_action') as actions, \
             patch.object(body, 'start_body_camera_poll') as poll, \
             patch('app.services.progressive_image_fetch.start_progressive_panel_fetch'):
            for _ in range(2):
                self.scheduler.on_radar_data(27, object_count=1)
            self.assertFalse(any(c.args[1] == 'capture' for c in actions.call_args_list))
            poll.assert_not_called()
            self.assertFalse(self.scheduler._tail_captured)
            self.scheduler.on_radar_data(27, object_count=1)
            actions.assert_any_call('camera_001', 'capture', {'category': 'head'})
            actions.assert_any_call('camera_001', 'capture', {'category': 'tail'})
            poll.assert_called_once()

    def test_other_distance_actions_still_run_while_photos_blocked(self):
        self.occupy()
        with patch('app.services.device_action.execute_device_action') as actions:
            self.scheduler.on_radar_data(8, object_count=1)
        actions.assert_called_once_with('plc_001', 'setPLC', {
            'redlight': False, 'yellowlight': True, 'greenlight': False})

    def test_normal_pass_after_gate_release_still_takes_top_below_21m(self):
        self.scheduler._gate_released = True
        with patch('app.services.device_action.execute_device_action') as actions:
            self.scheduler.on_radar_data(20.5, object_count=1)
        self.assertFalse(self.scheduler.capture_blocked)
        actions.assert_called_once_with('camera_001', 'capture', {'category': 'top'})

    def test_all_capture_categories_and_direct_device_dispatch_block_before_io(self):
        self.occupy()
        with patch.object(capture, '_load_camera_row') as camera, \
             patch.object(capture.cv2, 'VideoCapture') as rtsp, \
             patch.object(capture, 'fetch_url_bytes') as http, \
             patch.object(capture, '_push_image_ready') as push:
            for category in ('head', 'tail', 'top', 'goods', 'license', 'evidence'):
                with self.subTest(category=category), self.assertRaises(CaptureBlockedError):
                    capture.capture_category(category)
                self.assertFalse(execute_device_action('camera_001', 'capture', {'category': category}))
        for mocked in (camera, rtsp, http, push):
            mocked.assert_not_called()

    def test_manual_single_and_bulk_return_409_then_succeed_when_clear(self):
        self.occupy()
        app = Flask(__name__)
        app.register_blueprint(capture_api)
        client = app.test_client()
        headers = {'Authorization': 'Bearer ' + create_token({'username': 'capture-test'})}
        with patch.object(capture, '_load_camera_row', return_value={'config': {}}), \
             patch.object(capture, 'snapshot_from_camera', return_value=b'\xff\xd8test') as snapshot, \
             patch('app.services.temp_image_cache.put_and_url', return_value='/test.jpg'), \
             patch.object(capture, '_push_image_ready'):
            for endpoint in ('head', 'top', 'tail', 'bulk'):
                response = client.post('/api/capture/' + endpoint, headers=headers)
                self.assertEqual(response.status_code, 409)
                self.assertIn('车道占用', response.json['message'])
            snapshot.assert_not_called()
            for _ in range(3):
                self.scheduler._try_release_after_accept(1, 22)
            response = client.post('/api/capture/top', headers=headers)
            self.assertEqual(response.status_code, 200)
            snapshot.assert_called_once_with('cam_road', right_two_thirds=True)

    def test_inflight_photo_discarded_even_if_occupancy_has_already_cleared(self):
        def late_frame(_device):
            self.occupy()
            for _ in range(3):
                self.scheduler._try_release_after_accept(1, 22)
            return b'\xff\xd8late'

        with patch.object(capture, '_load_camera_row', return_value={'config': {}}), \
             patch.object(capture, 'snapshot_from_camera', side_effect=late_frame), \
             patch('app.services.temp_image_cache.put_and_url') as cache, \
             patch.object(capture, '_push_image_ready') as push:
            with self.assertRaises(CaptureBlockedError):
                capture.capture_category('head')
        self.assertFalse(self.scheduler.capture_blocked)
        cache.assert_not_called()
        push.assert_not_called()

    def test_body_start_pull_and_frame_push_are_blocked(self):
        self.occupy()
        # 避免复用其他测试/生产线程的单例状态。
        fetcher = object.__new__(body.BodyCameraFetcher)
        fetcher._stop = Mock()
        fetcher._stop.is_set.return_value = False
        fetcher._stop.wait.return_value = True
        fetcher._frame_count = 0
        with patch.object(body, '_send_camcap') as send, \
             patch('app.services.middleware_http.middleware_get') as get:
            self.assertFalse(fetcher._trigger_capture())
            fetcher.start_poll()  # 必须在创建工作线程前返回
            send.assert_not_called()
            fetcher._poll_images()
            get.assert_not_called()
            send.assert_called_once_with(False)
        cache, preview, push = Mock(), Mock(), Mock()
        fetcher._push_frame(b'ignored', cache, preview, push)
        cache.put_preview_with_original.assert_not_called()
        preview.assert_not_called()
        push.assert_not_called()

    def test_late_body_response_is_discarded_and_poll_resumes_when_clear(self):
        fetcher = object.__new__(body.BodyCameraFetcher)
        fetcher._stop = Mock()
        fetcher._stop.is_set.side_effect = [False, False]
        fetcher._stop.wait.return_value = True
        fetcher._frame_count = 0
        response = SimpleNamespace(status_code=200, headers={'Content-Type': 'image/jpeg'}, content=b'\xff\xd8' + b'a' * 30)

        def late_response(*args, **kwargs):
            self.occupy()
            return response

        with patch.object(body, '_send_camcap'), \
             patch('app.services.middleware_http.middleware_get', side_effect=late_response) as get, \
             patch.object(fetcher, '_push_frame') as push:
            fetcher._poll_images()
            get.assert_called_once()
            push.assert_not_called()
        for _ in range(3):
            self.scheduler._try_release_after_accept(1, 22)
        response.headers['Capture-Finish'] = 'true'
        fetcher._stop.is_set.side_effect = [False]
        with patch.object(body, '_send_camcap'), \
             patch('app.services.middleware_http.middleware_get', return_value=response) as get, \
             patch.object(fetcher, '_push_frame') as push:
            fetcher._poll_images()
            get.assert_called_once()
            push.assert_called_once()

    def test_unknown_does_not_clear_confirmed_occupancy_and_reset_invalidates_old_frame(self):
        generation = ensure_capture_allowed()
        self.occupy()
        self.scheduler._try_release_after_accept(0, 0)
        self.assertTrue(self.scheduler.capture_blocked)
        self.scheduler.stop()
        self.scheduler.start()
        self.assertFalse(self.scheduler.capture_blocked)
        with self.assertRaises(CaptureBlockedError):
            ensure_capture_allowed(generation)


if __name__ == '__main__':
    unittest.main()
