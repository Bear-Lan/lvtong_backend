"""提示循环的接口、播放间隔与停止竞态：禁止创建网络套接字。"""
import threading
import time
import unittest
from unittest.mock import patch

from flask import Flask
from app.api.device import device_api
from app.devices import mcast_cmd
from app.extensions.auth import create_token
from app.services import command_announce as voice
from app.services import prompt_announce as prompt


class PromptAnnounceTest(unittest.TestCase):
    def setUp(self):
        self.service = prompt.PromptAnnounceService()
        self.cv = threading.Condition()
        self.messages = []
        self.config = {'targets': ['front', 'back'],
                       'steps': {str(n): f'VOICE{n}' for n in range(1, 10)}}
        def publish(message):
            with self.cv:
                self.messages.append((time.monotonic(), message))
                self.cv.notify_all()
            return True
        self.patches = [patch.object(voice, '_playback', voice._PlaybackState()),
                        patch.object(voice, 'load_config', return_value=self.config),
                        patch.object(prompt, '_timing', return_value=(0.3, 0.15)),
                        patch.object(prompt, 'get_prompt_announce', return_value=self.service),
                        patch.object(mcast_cmd, 'publish_mdrmc', side_effect=publish),
                        patch('socket.socket', side_effect=AssertionError('network forbidden')),
                        patch.object(voice, 'PLAYBACK_DURATION_SEC', .3)]
        for item in self.patches:
            value = item.start()
            self.addCleanup(item.stop)
            if item is self.patches[4]:
                self.sender = value
        app = Flask(__name__)
        app.register_blueprint(device_api)
        self.client = app.test_client()
        self.headers = {'Authorization': 'Bearer '+create_token({'username': 'prompt-test'})}

    def tearDown(self):
        self.service.stop()
        if self.service._thread:
            self.service._thread.join(1)
            self.assertFalse(self.service._thread.is_alive())

    def wait_messages(self, number):
        with self.cv:
            self.assertTrue(self.cv.wait_for(lambda: len(self.messages) >= number, timeout=2))

    def post(self, body):
        return self.client.post('/api/device/voice/prompt', json=body, headers=self.headers)

    def test_authenticated_list_start_repeat_and_stop_use_only_voice9(self):
        state = self.client.get('/api/device/voice/prompt', headers=self.headers).json['data']
        self.assertFalse(state['active'])
        self.assertEqual(state['options'], [{'step': 9, 'text': '前方超限车辆倒车，请注意避让'}])
        result = self.post({'action': 'start', 'step': 9})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json['data']['active'])
        self.wait_messages(4)
        delta = self.messages[2][0]-self.messages[0][0]
        self.assertGreaterEqual(delta, .44, '反馈缺失也保留完整播放时段及停顿')
        stopped = self.post({'action': 'stop'})
        self.assertFalse(stopped.json['data']['active'])
        count = len(self.messages)
        self.assertTrue(self.service._cancel.wait(.5))
        self.service._thread.join(1)
        self.assertEqual(len(self.messages), count)
        self.assertEqual([message for _, message in self.messages], [
            f'$MDRMC,1,{side}voiceannounce=VOICE9'
            for _ in range(count//2) for side in ('front', 'back')])
        self.assertFalse(voice.speak_step(1), '当前条尚未播完，停止循环不清除空闲保护')

    def test_shared_timer_completion_then_gap_and_no_idle_during_gap(self):
        self.patches[2].stop()
        with patch.object(prompt, '_timing', return_value=(.3, .2)):
            self.assertTrue(self.service.start()[0])
        self.assertFalse(voice.speak_step(1))
        self.assertEqual(len(self.messages), 2)
        deadline = voice._playback.busy_until
        self.assertFalse(voice.speak_step(1), '两轮之间仍由提示循环占用')
        self.wait_messages(4)
        self.assertGreaterEqual(self.messages[2][0]-deadline, .19)

    def test_duplicate_start_is_idempotent_and_restart_has_new_version(self):
        first = self.service.start()[1]
        self.assertTrue(self.service.start()[0])
        self.assertEqual(len(self.messages), 2)
        old = self.service._thread
        stopped = self.service.stop()
        self.assertGreater(stopped['version'], first['version'])
        again = self.service.start()[1]
        self.assertGreater(again['version'], stopped['version'])
        old.join(1)
        self.assertEqual(len(self.messages), 4)

    def test_new_business_voice_prevents_repeat_until_it_finishes(self):
        self.assertTrue(self.service.start()[0])
        with patch.object(voice, 'PLAYBACK_DURATION_SEC', 1):
            self.assertTrue(voice.speak_step(6, interrupt=False))
        with self.cv:
            self.assertFalse(self.cv.wait_for(lambda: len(self.messages)>4, timeout=.65))
        self.wait_messages(6)
        self.assertTrue(self.messages[4][1].endswith('VOICE9'))
        self.assertGreaterEqual(self.messages[4][0]-self.messages[2][0], 1.14)

    def test_repeat_uses_full_shared_playback_duration_even_with_shorter_clip_setting(self):
        self.patches[2].stop()
        with patch.object(prompt, '_timing', return_value=(.05, .15)):
            self.assertTrue(self.service.start()[0])
        with self.cv:
            self.assertFalse(self.cv.wait_for(lambda: len(self.messages)>2, timeout=.25))
        self.wait_messages(4)
        self.assertGreaterEqual(self.messages[2][0]-self.messages[0][0], .44)
        self.service.stop()
        self.assertFalse(voice.speak_step(1), '停止循环不能提前释放本轮三秒保护')

    def test_stop_waits_for_current_dispatch_and_prevents_any_later_dispatch(self):
        second = threading.Event()
        release = threading.Event()
        calls = []
        def publish(message):
            calls.append(message)
            if len(calls)==3:
                second.set()
                self.assertTrue(release.wait(2))
            return True
        self.sender.side_effect = publish
        self.assertTrue(self.service.start()[0])
        self.assertTrue(second.wait(2))
        finished = threading.Event()
        worker = threading.Thread(target=lambda: (self.service.stop(), finished.set()))
        worker.start()
        try:
            self.assertFalse(finished.wait(.05))
            release.set()
            self.assertTrue(finished.wait(1))
            self.service._thread.join(1)
            self.assertEqual(len(calls), 4)
        finally:
            release.set()
            worker.join(1)

    def test_failed_first_and_repeat_dispatch_stop_loop_and_expose_error(self):
        self.sender.side_effect = None
        self.sender.return_value = False
        response = self.post({'action': 'start', 'step': 9})
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json['data']['active'])
        self.assertFalse(voice._playback.prompt_active)
        self.sender.side_effect = [True, True, False, False]
        self.assertTrue(self.service.start()[0])
        self.service._thread.join(2)
        state = self.service.status()
        self.assertFalse(state['active'])
        self.assertIn('循环已停止', state['error'])
        self.assertFalse(voice._playback.prompt_active)

    def test_only_authenticated_step9_start_and_stop_are_accepted(self):
        self.assertEqual(self.client.get('/api/device/voice/prompt').status_code, 401)
        self.assertEqual(self.client.post('/api/device/voice/prompt',json={'action':'start','step':9}).status_code, 401)
        for body in ([], {}, {'action':'start','step': True}, {'action':'start','step':'9'},
                     {'action':'start','step':9.0}, {'action':'start','step':6}, {'action':'other'}):
            self.assertEqual(self.post(body).status_code, 400, body)
        self.sender.assert_not_called()

    def test_timing_configuration_validation(self):
        self.patches[2].stop()
        for key,value in (('prompt_clip_sec',False),('prompt_clip_sec',0),('prompt_gap_sec',float('nan')),('prompt_gap_sec',31)):
            self.config[key] = value
            with self.assertRaises(ValueError):prompt._timing()
            del self.config[key]
        self.assertEqual(prompt._timing(), (3,2))


if __name__ == '__main__':
    unittest.main()
