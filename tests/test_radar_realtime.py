"""实时小车位置独立于受理流程；网络和设备动作全部隔离。"""
from contextlib import ExitStack
import unittest
from unittest.mock import patch

from flask import Flask
from flask_socketio import SocketIO

from app.devices.radar import RadarReader
from app.services.scheduler import DistanceScheduler
from app.websocket import handler


class RadarRealtimeTest(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(DistanceScheduler, '_instance', None))
        self.scheduler = DistanceScheduler()
        stack.enter_context(patch.object(handler, '_last_radar_message', None))
        stack.enter_context(patch.object(handler, '_last_radar_time', 0.0))
        stack.enter_context(patch.object(handler, '_radar_push_state', {'t': 0, 'd': None, 'mode': None, 'count': None}))
        stack.enter_context(patch('socket.socket', side_effect=AssertionError('network forbidden')))

    def test_idle_booking_and_stopped_update_distance_without_business_actions(self):
        with patch('app.services.scheduler.push_radar_distance') as push, \
             patch.object(self.scheduler, '_try_release_after_accept') as release, \
             patch.object(self.scheduler, '_try_tail_capture') as tail, \
             patch.object(self.scheduler, '_process_distance_update') as thresholds:
            for distance in (35.4, 28.1, 20.5):
                self.scheduler.on_radar_data(distance, 4, object_count=4)
                push.assert_called_with(distance, 4, object_count=4)
                self.assertEqual(self.scheduler.current_distance, distance)
            self.scheduler.start()
            self.scheduler.stop()
            self.scheduler.on_radar_data(19.2, 1, object_count=1)
            push.assert_called_with(19.2, 1, object_count=1)
        release.assert_not_called()
        tail.assert_not_called()
        thresholds.assert_not_called()

    def test_detection_keeps_original_actions_and_emits_only_one_position(self):
        self.scheduler.start()
        with patch('app.services.scheduler.push_radar_distance') as push, \
             patch.object(self.scheduler, '_try_release_after_accept') as release, \
             patch.object(self.scheduler, '_try_tail_capture') as tail, \
             patch.object(self.scheduler, '_process_distance_update') as thresholds:
            self.scheduler.on_radar_data(27.2, 1, object_count=2)
        push.assert_called_once_with(27.2, 1, object_count=2)
        release.assert_called_once_with(2, 27.2, intrusion=None)
        tail.assert_called_once_with(27.2)
        thresholds.assert_called_once_with(27.2)

    def test_real_vmrmc_updates_before_accept_after_accept_and_after_stop(self):
        radar = RadarReader('test-radar')
        radar.set_distance_callback(self.scheduler.on_radar_data)
        with patch.object(handler.socketio, 'emit') as emit, \
             patch.object(self.scheduler, '_try_release_after_accept'), \
             patch.object(self.scheduler, '_try_tail_capture'), \
             patch.object(self.scheduler, '_process_distance_update'):
            for state, distance in ((False, 34.8), (True, 28.1), (False, 19.3)):
                self.scheduler.start() if state else self.scheduler.stop()
                radar._dispatch(f'$VMRMC,328011.983,4,{distance},0.000,0.095,0.000,76')
                message = emit.call_args.args[1]
                self.assertEqual(message['type'], 'radar_distance')
                self.assertEqual(message['data'], {'distance': distance, 'mode': 1, 'objectCount': 4})
            radar._dispatch('$VMRMC,328012,0,0,0')
            self.assertEqual(emit.call_args.args[1]['data']['distance'], 0)
            self.assertEqual(emit.call_args.args[1]['data']['objectCount'], 0)

    def test_nonfinite_distance_not_broadcast_or_used_for_actions(self):
        self.scheduler.start()
        with patch('app.services.scheduler.push_radar_distance') as push, \
             patch.object(self.scheduler, '_process_distance_update') as thresholds, \
             patch.object(handler.socketio, 'emit') as emit:
            for distance in (float('nan'), float('inf'), -float('inf')):
                self.scheduler.on_radar_data(distance)
                handler.push_radar_distance(distance)
        push.assert_not_called()
        thresholds.assert_not_called()
        emit.assert_not_called()

    def test_cache_tracks_latest_packet_even_when_broadcast_is_throttled(self):
        with patch.object(handler.time, 'monotonic', return_value=100.0), \
             patch.object(handler.socketio, 'emit') as emit:
            handler.push_radar_distance(28.108, 4, object_count=4)
            handler.push_radar_distance(28.100, 4, object_count=4)
            self.assertEqual(emit.call_count, 1)
            message = handler.get_last_radar_distance()
            self.assertEqual(message['data']['distance'], 28.1)
            message['data']['distance'] = 0
            self.assertEqual(handler.get_last_radar_distance()['data']['distance'], 28.1)
        with patch.object(handler.time, 'monotonic', return_value=105.01):
            self.assertIsNone(handler.get_last_radar_distance())

    def test_connect_reconnect_and_subscribe_replay_fresh_position_without_actions(self):
        app = Flask(__name__)
        server = SocketIO(app, async_mode='threading')
        with patch.object(handler, 'socketio', server), \
             patch.object(handler, '_ws_clients', 0), \
             patch.object(handler, 'get_last_plc_status', return_value=None), \
             patch.object(handler, 'get_last_radiometer_status', return_value=None), \
             patch.object(handler.time, 'monotonic', return_value=200.0):
            handler.register_handlers()
            handler.push_radar_distance(31.2, 1, object_count=1)
            for _ in range(2):
                client = server.test_client(app)
                messages = [event['args'] for event in client.get_received() if event['name'] == 'message']
                radar = [message for message in messages if message['type'] == 'radar_distance']
                self.assertEqual(len(radar), 1)
                self.assertEqual(radar[0]['data']['distance'], 31.2)
                client.emit('message', {'type': 'subscribe', 'data': {'topics': ['radar_distance']}})
                replay = [event['args'] for event in client.get_received() if event['name'] == 'message']
                self.assertTrue(any(message['type'] == 'radar_distance' for message in replay))
                client.disconnect()
            with patch.object(handler.time, 'monotonic', return_value=206.0):
                client = server.test_client(app)
                messages = [event['args'] for event in client.get_received() if event['name'] == 'message']
                self.assertFalse(any(message['type'] == 'radar_distance' for message in messages))
                client.disconnect()


if __name__ == '__main__':
    unittest.main()
