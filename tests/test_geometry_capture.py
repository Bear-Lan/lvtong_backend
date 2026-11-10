"""几何采集生命周期隔离验证；全部命令拦截，不连接设备。"""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from flask import Flask

from app.services import geometry_capture as geometry
from app.api import booking
from app.services.scheduler import DistanceScheduler
from app.extensions.auth import create_token
from app.devices.led import LedController
from app.devices import mcast_cmd


class GeometryCaptureTest(unittest.TestCase):
    def setUp(self):
        original = dict(booking.get_booking_state())
        self.addCleanup(lambda: (booking.get_booking_state().clear(), booking.get_booking_state().update(original)))
        for name, value in (('_generation', 0), ('_armed', False), ('_started', False),
                            ('_poll_started', False), ('_poll_requested', False)):
            item = patch.object(geometry, name, value)
            item.start()
            self.addCleanup(item.stop)
        item = patch.object(geometry, '_notify')
        item.start()
        self.addCleanup(item.stop)

    def test_accept_only_arms_release_starts_cancel_stops(self):
        with patch.object(geometry, 'send_geomcap', return_value=True) as send:
            generation = geometry.prepare_geometry_capture()
            send.assert_not_called()
            self.assertTrue(geometry.start_geometry_capture(generation))
            self.assertTrue(geometry.stop_geometry_capture())
            self.assertFalse(geometry.start_geometry_capture(generation))
        self.assertEqual([call.args[0] for call in send.call_args_list], [True, False])

    def test_previous_vehicle_cannot_start_new_capture_and_send_failure_propagates(self):
        with patch.object(geometry, 'send_geomcap', return_value=False) as send:
            old = geometry.prepare_geometry_capture()
            current = geometry.prepare_geometry_capture()
            self.assertFalse(geometry.start_geometry_capture(old))
            send.assert_not_called()
            self.assertFalse(geometry.start_geometry_capture(current))
            self.assertFalse(geometry.stop_geometry_capture())

    def test_release_starts_geometry_together_with_camera_without_real_xray(self):
        generation = geometry.prepare_geometry_capture()
        booking.set_booking_state(is_detection=True, check_step=1, geometry_generation=generation)
        scheduler = DistanceScheduler()
        with patch.object(scheduler, '_is_detection', True), \
             patch.object(scheduler, '_gate_released', False), \
             patch.object(scheduler, '_release_clear_count', 0), \
             patch('app.services.scheduler.threading.Thread', side_effect=lambda target, **kw: SimpleNamespace(start=target)), \
             patch.object(geometry, 'send_geomcap', return_value=True) as send, \
             patch('app.services.device_action.execute_device_action', return_value=True), \
             patch('app.services.body_camera_fetch.trigger_body_camera_capture') as camera, \
             patch.object(scheduler, '_trigger_xray_start_scan') as xray, \
             patch('app.services.scheduler.push_detection_step'), \
             patch('app.services.scheduler.push_lane_occupied'), \
             patch('socket.socket', side_effect=AssertionError('network forbidden')):
            for _ in range(5):
                scheduler._try_release_after_accept(1, 40, intrusion=False)
        send.assert_called_once_with(True)
        camera.assert_called_once()
        xray.assert_called_once()

    def test_reject_and_reset_stop_geometry_but_emergency_does_not(self):
        app = Flask(__name__)
        app.register_blueprint(booking.booking_api)
        headers = {'Authorization': 'Bearer ' + create_token({'username': 'geometry-test'})}
        for route in ('reject', 'reset', 'urgent-stop'):
            with patch.object(geometry, 'send_geomcap', return_value=True) as send, \
                 patch('app.services.device_action.execute_device_action', return_value=True), \
                 patch('app.services.lane_announce.get_lane_announce'), \
                 patch('app.services.command_announce.stop_speak'), \
                 patch('app.services.scheduler.DistanceScheduler'), \
                 patch('app.services.progressive_image_fetch.stop_progressive_panel_fetch'), \
                 patch('app.services.body_camera_fetch.stop_body_camera_fetch'), \
                 patch('app.devices.xray.close_xray_capture', return_value=True), \
                 patch('app.websocket.handler.socketio.emit'), \
                 patch('socket.socket', side_effect=AssertionError('network forbidden')):
                response = app.test_client().post('/api/booking/' + route, headers=headers)
            self.assertEqual(response.status_code, 200)
            if route == 'urgent-stop':
                send.assert_not_called()
            else:
                send.assert_called_once_with(False)

    def test_reject_returns_both_leds_to_idle_without_changing_gate_or_light_actions(self):
        app = Flask(__name__)
        app.register_blueprint(booking.booking_api)
        headers = {'Authorization': 'Bearer ' + create_token({'username': 'reject-test'})}
        booking.mark_book_button()
        booking.set_booking_state(booking_dialog_shown=True)
        led = LedController('test-led')

        def execute(device_id, action, params=None):
            return led.execute_action(action, params) if device_id == 'led_001' else True

        with patch.object(geometry, 'send_geomcap', return_value=True), \
             patch('app.services.device_action.execute_device_action', side_effect=execute) as actions, \
             patch.object(mcast_cmd, 'publish_mdrmc', return_value=True) as send, \
             patch('app.services.lane_announce.get_lane_announce'), \
             patch('app.services.command_announce.stop_speak'), \
             patch('app.services.scheduler.DistanceScheduler'), \
             patch('app.services.progressive_image_fetch.stop_progressive_panel_fetch'), \
             patch('app.services.body_camera_fetch.stop_body_camera_fetch'), \
             patch('app.websocket.handler.socketio.emit'), \
             patch('socket.socket', side_effect=AssertionError('network forbidden')):
            response = app.test_client().post('/api/booking/reject', headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([c.args[0] for c in send.call_args_list], [
            '$MDRMC,1,frontleddisplay=DISPLAY1', '$MDRMC,1,backleddisplay=DISPLAY1'])
        actions.assert_any_call('plc_001', 'setPLC', {
            'redlight': False, 'yellowlight': True, 'greenlight': False})
        actions.assert_any_call('gate_001', 'open')
        self.assertFalse(booking.get_booking_state()['btn_prebook_state'])
        self.assertFalse(booking.get_booking_state()['booking_dialog_shown'])
        self.assertEqual(booking.get_booking_state()['check_step'], 0)
