"""业务时机到实际 MDRMC 报文的测试；所有物理控制和网络均被隔离。"""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from flask import Flask

from app.api import booking
from app.devices import mcast_cmd
from app.devices.radar import RadarReader
from app.services import command_announce as voice
from app.services import lane_announce
from app.websocket.handler import push_booking_event


class VoiceWorkflowTimingTest(unittest.TestCase):
    def setUp(self):
        state = dict(booking.get_booking_state())
        self.addCleanup(lambda: booking.get_booking_state().update(state))
        booking.clear_book_button()
        booking.set_booking_state(is_detection=False, check_step=0, booking_dialog_shown=False)
        self.lane = lane_announce.LaneAnnounceService()
        app = Flask(__name__)
        app.register_blueprint(booking.booking_api)
        self.client = app.test_client()
        self.threads = []
        real_thread = threading.Thread
        def tracked_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            self.threads.append(thread)
            return thread
        self.patches = [
            patch.object(lane_announce.threading, 'Thread', side_effect=tracked_thread),
            patch.object(lane_announce, 'get_lane_announce', return_value=self.lane),
            patch('app.services.device_action.execute_device_action', return_value=True),
            patch('socket.socket', side_effect=AssertionError('network forbidden')),
        ]
        self.actions = self.patches[2].start()
        self.addCleanup(self.patches[2].stop)
        self.patches.pop(2)
        self.ws_patch = patch('app.websocket.handler.push_booking_event')
        self.ws = self.ws_patch.start()
        self.addCleanup(self.ws_patch.stop)
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / 'voice.json'
        path.write_text(json.dumps({'targets': ['front', 'back'],
                                   'steps': {str(n): f'VOICE{n}' for n in range(1, 10)},
                                   'repeat_interval_sec': 10}), encoding='utf8')
        config_patch = patch.object(voice, 'CONFIG_PATH', path)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.messages = []
        self.cv = threading.Condition()
        def publish(message):
            with self.cv:
                self.messages.append(message)
                self.cv.notify_all()
            return True
        sender_patch = patch.object(mcast_cmd, 'publish_mdrmc', side_effect=publish)
        sender_patch.start()
        self.addCleanup(sender_patch.stop)
        voice.stop_speak()

    def tearDown(self):
        self.lane.stop_idle_loop()
        voice.stop_speak()
        for thread in self.threads:
            if thread.ident is not None:
                thread.join(2)
                self.assertFalse(thread.is_alive(), thread.name)
        booking.clear_book_button()

    def wait_messages(self, count):
        with self.cv:
            self.assertTrue(self.cv.wait_for(lambda: len(self.messages) >= count, timeout=2))

    def expected(self, *steps):
        return [f'$MDRMC,1,{target}voiceannounce=VOICE{step}'
                for step in steps for target in ['front', 'back']]

    def test_idle_target_sends_voice1_to_both_sides_and_stops_when_target_leaves(self):
        self.lane.on_radar(30, 1)
        self.wait_messages(2)
        self.lane.on_radar(0, 0)
        for thread in self.threads:
            thread.join(1)
        self.assertEqual(self.messages, self.expected(1))

    def test_idle_led_follows_each_voice_dispatch_not_each_radar_packet(self):
        # 运行真实重复播报器，但立即到达下一周期，避免测试等待十秒。
        self.lane._idle_looping = True
        self.lane._radar_target = True
        self.lane._is_system_idle_unlocked = lambda: len(self.messages) < 4
        with patch.object(voice.time, 'monotonic', side_effect=range(0, 1000, 20)):
            self.lane._idle_worker()
        for thread in list(self.threads):
            thread.join(1)
        self.assertEqual(self.messages, self.expected(1, 1))
        self.assertEqual([c.args for c in self.actions.call_args_list], [
            ('led_001', 'set_step1'), ('led_001', 'set_step1')])

    def test_radar_does_not_send_led_before_voice_dispatch(self):
        with patch.object(self.lane, '_ensure_idle_loop'):
            self.lane.on_radar(30, 1)
            self.lane.on_radar(29, 1)
        self.actions.assert_not_called()
        self.assertEqual(self.messages, [])

    def test_no_idle_announcement_without_target_or_during_detection(self):
        self.lane.on_radar(0, 0)
        booking.set_booking_state(is_detection=True, check_step=1)
        self.lane.on_radar(30, 1)
        self.assertEqual(self.messages, [])
        self.assertIsNone(self.lane._idle_thread)

    def test_radar_packet_triggers_voice1_and_no_target_packet_stops_it(self):
        radar = RadarReader('test-radar')
        radar.set_distance_callback(lambda distance, mode, count: self.lane.on_radar(distance, mode))
        radar._dispatch('$VMRMC,120000,1,30,0')
        self.wait_messages(2)
        radar._dispatch('$VMRMC,120001,0,0,0')
        for thread in self.threads:
            thread.join(1)
        self.assertEqual(self.messages, self.expected(1))
        self.assertFalse(self.lane._idle_looping)

    def test_booking_state_stops_voice1_without_another_radar_packet(self):
        self.lane.on_radar(30, 1)
        self.wait_messages(2)
        booking.set_booking_state(booking_dialog_shown=True)
        thread = self.lane._idle_thread
        if thread is not None:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.messages, self.expected(1))
        self.assertFalse(self.lane._idle_looping)

    def test_booking_button_and_following_dialog_send_voice2_once(self):
        self.lane.on_booking_button(push_ws=False)
        self.wait_messages(2)
        self.assertTrue(booking.ensure_step2_announce_once())
        self.assertEqual(self.messages, self.expected(2))

    def test_physical_and_ui_buttons_use_same_booking_actions_and_voice(self):
        for source in ('plc', 'ui'):
            for detecting in (False, True):
                with self.subTest(source=source, detecting=detecting):
                    booking.clear_book_button()
                    booking.set_booking_state(is_detection=detecting, check_step=int(detecting), booking_dialog_shown=False)
                    self.messages.clear()
                    self.actions.reset_mock()
                    self.ws.reset_mock()
                    if source == 'plc':
                        radar = RadarReader('test-radar')
                        radar.set_plc_status_callback(self.lane.on_plc_status)
                        radar._dispatch('$NTRMC,DEVICE,000080c2,001fffff')
                        radar._dispatch('$NTRMC,DEVICE,000080e2,001fffff')
                        # 重复高电平、语音反馈位变化均不能再处理预约键。
                        radar._dispatch('$NTRMC,DEVICE,000380e2,001fffff')
                        radar._dispatch('$NTRMC,DEVICE,000080e2,001fffff')
                        self.ws.assert_called_once()
                        self.assertEqual(self.ws.call_args.args, ('book_button',))
                        self.assertIs(self.ws.call_args.kwargs['openDialog'], not detecting)
                    else:
                        response = self.client.post('/api/booking/btn-press', json={'source': 'ui'})
                        self.assertEqual(response.status_code, 200)
                        self.assertIs(response.get_json()['data']['openDialog'], not detecting)
                        self.ws.assert_not_called()
                    self.wait_messages(2)
                    for thread in list(self.threads):
                        thread.join(1)
                    self.assertTrue(booking.get_booking_state()['btn_prebook_state'])
                    self.assertTrue(booking.get_booking_state()['last_btn_click_time'])
                    self.assertEqual(self.messages, self.expected(2))
                    self.actions.assert_any_call('plc_001', 'setPLC', {'redlight': True, 'yellowlight': False, 'greenlight': False})
                    self.actions.assert_any_call('gate_001', 'close')
                    self.actions.assert_any_call('led_001', 'set_step2')
                    self.assertEqual(self.actions.call_count, 3)
                    self.assertTrue(booking.ensure_step2_announce_once())
                    self.assertEqual(self.messages, self.expected(2))

    def test_accept_then_gate_release_send_voice3_then_voice4_once(self):
        self.lane.on_accepted()
        self.wait_messages(2)
        self.lane.on_gate_open()
        self.wait_messages(4)
        self.lane.on_gate_open()
        self.assertEqual(self.messages, self.expected(3, 4))

    def test_bit5_press_emits_page_booking_event_release_only_rearms_next_press(self):
        radar = RadarReader('appointment-test')
        radar.set_plc_status_callback(self.lane.on_plc_status)
        self.ws.side_effect = push_booking_event
        with patch('app.websocket.handler.socketio.emit') as emit:
            for word in (0, 0x20, 0x20, 0x10020, 0x40020):
                radar._dispatch(f'$NTRMC,DEVICE,{word:08x},001fffff')
            self.wait_messages(2)
            emit.assert_called_once()
            event = emit.call_args.args[1]
            self.assertEqual(event['type'], 'booking')
            self.assertEqual(event['data']['action'], 'book_button')
            self.assertTrue(event['data']['openDialog'])
            self.assertTrue(event['data']['btnPrebookState'])
            self.assertEqual(event['data']['btnPressTime'], booking.get_booking_state()['last_btn_click_time'])
            radar._dispatch('$NTRMC,DEVICE,00000000,001fffff')
            self.assertEqual(emit.call_count, 1)
            # 线上在线字bit5变化不是按键，不得产生预约。
            radar._dispatch('$NTRMC,DEVICE,00000000,001fffdf')
            self.assertEqual(emit.call_count, 1)
            radar._dispatch('$NTRMC,DEVICE,00000020,001fffff')
            self.wait_messages(4)
            self.assertEqual(emit.call_count, 2)
        self.assertEqual(self.messages, self.expected(2, 2))

    def test_gate_event_without_accepted_release_does_not_announce(self):
        self.lane.on_gate_open()
        self.assertEqual(self.messages, [])

    def test_light_curtain_sends_voice5_only_during_detection_and_once_per_session(self):
        self.lane.on_plc_status({'lightScreenStatus': True})
        self.assertEqual(self.messages, [])
        self.lane.on_plc_status({'lightScreenStatus': False})
        booking.set_booking_state(is_detection=True, check_step=1)
        self.lane.on_plc_status({'lightScreenStatus': True})
        self.lane.on_plc_status({'lightScreenStatus': True})
        self.lane.on_plc_status({'lightScreenStatus': False})
        self.lane.on_plc_status({'lightScreenStatus': True})
        self.assertEqual(self.messages, self.expected(5))


if __name__ == '__main__':
    unittest.main()
