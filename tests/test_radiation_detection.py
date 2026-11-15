"""辐射检测全程隔离：禁止网络，开/关命令仅写入模拟列表。"""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from flask import Flask
from app.api.device import device_api
from app.devices import mcast_cmd
from app.extensions.auth import create_token
from app.services import radiation_detection as radiation


def feedback(stamp=100000, source200=False, source160=False, **extra):
    return {'receivedAt': stamp, 'fresh': True, 'lightSource200Status': source200,
            'lightSource160Status': source160, 'stickDownStatus': False,
            'urgentStopStatus': False,
            'sensors': [{'bit': bit, 'online': True} for bit in (6, 9, 10)], **extra}


class RadiationDetectionTest(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'radiation.json'
        self.now = 100.0
        self.plc = feedback()
        self.idle = True
        self.sent = []
        self.sender_result = True
        def sender(*commands, **kwargs):
            self.sent.extend((self.now, command) for command in commands)
            return self.sender_result
        self.service = radiation.RadiationDetection(self.path, sender=sender, plc=lambda: self.plc,
            idle=lambda: self.idle, clock=lambda: self.now, wall=lambda: self.now, background=False)
        self.sender = sender
        self.getter = patch.object(radiation, 'get_radiation_detection', side_effect=lambda: self.service)
        self.getter.start()
        self.addCleanup(self.getter.stop)
        app = Flask(__name__)
        app.register_blueprint(device_api)
        self.client = app.test_client()
        self.headers = {'Authorization': 'Bearer ' + create_token({'username': 'radiation-test'})}

    def post(self, data, auth=True):
        return self.client.post('/api/device/radiation-detection', json=data,
                                headers=self.headers if auth else {})

    def start(self):
        response = self.post({'action': 'start', 'confirmed': True})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['data']['runId']

    def measure(self, maximum=10):
        self.start()
        self.now = 100.1
        self.plc = feedback(100100, True, True)
        self.service.tick()
        self.service.record(.05)
        self.now = 102
        self.service.record(maximum)
        self.now = 105.9
        self.service.record(1)
        self.plc = feedback(105900, True, True)
        self.now = 106
        self.service.tick()

    def closed(self):
        self.now += .01
        self.plc = feedback(int(self.now * 1000))
        self.service.tick()
        return self.service.status()

    def test_get_and_invalid_requests_never_send_commands(self):
        self.assertEqual(self.client.get('/api/device/radiation-detection').status_code, 401)
        self.assertEqual(self.client.get('/api/device/radiation-detection', headers=self.headers).status_code, 200)
        for payload in ({}, [], {'action': 'start'}, {'action': 'start', 'confirmed': 1},
                        {'action': 'start', 'confirmed': True, 'duration': 10},
                        {'action': 'stop'}, {'action': 'invalid'}):
            self.assertEqual(self.post(payload).status_code, 400)
        self.assertEqual(self.post({'action': 'start', 'confirmed': True}, auth=False).status_code, 401)
        self.assertEqual(self.sent, [])

    def test_preconditions_require_live_gate_and_both_sources_off(self):
        variants = [None, feedback(fresh=False), feedback(source200=True), feedback(source160=True),
                    feedback(stickDownStatus=True), feedback(urgentStopStatus=True),
                    feedback(sensors=[]), feedback(lightSource160Status=None),
                    feedback(sensors=[{'bit': 6, 'online': True}, {'bit': 9, 'online': True}, {'bit': 10, 'online': False}])]
        for data in variants:
            with self.subTest(data=data):
                self.plc = data
                self.assertEqual(self.post({'action': 'start', 'confirmed': True}).status_code, 409)
        self.plc = feedback()
        self.idle = False
        self.assertEqual(self.post({'action': 'start', 'confirmed': True}).status_code, 409)
        self.assertFalse(self.sent)

    def test_six_second_deadline_sends_all_three_stop_commands(self):
        self.measure()
        self.assertEqual(self.sent[:2], [(100, 'xraysource200=true'), (100, 'xraysource160=true')])
        self.assertEqual(self.sent[2:], [(106, command) for command in radiation.STOP_COMMANDS])
        state = self.service.status()
        self.assertTrue(state['active'])
        self.assertEqual(state['phase'], 'stopping')
        self.assertIsNone(state['result'])
        state = self.closed()
        self.assertFalse(state['active'])
        self.assertTrue(state['bothSourcesOff'])
        self.assertEqual(state['result'], {'value': 10, 'level': 'normal'})
        self.assertEqual(state['elapsedSeconds'], 6)
        self.assertFalse(json.loads(self.path.read_text())['active'])

    def test_old_single_missing_or_offline_feedback_does_not_finish_and_retries_continue(self):
        self.measure()
        for data in (feedback(105000), feedback(106100, True, False), feedback(106100, False, True),
                     feedback(106100, fresh=False), feedback(106100, lightSource160Status=None),
                     feedback(106100, sensors=[])):
            self.plc = data
            self.now += 1
            self.service.tick()
            self.assertTrue(self.service.status()['active'])
            self.assertFalse(self.service.status()['bothSourcesOff'])
        self.assertGreaterEqual(self.service.status()['stopAttempts'], 7)
        self.assertIn('尚未确认', self.service.status()['error'])
        state = self.closed()
        self.assertFalse(state['active'])
        self.assertEqual(state['phase'], 'completed')

    def test_confirmed_threshold_and_invalid_out_of_window_samples(self):
        for maximum, level in ((15, 'normal'), (15.001, 'high'), (31, 'high')):
            with self.subTest(maximum=maximum):
                self.setUpScenario()
                self.measure(maximum)
                for invalid in (float('nan'), float('inf'), -1, True):
                    self.service.record(invalid)
                self.service.record(9999)  # 已到停止阶段，不能混入结果。
                state = self.closed()
                self.assertEqual(state['result'], {'value': maximum, 'level': level})

    def setUpScenario(self):
        self.now = 100
        self.plc = feedback()
        self.service = radiation.RadiationDetection(self.path, sender=self.sender, plc=lambda: self.plc,
            idle=lambda: self.idle, clock=lambda: self.now, wall=lambda: self.now, background=False)

    def test_insufficient_data_never_reports_normal(self):
        self.start()
        self.now = 100.1
        self.plc = feedback(100100, True, True)
        self.service.tick()
        self.service.record(0)
        self.now = 106
        self.service.tick()
        state = self.closed()
        self.assertEqual(state['phase'], 'failed')
        self.assertIsNone(state['result'])
        self.assertTrue(state['bothSourcesOff'])

    def test_no_open_feedback_never_reports_normal_and_still_stops_at_six_seconds(self):
        self.start()
        self.now = 102.5
        self.service.record(0)
        self.service.tick()
        self.assertEqual(self.service.status()['phase'], 'measuring')
        self.now = 105.9
        self.service.record(0)
        self.now = 106
        self.service.tick()
        self.assertEqual([command for _, command in self.sent[-3:]], list(radiation.STOP_COMMANDS))
        self.assertEqual(self.closed()['phase'], 'failed')

    def test_duplicate_start_and_old_run_stop_cannot_restart_or_stop_new_run(self):
        run_id = self.start()
        self.assertEqual(self.post({'action': 'start', 'confirmed': True}).status_code, 409)
        self.assertEqual(len(self.sent), 2)
        self.post({'action': 'stop', 'runId': 'previous-run'})
        self.assertEqual(len(self.sent), 2)
        self.post({'action': 'stop', 'runId': run_id})
        self.assertEqual(self.service.status()['phase'], 'stopping')
        self.assertTrue(self.service.status()['active'])
        self.assertEqual(self.closed()['phase'], 'cancelled')

    def test_feedback_loss_early_source_loss_or_gate_raise_stops_early(self):
        for kind in ('feedback', 'gate', 'source', 'booking'):
            with self.subTest(kind=kind):
                self.setUpScenario()
                self.start()
                self.now = 100.1
                self.plc = feedback(100100, True, True)
                self.service.tick()
                self.now = 101
                self.plc = feedback(101000, kind != 'source', True,
                                    fresh=kind != 'feedback', stickDownStatus=kind == 'gate')
                self.idle = kind != 'booking'
                self.service.tick()
                self.assertEqual(self.service.status()['phase'], 'stopping')
                self.assertEqual(self.closed()['phase'], 'cancelled')
                self.idle = True

    def test_open_failure_and_stop_exception_still_attempt_both_sources(self):
        calls = []
        def failing_sender(*commands, **kwargs):
            calls.extend(commands)
            if commands == ('xraycap=false',):
                raise OSError('simulated failure')
            return False
        self.service._sender = failing_sender
        self.start()
        self.assertEqual(calls[-3:], list(radiation.STOP_COMMANDS))
        self.assertTrue(self.closed()['active'], '发送失败时不能仅凭旧关闭状态结束')
        self.service._sender = self.sender
        self.now += 1
        self.service.tick()
        self.assertFalse(self.closed()['active'])

    def test_journal_failure_prevents_opening_and_restart_only_sends_stops(self):
        with patch.object(self.service, '_save', side_effect=OSError('disk full')):
            self.assertEqual(self.post({'action': 'start', 'confirmed': True}).status_code, 409)
        self.assertFalse(self.sent)
        self.start()
        self.sent.clear()
        restored = radiation.RadiationDetection(self.path, sender=self.sender, plc=lambda: self.plc,
            idle=lambda: self.idle, clock=lambda: self.now, wall=lambda: self.now, background=False)
        restored.recover()
        self.assertTrue(restored.status()['active'])
        self.assertEqual([command for _, command in self.sent], list(radiation.STOP_COMMANDS))
        self.now += 1
        self.plc = feedback(101000)
        restored.tick()
        self.assertEqual(restored.status()['phase'], 'cancelled')

    def test_device_guard_blocks_other_xray_and_gate_commands_until_both_closed(self):
        run_id = self.start()
        with patch.object(mcast_cmd, 'publish_mdrmc', return_value=True) as publish:
            for command in ('xraycap=true&1.8&large', 'xraysource200=true', 'xraysource160=true',
                            'xraygate200=true', 'boombarrier=true', 'interlock160=false'):
                self.assertFalse(mcast_cmd.send_mdrmc(command))
            publish.assert_not_called()
            self.assertTrue(mcast_cmd.send_mdrmc('xraycap=false'))
            self.assertTrue(mcast_cmd.send_mdrmc('boombarrier=false'))
            self.assertTrue(mcast_cmd.send_mdrmc('xraysource200=true', radiation_token=run_id))
            self.service.stop(run_id)
            self.assertFalse(mcast_cmd.send_mdrmc('xraysource200=true', radiation_token=run_id))
            self.closed()
            self.assertTrue(mcast_cmd.send_mdrmc('boombarrier=true'))

    def test_push_records_real_stream_before_browser_emission(self):
        from app.websocket import handler
        self.start()
        self.now = 100.5
        with patch.object(handler.socketio, 'emit'):
            handler.push_radiometer_status(.05)
            handler.push_radiometer_status(12)
        self.assertEqual([sample['value'] for sample in self.service.status()['samples']], [.05, 12])


class RadiationWatchdogWallClockTest(unittest.TestCase):
    def test_real_six_seconds_without_browser_calls(self):
        """真实六秒计时，完全模拟命令传输；不启动应用/数据库/设备。"""
        data = feedback(int(time.time() * 1000))
        commands = []
        stopped = threading.Event()
        def sender(*items, **kwargs):
            commands.extend((time.monotonic(), item) for item in items)
            if 'xraysource200=true' in items:
                data.update(lightSource200Status=True, lightSource160Status=True,
                            receivedAt=int(time.time() * 1000) + 1)
            if items == ('xraysource160=false',):
                stopped.set()
            return True
        service = radiation.RadiationDetection(sender=sender, plc=lambda: data, idle=lambda: True)
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            service.start()
            self.assertTrue(stopped.wait(7))
            delta = commands[2][0] - commands[0][0]
            self.assertGreaterEqual(delta, 5.95)
            self.assertLess(delta, 6.3)
            self.assertEqual([command for _, command in commands[2:5]], list(radiation.STOP_COMMANDS))
            data.update(lightSource200Status=False, lightSource160Status=False,
                        receivedAt=int(time.time() * 1000) + 1)
            service._thread.join(1)
            self.assertFalse(service._thread.is_alive())
            self.assertTrue(service.status()['bothSourcesOff'])


if __name__ == '__main__':
    unittest.main()
