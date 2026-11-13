"""球机手动语音入口：验证实际MDRMC组包，所有网络发送被替换。"""
import unittest
from unittest.mock import patch

from flask import Flask

from app.api.device import device_api
from app.devices import mcast_cmd
from app.extensions.auth import create_token
from app.services import command_announce as voice


class CaptureVoiceApiTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(device_api)
        self.client = app.test_client()
        self.headers = {'Authorization': 'Bearer ' + create_token({'username': 'capture-test'})}
        self.sender_patch = patch.object(mcast_cmd, 'publish_mdrmc', return_value=True)
        self.sender = self.sender_patch.start()
        self.addCleanup(self.sender_patch.stop)
        self.config_patch = patch.object(voice, 'load_config', return_value={
            'targets': ['front', 'back'], 'steps': {str(n): f'VOICE{n}' for n in (6, 7, 8)},
        })
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.network_patch = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.network_patch.start()
        self.addCleanup(self.network_patch.stop)

    def test_three_buttons_send_correct_front_and_back_commands_only(self):
        expected_texts = {6: '请打开车门', 7: '请勿遮挡车厢', 8: '拍摄完成，请关好车门'}
        generation = voice._generation
        for step, text in expected_texts.items():
            with self.subTest(step=step):
                self.sender.reset_mock()
                response = self.client.post('/api/device/voice/announce', json={'step': step}, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['data'], {'step': step, 'text': text, 'sent': True})
                self.assertEqual([c.args[0] for c in self.sender.call_args_list], [
                    f'$MDRMC,1,frontvoiceannounce=VOICE{step}',
                    f'$MDRMC,1,backvoiceannounce=VOICE{step}',
                ])
        self.assertEqual(voice._generation, generation, '手动提示不取消自动播报')

    def test_only_authenticated_capture_steps_are_allowed(self):
        self.assertEqual(self.client.post('/api/device/voice/announce', json={'step': 6}).status_code, 401)
        for payload in ({}, [], {'step': True}, {'step': '6'}, {'step': 6.0}, {'step': 1}, {'step': 9}, {'step': 'VOICE6,xraycap=true'}):
            with self.subTest(payload=payload):
                response = self.client.post('/api/device/voice/announce', json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 400)
        self.sender.assert_not_called()

    def test_send_failure_is_visible_and_never_claims_playback_complete(self):
        self.sender.return_value = False
        response = self.client.post('/api/device/voice/announce', json={'step': 8}, headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json['data']['sent'])
        self.assertNotIn('played', response.json['data'])
        self.assertEqual(self.sender.call_count, 2)


if __name__ == '__main__':
    unittest.main()
