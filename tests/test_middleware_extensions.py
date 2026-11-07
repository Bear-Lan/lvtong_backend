"""补充状态位、辐射计和几何采集命令；不打开网络套接字。"""
import unittest
from unittest.mock import Mock, patch
from flask import Flask

from app.devices import mcast_cmd
from app.devices.radar import RadarReader
from app.websocket import handler
from app.api import device
from app.extensions.auth import create_token


class MiddlewareExtensionsTest(unittest.TestCase):
    def test_gate_online_uses_bit6_and_feedback_age_not_local_driver(self):
        app = Flask(__name__)
        app.register_blueprint(device.device_api)
        headers = {'Authorization': 'Bearer ' + create_token({'username': 'gate-feedback-test'})}
        for fresh, online, expected in ((True, 0, False), (True, 1 << 6, True), (False, 1 << 6, False)):
            data = RadarReader.parse_ntrmc_dev(f'$NTRMC,DEVICE,00000040,{online:08x}')
            with patch.object(device, 'DeviceManager') as manager, \
                 patch.object(handler, 'get_last_plc_status', return_value={**data, 'fresh': fresh, 'stickdown': True}):
                manager.return_value.get_device.return_value.is_online = True
                manager.return_value.get_device.return_value.last_error = ''
                response = app.test_client().get('/api/device/gate-status', headers=headers)
            self.assertIs(response.json['data']['connected'], expected)
            self.assertTrue(response.json['data']['gateOpen'])

    def test_sensor_api_separates_online_and_action_and_hides_bit20(self):
        app = Flask(__name__)
        app.register_blueprint(device.device_api)
        token = create_token({'username': 'feedback-test'})
        parsed = RadarReader.parse_ntrmc_dev('$NTRMC,DEVICE,00040000,001fff7f')
        with patch.object(device, 'DeviceManager') as manager, \
             patch.object(handler, 'get_last_plc_status', return_value={**parsed, 'fresh': True}):
            manager.return_value.get_all_status.return_value = []
            manager.return_value.online_count.return_value = 0
            manager.return_value.offline_count.return_value = 0
            result = app.test_client().get('/api/device/status', headers={'Authorization': 'Bearer ' + token})
        rows = result.json['data']['sensors']
        self.assertEqual(len(rows), 20)
        self.assertFalse(rows[7]['online'])
        self.assertFalse(rows[7]['state'])
        self.assertTrue(rows[8]['online'])
        self.assertFalse(rows[8]['state'])
        self.assertTrue(rows[18]['state'])
        with patch.object(device, 'DeviceManager') as manager, \
             patch.object(handler, 'get_last_plc_status', return_value={**parsed, 'fresh': False}):
            manager.return_value.get_all_status.return_value = []
            manager.return_value.online_count.return_value = 0
            manager.return_value.offline_count.return_value = 0
            result = app.test_client().get('/api/device/status', headers={'Authorization': 'Bearer ' + token})
        self.assertTrue(all(row['online'] is None and row['state'] is None for row in result.json['data']['sensors']))

    def test_online_word_never_changes_action_states(self):
        for state in (0, 0x80c2, 1 << 8, 1 << 9, (1 << 20) - 1):
            expected = RadarReader.parse_dev_bits(state)
            for online in (0, 0x1fffff, 0x1fff7f, 1 << 8):
                parsed = RadarReader.parse_ntrmc_dev(f'$NTRMC,DEVICE,{state:08x},{online:08x}')
                for key, value in expected.items():
                    if key != 'raw':
                        self.assertEqual(parsed[key], value, (state, online, key))
                for sensor in parsed['sensors']:
                    self.assertIs(sensor['online'], bool(online & (1 << sensor['bit'])))
                self.assertEqual(len(parsed['sensors']), 20)

    def test_single_word_has_unknown_online_not_false_offline(self):
        parsed = RadarReader.parse_ntrmc_dev('$NTRMC,DEVICE,00008081')
        self.assertTrue(all(row['online'] is None for row in parsed['sensors']))
        self.assertIsNone(parsed['onlineRaw'])

    def test_feedback_expires_and_unchanged_packets_refresh_once_per_second(self):
        radar = RadarReader('isolated-device')
        radar.set_plc_status_callback(handler.push_plc_status)
        with patch.object(handler, '_last_plc_data', None), \
             patch.object(handler, '_last_plc_time', 0), \
             patch('time.monotonic', return_value=100) as clock, \
             patch.object(handler.socketio, 'emit') as emit:
            radar._dispatch('$NTRMC,DEVICE,00008081,001fffff')
            self.assertTrue(handler.get_last_plc_status()['fresh'])
            clock.return_value = 100.5
            radar._dispatch('$NTRMC,DEVICE,00008081,001fffff')
            self.assertEqual(emit.call_count, 1)
            clock.return_value = 101.1
            radar._dispatch('$NTRMC,DEVICE,00008081,001fffff')
            self.assertEqual(emit.call_count, 2)
            clock.return_value = 107
            self.assertFalse(handler.get_last_plc_status()['fresh'])

    def test_geometry_capture_commands_and_send_failure(self):
        with patch.object(mcast_cmd, 'publish_mdrmc', side_effect=[True, False]) as publish:
            self.assertTrue(mcast_cmd.send_geomcap(True))
            self.assertFalse(mcast_cmd.send_geomcap(False))
        self.assertEqual([call.args[0] for call in publish.call_args_list],
                         ['$MDRMC,1,geomcap=true', '$MDRMC,1,geomcap=false'])

    def test_led_and_radiometer_bits_do_not_overlap_other_signals(self):
        fields = ['frontLedDisplayStatus', 'backLedDisplayStatus']
        for bit, field in enumerate(fields, 18):
            parsed = RadarReader.parse_dev_bits(1 << bit)
            self.assertEqual([key for key, value in parsed.items() if value is True], [field])

    def test_radiometer_zero_and_finite_values_are_forwarded(self):
        radar = RadarReader('isolated-radiometer')
        callback = Mock()
        radar.set_radiometer_callback(callback)
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            for text in ('$NTRMC,RADIOMETER,0', '$NTRMC,RADIOMETER,0.05', '$NTRMC,RADIOMETER,1.2'):
                radar._dispatch(text)
        self.assertEqual([call.args[0] for call in callback.call_args_list], [0, .05, 1.2])

    def test_invalid_radiometer_values_are_ignored(self):
        for text in ('$NTRMC,RADIOMETER,nan', '$NTRMC,RADIOMETER,inf',
                     '$NTRMC,RADIOMETER,', '$NTRMC,RADIOMETER,1,2', '$NTRMC,DM200,1'):
            self.assertIsNone(RadarReader.parse_radiometer(text))

    def test_radiometer_callback_reaches_cache_and_expires_without_inventing_units(self):
        radar = RadarReader('isolated-radiometer')
        radar.set_radiometer_callback(handler.push_radiometer_status)
        with patch.object(handler, '_last_radiometer_data', None), \
             patch.object(handler, '_last_radiometer_time', 0), \
             patch.object(handler.time, 'monotonic', return_value=100) as clock, \
             patch.object(handler.socketio, 'emit') as emit:
            self.assertIsNone(handler.get_last_radiometer_status())
            radar._dispatch('$NTRMC,RADIOMETER,0.05')
            self.assertEqual(emit.call_args.args[1]['type'], 'radiometer_status')
            data = handler.get_last_radiometer_status()
            self.assertEqual(data['value'], .05)
            self.assertIsNone(data['unit'])
            self.assertTrue(data['fresh'])
            clock.return_value = 111
            self.assertFalse(handler.get_last_radiometer_status()['fresh'])
