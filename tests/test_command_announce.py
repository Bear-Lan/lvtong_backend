"""命令协议与并发回归测试：所有网络发送均被替换，不接触现场设备。"""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.devices import mcast_cmd
from app.services import command_announce as voice


class VoiceCommandsTest(unittest.TestCase):
    def setUp(self):
        self.playback_patch = patch.object(voice, '_playback', voice._PlaybackState())
        self.playback_patch.start()
        self.addCleanup(self.playback_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'voice.json'
        self.config = {'targets': ['front'], 'steps': {'1': 'VOICE3', '2': 'VOICE7'},
                       'repeat_interval_sec': 10}
        self.write_config()
        self.path_patch = patch.object(voice, 'CONFIG_PATH', self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.sender_patch = patch.object(mcast_cmd, 'publish_mdrmc', return_value=True)
        self.sender = self.sender_patch.start()
        self.addCleanup(self.sender_patch.stop)
        # 即便测试遗漏 mock，也不允许打开网络套接字。
        self.socket_patch = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)
        voice.stop_speak()

    def write_config(self):
        self.path.write_text(json.dumps(self.config), encoding='utf8')

    def test_step_uses_explicit_mapping_and_exact_protocol(self):
        self.assertTrue(voice.speak_step(2, blocking=True))
        self.sender.assert_called_once_with('$MDRMC,1,frontvoiceannounce=VOICE7')

    def test_front_and_back_are_separate_commands(self):
        self.config['targets'] = ['front', 'back']
        self.write_config()
        self.assertTrue(voice.speak_step(1))
        self.assertEqual([c.args[0] for c in self.sender.call_args_list],
                         ['$MDRMC,1,frontvoiceannounce=VOICE3', '$MDRMC,1,backvoiceannounce=VOICE3'])

    def test_back_only(self):
        self.config['targets'] = ['back']
        self.write_config()
        self.assertTrue(voice.speak_step(1))
        self.sender.assert_called_once_with('$MDRMC,1,backvoiceannounce=VOICE3')

    def test_missing_configuration_does_not_guess(self):
        for change in [{'targets': []}, {'steps': {}}, {'targets': ['unknown']},
                       {'steps': {'1': 'VOICE0'}}, {'steps': {'1': 'VOICE10'}},
                       {'steps': {'1': 'VOICE1,xraysource200=true'}},
                       {'targets': ['front', 'front']}, {'steps': {'10': 'VOICE1'}},
                       {'repeat_interval_sec': 0}, {'repeat_interval_sec': float('nan')}]:
            with self.subTest(change=change):
                original = dict(self.config)
                self.config.update(change)
                self.write_config()
                self.assertFalse(voice.speak_step(1))
                self.config = original
        self.sender.assert_not_called()

    def test_file_missing_or_malformed_never_sends(self):
        self.path.unlink()
        self.assertFalse(voice.speak_step(1))
        self.path.write_text('{', encoding='utf8')
        self.assertFalse(voice.speak_step(1))
        self.sender.assert_not_called()

    def test_unknown_step_or_text_does_not_send(self):
        self.assertFalse(voice.speak_step(10))
        self.assertFalse(voice.speak_step(1.0))
        self.assertFalse(voice.speak_step(True))
        self.assertFalse(voice.speak_text('arbitrary command'))
        self.sender.assert_not_called()

    def test_non_object_configuration_is_rejected(self):
        self.path.write_text('[]', encoding='utf8')
        self.assertFalse(voice.speak_step(1))
        self.sender.assert_not_called()

    def test_all_nine_confirmed_voice_codes_can_be_dispatched(self):
        self.config['steps'] = {str(n): f'VOICE{n}' for n in range(1, 10)}
        self.write_config()
        for step in range(1, 10):
            self.assertTrue(voice.speak_step(step))
        self.assertEqual([c.args[0] for c in self.sender.call_args_list],
                         [f'$MDRMC,1,frontvoiceannounce=VOICE{n}' for n in range(1, 10)])

    def test_udp_failure_is_reported_and_not_retried(self):
        self.sender.return_value = False
        self.assertFalse(voice.speak_step(1))
        self.assertEqual(self.sender.call_count, 1)

    def test_partial_target_failure_is_not_success(self):
        self.config['targets'] = ['front', 'back']
        self.write_config()
        self.sender.side_effect = [True, False]
        self.assertFalse(voice.speak_step(1))
        self.assertEqual(self.sender.call_count, 2)

    def test_idle_stop_is_prompt_and_prevents_repeat(self):
        sent = threading.Event()
        self.sender.side_effect = lambda message: (sent.set() or True)
        thread = threading.Thread(target=voice.run_idle_announcements, args=(1, lambda: True))
        thread.start()
        try:
            self.assertTrue(sent.wait(1))
            voice.stop_speak()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(self.sender.call_count, 1)
        finally:
            voice.stop_speak()
            thread.join(1)

    def test_new_step_cancels_old_idle_loop(self):
        sent = threading.Event()
        self.sender.side_effect = lambda message: (sent.set() or True)
        thread = threading.Thread(target=voice.run_idle_announcements, args=(1, lambda: True))
        thread.start()
        try:
            self.assertTrue(sent.wait(1))
            self.assertTrue(voice.speak_step(2))
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual([c.args[0] for c in self.sender.call_args_list],
                             ['$MDRMC,1,frontvoiceannounce=VOICE3', '$MDRMC,1,frontvoiceannounce=VOICE7'])
        finally:
            voice.stop_speak()
            thread.join(1)

    def test_cancelled_idle_generation_cannot_dispatch_after_new_step(self):
        generation = voice._generation
        voice.stop_speak()
        self.assertFalse(voice._dispatch_step(1, expected_generation=generation))
        self.sender.assert_not_called()

    def test_repeat_interval_limits_rate(self):
        self.config['repeat_interval_sec'] = 1
        self.write_config()
        ticks = []
        def publish(message):
            ticks.append(time.monotonic())
            return True
        self.sender.side_effect = publish
        voice.run_idle_announcements(1, lambda: len(ticks) < 2)
        self.assertEqual(len(ticks), 2)
        self.assertGreaterEqual(ticks[1] - ticks[0], 0.95)

    def test_legacy_import_dispatches_without_camera_connection(self):
        from app.services import booking_camera_tts as legacy
        self.assertTrue(legacy.speak_text(voice.ANNOUNCE_TEXT[2]))
        legacy.release_voice_channel(settle_sec=10)
        self.sender.assert_called_once_with('$MDRMC,1,frontvoiceannounce=VOICE7')


if __name__ == '__main__':
    unittest.main()
