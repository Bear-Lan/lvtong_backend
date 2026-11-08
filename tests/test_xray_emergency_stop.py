"""急停只提交停止X光命令；不触发其他设备或停止其他业务。"""
import unittest
from unittest.mock import patch, Mock

from flask import Flask

from app.api import booking, device
from app.extensions.auth import create_token
from app.devices.plc import params_to_mdrmc_cmds, PLCController


class XrayEmergencyStopTest(unittest.TestCase):
    def test_plc_emergency_button_uses_xray_stop_command(self):
        self.assertEqual(params_to_mdrmc_cmds({'urgentstop': True}), ['xraycap=false'])

    def test_plc_control_uses_xray_stop_command(self):
        with patch('app.devices.mcast_cmd.publish_mdrmc', return_value=True) as send:
            self.assertTrue(PLCController('isolated-stop').execute_action('setPLC', {'urgentstop': True}))
        send.assert_called_once_with('$MDRMC,1,xraycap=false')

    def setUp(self):
        original = dict(booking.get_booking_state())
        self.addCleanup(lambda: (booking.get_booking_state().clear(), booking.get_booking_state().update(original)))
        app = Flask(__name__)
        app.register_blueprint(booking.booking_api)
        self.client = app.test_client()
        self.headers = {'Authorization': 'Bearer ' + create_token({'username': 'stop-test'})}

    def test_stop_only_sends_xray_false_and_preserves_detection_state(self):
        state = dict(booking.get_booking_state())
        self.addCleanup(lambda: booking.get_booking_state().update(state))
        booking.set_booking_state(is_detection=True, check_step=1)
        before = dict(booking.get_booking_state())
        with patch('app.devices.mcast_cmd.publish_mdrmc', return_value=True) as send, \
             patch('app.services.device_action.execute_device_action') as action, \
             patch('app.services.progressive_image_fetch.stop_progressive_panel_fetch') as stop_poll, \
             patch('socket.socket', side_effect=AssertionError('network forbidden')):
            response = self.client.post('/api/booking/urgent-stop', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        send.assert_called_once_with('$MDRMC,1,xraycap=false')
        action.assert_not_called()
        stop_poll.assert_not_called()
        self.assertEqual(booking.get_booking_state(), before)
        self.assertTrue(response.json['data']['commandSent'])

    def test_send_failure_is_not_reported_as_stopped(self):
        for failure in (False, OSError('offline')):
            with self.subTest(failure=str(failure)):
                kwargs = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
                with patch('app.devices.xray.close_xray_capture', **kwargs):
                    response = self.client.post('/api/booking/urgent-stop', headers=self.headers)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json['code'], 503)

    def test_stop_requires_login(self):
        with patch('app.devices.xray.close_xray_capture') as stop:
            response = self.client.post('/api/booking/urgent-stop')
        self.assertEqual(response.status_code, 401)
        stop.assert_not_called()

    def test_reset_retains_existing_commands_without_faking_feedback_or_restarting_xray(self):
        with patch('app.services.device_action.execute_device_action', return_value=True) as action, \
             patch('app.devices.mcast_cmd.publish_mdrmc', return_value=True) as send, \
             patch('app.websocket.handler.socketio.emit') as emit:
            response = self.client.post('/api/booking/stop-reset', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        action.assert_called_once_with('plc_001', 'setPLC', {'urgentstop': False})
        send.assert_called_once_with('$MDRMC,1,xraycap=false')
        emit.assert_not_called()

    def test_manual_gate_open_does_not_independently_change_led(self):
        app = Flask(__name__)
        app.register_blueprint(device.device_api)
        gate = Mock(status=1, device_type='gate', is_opened=True, last_boom_result={})
        gate.execute_action.return_value = True
        with patch.object(device, 'DeviceManager') as manager, \
             patch('app.services.device_action.execute_device_action') as action, \
             patch('app.devices.xray.close_xray_capture', return_value=True) as stop:
            manager.return_value.get_device.return_value = gate
            response = app.test_client().post('/api/device/gate_001/control',
                                            json={'action': 'open'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        gate.execute_action.assert_called_once_with('open', {})
        stop.assert_called_once()
        action.assert_not_called()

