"""中间件语音状态位解析；不创建网络或调用设备。"""
import unittest
from unittest.mock import patch

from app.devices.radar import RadarReader
from app.websocket import handler


class VoiceStatusBitsTest(unittest.TestCase):
    def test_front_and_back_bits_are_independent(self):
        for mask, front, back in ((0, False, False), (0x10000, True, False),
                                  (0x20000, False, True), (0x30000, True, True)):
            with self.subTest(mask=mask):
                parsed = RadarReader.parse_dev_bits(mask)
                self.assertIs(parsed['frontVoiceAnnounceStatus'], front)
                self.assertIs(parsed['backVoiceAnnounceStatus'], back)

    def test_sound_alarm_is_separate_from_voice_playback(self):
        parsed = RadarReader.parse_dev_bits(1 << 15)
        self.assertTrue(parsed['soundalarmCmd'])
        self.assertFalse(parsed['frontVoiceAnnounceStatus'])
        self.assertFalse(parsed['backVoiceAnnounceStatus'])

    def test_voice_bits_do_not_set_other_device_signals(self):
        parsed = RadarReader.parse_dev_bits(0x30000)
        self.assertFalse(parsed['bookingStatus'])
        self.assertFalse(parsed['urgentStopStatus'])
        for field in ('lightSource200Status', 'lightSource160Status',
                      'lightGate200Status', 'lightGate160Status'):
            self.assertFalse(parsed[field])

    def test_status_transitions_are_forwarded_and_cached_for_late_clients(self):
        with patch.object(handler, '_last_plc_data', None), patch.object(handler.socketio, 'emit') as emit:
            for mask in (0x10000, 0x30000, 0x20000, 0):
                parsed = RadarReader.parse_dev_bits(mask)
                handler.push_plc_status(parsed=parsed)
                payload = emit.call_args.args[1]
                self.assertEqual(payload['type'], 'plc_status')
                for field in ('frontVoiceAnnounceStatus', 'backVoiceAnnounceStatus'):
                    self.assertIs(payload['data'][field], parsed[field])
                    self.assertIs(handler.get_last_plc_status()[field], parsed[field])

    def test_device_voice_feedback_comes_from_first_field_as_verified_onsite(self):
        for cmd in (0, 0x10000, 0x20000, 0x30000):
            for status in (0, 0x10000, 0x20000, 0x30000):
                with self.subTest(cmd=cmd, status=status):
                    parsed = RadarReader.parse_ntrmc_dev(f'$NTRMC,DEVICE,{cmd:08x},{status:08x}')
                    self.assertIs(parsed['frontVoiceAnnounceStatus'], bool(cmd & 0x10000))
                    self.assertIs(parsed['backVoiceAnnounceStatus'], bool(cmd & 0x20000))

    def test_all_ones_second_field_does_not_claim_both_speakers_are_playing(self):
        for cmd in (0, 0x30000):
            for voice in (0, 0x10000, 0x20000, 0x30000):
                status = voice | 0xffff
                with self.subTest(cmd=cmd, status=status):
                    parsed = RadarReader.parse_ntrmc_dev(f'$NTRMC,DEVICE,{cmd:08x},{status:08x}')
                    self.assertNotIn('statusUnreliable', parsed)
                    self.assertEqual(parsed['onlineRaw'], status)
                    self.assertIs(parsed['frontVoiceAnnounceStatus'], bool(cmd & 0x10000))
                    self.assertIs(parsed['backVoiceAnnounceStatus'], bool(cmd & 0x20000))
                    self.assertFalse(parsed['urgentStopStatus'])
                    self.assertFalse(parsed['bookingStatus'])

    def test_real_site_sequence_is_cached_and_forwarded_without_false_booking(self):
        radar = RadarReader('test-radar')
        radar.set_plc_status_callback(lambda parsed: handler.push_plc_status(parsed=parsed))
        with patch.object(handler, '_last_plc_data', None), patch.object(handler.socketio, 'emit') as emit:
            for word, front, back in ((0x80c2, False, False), (0x180c2, True, False),
                                      (0x80c2, False, False), (0x280c2, False, True),
                                      (0x80c2, False, False), (0x380c2, True, True),
                                      (0x80c2, False, False)):
                radar._dispatch(f'$NTRMC,DEVICE,{word:08x},001fffff')
                data = handler.get_last_plc_status()
                self.assertEqual((data['frontVoiceAnnounceStatus'], data['backVoiceAnnounceStatus']), (front, back))
                self.assertFalse(data['bookingStatus'])
            self.assertEqual(emit.call_count, 7)

    def test_appointment_is_first_field_bit5_independent_of_voice_and_second_word(self):
        for voice in (0, 0x10000, 0x20000, 0x30000):
            for status in (0, 0x20, 0x1fffff):
                for pressed in (False, True):
                    word = 0x80c2 | voice | (0x20 if pressed else 0)
                    parsed = RadarReader.parse_ntrmc_dev(f'$NTRMC,DEVICE,{word:08x},{status:08x}')
                    self.assertIs(parsed['bookingStatus'], pressed)

    def test_single_status_word_and_bad_packets(self):
        parsed = RadarReader.parse_ntrmc_dev('$NTRMC,DEVICE,00030000')
        self.assertTrue(parsed['frontVoiceAnnounceStatus'])
        self.assertTrue(parsed['backVoiceAnnounceStatus'])
        self.assertIsNone(RadarReader.parse_ntrmc_dev('$NTRMC,DEVICE,not-hex'))


if __name__ == '__main__':
    unittest.main()
