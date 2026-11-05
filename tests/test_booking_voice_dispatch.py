"""预约语音调用集成回归；设备动作、语音派发均为 mock。"""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

from flask import Flask

from app.api import booking
from app.services import command_announce as voice
from app.services import lane_announce
from app.services.voice_channel import get_voice_channel


class BookingVoiceDispatchTest(unittest.TestCase):
    def setUp(self):
        state = dict(booking.get_booking_state())
        self.addCleanup(lambda: booking.get_booking_state().update(state))
        booking.clear_book_button()
        self.sender_patch = patch.object(voice, 'speak_step', return_value=True)
        self.sender = self.sender_patch.start()
        self.addCleanup(self.sender_patch.stop)
        self.action_patch = patch('app.services.device_action.execute_device_action', return_value=True)
        self.action = self.action_patch.start()
        self.addCleanup(self.action_patch.stop)
        self.lane_patch = patch.object(lane_announce, 'get_lane_announce')
        self.lane_patch.start()
        self.addCleanup(self.lane_patch.stop)
        self.network_patch = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.network_patch.start()
        self.addCleanup(self.network_patch.stop)
        self.addCleanup(booking.clear_book_button)

    def test_button_then_dialog_does_not_send_twice(self):
        booking.mark_book_button()
        self.assertTrue(booking.ensure_step2_announce_once())
        self.assertTrue(booking.ensure_step2_announce_once())
        self.sender.assert_called_once_with(2, interrupt=True, blocking=True)

    def test_new_button_starts_a_new_dispatch_round(self):
        booking.mark_book_button()
        booking.ensure_step2_announce_once()
        booking.mark_book_button()
        booking.ensure_step2_announce_once()
        self.assertEqual(self.sender.call_count, 2)

    def test_concurrent_dialog_calls_share_one_result(self):
        entered, release = threading.Event(), threading.Event()
        def dispatch(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return True
        self.sender.side_effect = dispatch
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(booking.ensure_step2_announce_once)
            self.assertTrue(entered.wait(1))
            second = executor.submit(booking.ensure_step2_announce_once)
            release.set()
            self.assertTrue(first.result(2))
            self.assertTrue(second.result(2))
        self.assertEqual(self.sender.call_count, 1)

    def test_dispatch_failure_is_not_reported_as_success(self):
        self.sender.return_value = False
        self.assertFalse(booking.ensure_step2_announce_once())
        self.assertFalse(booking.ensure_step2_announce_once())
        self.assertEqual(self.sender.call_count, 1)

    def test_talk_lock_is_not_stolen_or_released(self):
        lock = get_voice_channel()
        result = lock.acquire('talk')
        self.assertTrue(result['ok'])
        try:
            self.assertTrue(booking.ensure_step2_announce_once())
            self.assertEqual(lock.status()['owner'], 'talk')
            self.assertEqual(lock.status()['token'], result['token'])
        finally:
            lock.release(owner='talk', token=result['token'])

    def test_open_response_does_not_claim_playback_complete(self):
        app = Flask(__name__)
        with app.test_request_context('/api/booking/open', method='POST'):
            with patch.object(booking, 'DeviceManager') as manager:
                manager.return_value.health_check_all.return_value = True
                response = booking.open_booking_session.__wrapped__()
        data = response.get_json()['data']
        self.assertTrue(data['announceCommandSent'])
        self.assertFalse(data['announceDone'])

    def test_announcement_failure_does_not_drop_light_curtain_timestamp(self):
        lane = lane_announce.LaneAnnounceService()
        with patch.object(lane, '_play', return_value=False):
            with patch.object(booking, 'set_booking_state') as update:
                lane.on_light_curtain()
        update.assert_called_once()
        self.assertTrue(update.call_args.kwargs['openlightscreen_time'])


if __name__ == '__main__':
    unittest.main()
