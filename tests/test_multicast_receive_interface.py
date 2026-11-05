"""收包网卡可以与语音发令网卡分离，不创建实际套接字。"""
import unittest
from unittest.mock import patch
import config
from app.devices.radar import RadarReader


class MulticastReceiveInterfaceTest(unittest.TestCase):
    def test_explicit_receive_interface_and_legacy_fallback(self):
        for receive, expected in (('192.168.0.102', '192.168.0.102'), ('', '192.168.88.253')):
            radar = RadarReader('test-radar')
            with patch.object(config, 'MCAST_RECV_IFACE_IP', receive), \
                 patch.object(config, 'MCAST_IFACE_IP', '192.168.88.253'), \
                 patch.object(config, 'RADAR_TRANSPORT', 'multicast'), \
                 patch.object(radar, '_start_multicast_listen', return_value=True) as listen:
                self.assertTrue(radar.initialize())
            listen.assert_called_once_with(config.MCAST_GROUP, config.MCAST_PORT, expected)


if __name__ == '__main__':
    unittest.main()
