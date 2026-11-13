"""自动抓拍路由与前后 LED 新协议；网络和推送均隔离。"""
import json
import unittest
from unittest.mock import patch, call

from app.devices import led, mcast_cmd
from app.services import capture_service as capture


class CaptureMappingTest(unittest.TestCase):
    def test_capture_categories_route_to_distinct_cameras(self):
        with patch.object(capture, '_load_camera_row', return_value={'config': {}}), \
             patch.object(capture, 'snapshot_from_camera', return_value=b'\xff\xd8test') as snapshot, \
             patch('app.services.temp_image_cache.put_and_url', return_value='/test.jpg'), \
             patch.object(capture, '_push_image_ready') as push, \
             patch('socket.socket', side_effect=AssertionError('network forbidden')):
            for category, device in [('head', 'cam_head'), ('tail', 'cam_lane'), ('top', 'cam_road')]:
                capture.capture_category(category)
                if category == 'top':
                    snapshot.assert_called_with(device, right_two_thirds=True)
                else:
                    snapshot.assert_called_with(device)
                push.assert_called_with(category, '/test.jpg')

    def test_only_category_specific_override_applies_including_json_config(self):
        config = {'head_device_id': 'custom', 'body_capture_device_id': 'obsolete_shared'}
        for value in (config, json.dumps(config)):
            with patch.object(capture, '_load_camera_row', return_value={'config': value}), \
                 patch.object(capture, 'snapshot_from_camera', return_value=b'\xff\xd8test') as snapshot, \
                 patch('app.services.temp_image_cache.put_and_url', return_value='/test.jpg'), \
                 patch.object(capture, '_push_image_ready'):
                capture.capture_category('head'); snapshot.assert_called_with('custom')
                capture.capture_category('tail'); snapshot.assert_called_with('cam_lane')


class LedMappingTest(unittest.TestCase):
    def setUp(self):
        self.led = led.LedController('test')
        sender_patch = patch.object(mcast_cmd, 'publish_mdrmc', return_value=True)
        self.sender = sender_patch.start()
        self.addCleanup(sender_patch.stop)
        socket_patch = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        socket_patch.start()
        self.addCleanup(socket_patch.stop)

    def test_all_business_steps_use_two_new_commands_with_confirmed_mapping(self):
        for step, number in [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 5), (7, 4)]:
            self.sender.reset_mock()
            self.assertTrue(self.led.execute_action(f'set_step{step}'))
            self.assertEqual(self.sender.call_args_list, [
                call(f'$MDRMC,1,frontleddisplay=DISPLAY{number}'),
                call(f'$MDRMC,1,backleddisplay=DISPLAY{number}'),
            ])

    def test_legacy_cancel_and_maintenance_display(self):
        self.assertTrue(self.led.execute_action('set_display', {'code': 'SQYQX_QTX'}))
        self.assertIn('DISPLAY4', self.sender.call_args.args[0])
        self.assertTrue(self.led.set_display('DISPLAY6'))
        self.assertIn('DISPLAY6', self.sender.call_args.args[0])

    def test_partial_failure_still_attempts_other_screen_and_reports_failure(self):
        self.sender.side_effect = [False, True]
        self.assertFalse(self.led.set_display('DISPLAY1'))
        self.assertEqual(self.sender.call_count, 2)
        self.assertIn('frontleddisplay', self.led.last_error)

    def test_invalid_or_injected_code_never_sends(self):
        for code in ['', 'DISPLAY0', 'DISPLAY10', 'unknown', 'DISPLAY1,xraysource200=true']:
            self.assertFalse(self.led.set_display(code))
        self.sender.assert_not_called()


if __name__ == '__main__':
    unittest.main()
