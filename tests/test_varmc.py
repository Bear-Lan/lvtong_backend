"""VARMC三段样例、现场四段报文与VMRMC互不污染。"""
import unittest
from app.devices.radar import RadarReader


class VarmcTest(unittest.TestCase):
    def test_user_and_captured_site_packets(self):
        for packet in ('$VARMC,100184.818,1.23', '$VARMC,268152.699,3.257,433'):
            parsed = RadarReader.parse_varmc(packet)
            self.assertIsNotNone(parsed)
            self.assertGreater(parsed['timestamp'], 0)
        self.assertEqual(RadarReader.parse_varmc('$VARMC,100184.818,1.23')['value'], 1.23)

    def test_malformed_or_nonfinite_packets_do_not_trigger(self):
        radar = RadarReader('varmc-malformed')
        received = []
        radar.set_idle_announce_callback(received.append)
        for packet in ('$VARMC', '$VARMC,1', '$VARMC,nan,1', '$VARMC,1,inf',
                       '$VARMC,1,', '$VARMCX,1,2', '$VARMC,1,2,bad',
                       '$VARMC,1,2,3,4', '$VARMC,1,2,nan'):
            radar._dispatch(packet)
        self.assertEqual(received, [])

    def test_varmc_does_not_change_position_speed_or_target_count(self):
        radar = RadarReader('varmc-independent')
        positions, received = [], []
        radar.set_distance_callback(lambda *args: positions.append(args))
        radar.set_idle_announce_callback(received.append)
        radar._dispatch('$VMRMC,100183,2,30,4')
        radar._dispatch('$VARMC,100184.818,1.23')
        self.assertEqual((radar.distance, radar.speed, radar._object_count), (30, 4, 2))
        self.assertEqual(positions, [(30, 1, 2)])
        self.assertEqual(received, [{'timestamp': 100184.818, 'value': 1.23}])


if __name__ == '__main__':
    unittest.main()
