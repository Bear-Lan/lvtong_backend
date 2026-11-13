"""共享三秒播放状态：真实调度及DEVICE反馈，隔离所有网络和设备动作。"""
import threading
import unittest
from unittest.mock import patch

from app.devices import mcast_cmd
from app.devices.radar import RadarReader
from app.services import command_announce as voice
from app.services.lane_announce import LaneAnnounceService


class VoicePriorityTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.config = {'targets': ['front', 'back'],
                       'steps': {str(n): f'VOICE{n}' for n in range(1, 10)},
                       'repeat_interval_sec': 10}
        self.patches = [
            patch.object(voice, '_playback', voice._PlaybackState()),
            patch.object(voice, 'load_config', return_value=self.config),
            patch.object(voice.time, 'monotonic', side_effect=lambda: self.now),
            patch.object(mcast_cmd, 'publish_mdrmc', return_value=True),
            patch('socket.socket', side_effect=AssertionError('network forbidden')),
        ]
        for item in self.patches:
            value = item.start()
            self.addCleanup(item.stop)
            if item is self.patches[3]:
                self.sender = value
        voice.stop_speak()
        self.addCleanup(voice.stop_speak)
        self.lane = LaneAnnounceService()
        self.radar = RadarReader('priority-test')
        self.radar.set_plc_status_callback(self.lane.on_plc_status)

    def feedback(self, mask):
        self.radar._dispatch(f'$NTRMC,DEVICE,{mask:08x},001fffff')

    def test_manual_reserves_priority_before_device_feedback_and_stop_does_not_clear_it(self):
        self.assertTrue(voice.speak_step(6, interrupt=False))
        self.feedback(0)  # 命令后的迟到空闲反馈不能当作播放完成。
        voice.stop_speak()
        self.assertFalse(voice.speak_step(1))
        self.assertEqual(self.sender.call_count, 2)
        self.now += 2.999
        self.assertFalse(voice.speak_step(1))
        self.now = 103
        self.assertTrue(voice.speak_step(1))  # 无任何反馈，三秒自动恢复。

    def test_both_sides_share_one_deadline_ignoring_staggered_feedback(self):
        self.assertTrue(voice.speak_step(6, interrupt=False))
        self.now += 2
        self.feedback(0x10000)
        self.feedback(0)
        self.assertFalse(voice.speak_step(1))
        self.feedback(0x20000)
        self.feedback(0)
        self.now += 1
        self.assertTrue(voice.speak_step(1))

    def test_latched_playing_feedback_cannot_block_timed_recovery(self):
        self.assertTrue(voice.speak_step(6, interrupt=False))
        self.feedback(0x30000)
        self.now += 1000
        self.assertTrue(voice.speak_step(1))
        self.assertTrue(voice.speak_step(7, interrupt=False))
        self.assertTrue(voice.speak_step(8))
        self.assertEqual(self.sender.call_count, 8)

    def test_system_initially_idle_regardless_of_device_playing_feedback(self):
        self.feedback(0x20000)
        self.assertTrue(voice.speak_step(1))
        self.assertFalse(voice.speak_step(1))  # 空闲播报本身也占用同一状态。

    def test_high_priority_immediately_replaces_idle_and_new_voice_extends_guard(self):
        self.assertTrue(voice.speak_step(1))
        self.feedback(0x30000)
        self.assertTrue(voice.speak_step(6, interrupt=False))
        self.now += 2
        self.assertTrue(voice.speak_step(8, interrupt=False))
        self.feedback(0)  # 没看到新命令起始，不能提前结束保护。
        self.now += 1
        self.assertFalse(voice.speak_step(1))
        self.now += 2
        self.feedback(0)
        self.assertTrue(voice.speak_step(1))
        self.assertEqual([c.args[0] for c in self.sender.call_args_list], [
            f'$MDRMC,1,{side}voiceannounce=VOICE{step}'
            for step in (1, 6, 8, 1) for side in ('front', 'back')])

    def test_failed_send_does_not_leave_priority_but_partial_send_does(self):
        self.sender.return_value = False
        self.assertFalse(voice.speak_step(6, interrupt=False))
        self.sender.return_value = True
        self.assertTrue(voice.speak_step(1))
        self.sender.side_effect = [True, False]
        self.assertFalse(voice.speak_step(7, interrupt=False))
        self.sender.side_effect = None
        self.assertFalse(voice.speak_step(1))
        self.now += 3
        self.assertTrue(voice.speak_step(1))

    def test_failed_send_preserves_existing_deadline_without_extending_it(self):
        self.assertTrue(voice.speak_step(6, interrupt=False))
        self.now += 2
        self.sender.return_value = False
        self.assertFalse(voice.speak_step(7, interrupt=False))
        self.sender.return_value = True
        self.assertFalse(voice.speak_step(1))
        self.now += 1
        self.assertTrue(voice.speak_step(1))

    def test_deadline_starts_from_last_successful_direction_send(self):
        def publish(message):
            self.now += .5
            return True
        self.sender.side_effect = publish
        self.assertTrue(voice.speak_step(6, interrupt=False))
        self.now = 103.999
        self.assertFalse(voice.speak_step(1))
        self.now = 104
        self.assertTrue(voice.speak_step(1))

    def test_old_idle_playback_falling_edge_does_not_finish_new_business_voice(self):
        self.assertTrue(voice.speak_step(1))
        self.feedback(0x30000)
        self.assertTrue(voice.speak_step(6, interrupt=False))
        self.now += 2
        self.feedback(0)  # 原空闲语音的结束不能释放尚未起播的新语音。
        self.assertFalse(voice.speak_step(1))
        self.feedback(0x30000)
        self.feedback(0)
        self.assertFalse(voice.speak_step(1))
        self.now += 1
        self.assertTrue(voice.speak_step(1))

    def test_waiting_idle_dispatch_cannot_slip_after_manual_send(self):
        sending = threading.Event()
        release = threading.Event()
        results = []
        def publish(message):
            if 'frontvoiceannounce=VOICE6' in message:
                sending.set()
                self.assertTrue(release.wait(2))
            return True
        self.sender.side_effect = publish
        manual = threading.Thread(target=lambda: results.append(voice.speak_step(6, interrupt=False)))
        idle = threading.Thread(target=lambda: results.append(voice.speak_step(1, interrupt=False)))
        manual.start()
        try:
            self.assertTrue(sending.wait(1))
            idle.start()
            release.set()
            manual.join(1)
            idle.join(1)
            self.assertFalse(manual.is_alive())
            self.assertFalse(idle.is_alive())
            self.assertCountEqual(results, [True, False])
            self.assertEqual(self.sender.call_count, 2)
        finally:
            release.set()
            manual.join(2)
            if idle.ident is not None:
                idle.join(2)

    def test_idle_loop_pauses_without_led_and_resumes_without_new_radar_event(self):
        self.assertTrue(voice.speak_step(6, interrupt=False))
        entered = threading.Event()
        completed = threading.Event()
        clips = []
        def continuing():
            entered.set()
            return not completed.is_set()
        def on_clip():
            clips.append(1)
            completed.set()
        worker = threading.Thread(target=voice.run_idle_announcements,
                                  args=(1, continuing), kwargs={'on_clip': on_clip})
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.sender.call_count, 2)
            self.assertEqual(clips, [])
            self.feedback(0x30000)
            self.now += 3
            self.feedback(0)
            self.assertTrue(completed.wait(1))
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(self.sender.call_count, 4)
            self.assertEqual(clips, [1])
        finally:
            completed.set()
            voice.stop_speak()
            worker.join(1)


if __name__ == '__main__':
    unittest.main()
