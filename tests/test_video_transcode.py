import copy
import subprocess
import shlex
import unittest
from unittest.mock import patch

from app.services.video_transcode import build_transcode_config, choose_encoder, transcode_args, configure_webrtc_hosts


class VideoTranscodeTest(unittest.TestCase):
    def base(self):
        return {'rtspAddress': ':8554', 'pathDefaults': {'sourceOnDemand': True},
                'webrtcAdditionalHosts': ['100.69.226.19'],
                'paths': {f'cam{i}': {'source': f'rtsp://user:secret@camera{i}/101',
                                     'rtspTransport': 'tcp'} for i in range(1, 7)}}

    def test_all_six_paths_become_shared_on_demand_h264_publishers(self):
        base = self.base()
        before = copy.deepcopy(base)
        result = build_transcode_config(base, r'C:\Program Files\ffmpeg.exe', 'libx264')
        self.assertEqual(base, before)
        self.assertEqual(result['webrtcAdditionalHosts'], before['webrtcAdditionalHosts'])
        for i in range(1, 7):
            name = f'cam{i}'
            out, raw = result['paths'][name], result['paths'][name + '_input']
            self.assertEqual(raw['source'], before['paths'][name]['source'])
            self.assertTrue(raw['sourceOnDemand'])
            self.assertEqual(out['source'], 'publisher')
            self.assertFalse(out['sourceOnDemand'])
            self.assertTrue(out['runOnDemandRestart'])
            self.assertEqual(out['runOnDemandCloseAfter'], '10s')
            self.assertNotIn('secret', out['runOnDemand'])
            self.assertIn('libx264', out['runOnDemand'])
            self.assertIn(f'rtsp://127.0.0.1:8554/{name}_input', out['runOnDemand'])
            args = shlex.split(out['runOnDemand'])
            self.assertEqual(args[0], r'C:\Program Files\ffmpeg.exe')
            self.assertEqual(args[args.index('-vf') + 1], "fps=15,scale=w='min(1280,iw)':h=-2,format=yuv420p")

    def test_unsupported_source_or_conflicting_private_path_fails_closed(self):
        for source in (None, 'publisher', 'http://invalid/'):
            base = self.base()
            base['paths']['cam2']['source'] = source
            with self.assertRaises(ValueError):
                build_transcode_config(base, 'ffmpeg', 'libx264')
        base = self.base()
        base['paths']['cam2_input'] = {}
        with self.assertRaises(ValueError):
            build_transcode_config(base, 'ffmpeg', 'libx264')

    def test_encoder_selection_probes_driver_and_falls_back_to_cpu(self):
        with patch.dict('os.environ', {'VIDEO_ENCODER': 'auto'}):
            with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
                self.assertEqual(choose_encoder('ffmpeg'), 'h264_nvenc')
            for error in (OSError(), subprocess.TimeoutExpired('ffmpeg', 12)):
                with patch('subprocess.run', side_effect=error):
                    self.assertEqual(choose_encoder('ffmpeg'), 'libx264')
            with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 1)):
                self.assertEqual(choose_encoder('ffmpeg'), 'libx264')

    def test_explicit_hardware_failure_is_not_reported_as_success(self):
        with patch.dict('os.environ', {'VIDEO_ENCODER': 'h264_nvenc'}):
            with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaises(RuntimeError):
                    choose_encoder('ffmpeg')

    def test_low_latency_browser_codec_and_bounded_resources(self):
        for encoder in ('libx264', 'h264_nvenc'):
            args = transcode_args('ffmpeg', 'rtsp://input', 'rtsp://output', encoder=encoder)
            for option, value in (('-profile:v', 'baseline'), ('-bf', '0'), ('-g', '15'),
                                  ('-rtsp_transport', 'tcp'), ('-c:v', encoder)):
                self.assertEqual(args[args.index(option) + 1], value)
            self.assertIn('format=yuv420p', args[args.index('-vf') + 1])
            self.assertIn('-an', args)
            self.assertNotIn('copy', args)
        for width, fps in ((319, 15), (1281, 15), (1280, 0), (1280, 31)):
            with self.assertRaises(ValueError):
                transcode_args('ffmpeg', 'a', 'b', encoder='libx264', width=width, fps=fps)

    def test_explicit_site_ipv4_hosts_avoid_unusable_ipv6_candidates(self):
        config = self.base()
        configure_webrtc_hosts(config, '')
        self.assertEqual(config, self.base())
        configure_webrtc_hosts(config, '192.168.88.253, 100.69.226.19,100.69.226.19')
        self.assertFalse(config['webrtcIPsFromInterfaces'])
        self.assertEqual(config['webrtcAdditionalHosts'], ['192.168.88.253', '100.69.226.19'])
        self.assertEqual(config['webrtcLocalUDPAddress'], '0.0.0.0:8189')
        self.assertEqual(config['webrtcLocalTCPAddress'], '0.0.0.0:8189')
        self.assertEqual(config['udpMaxPayloadSize'], 1180)
        for invalid in ('::1', '1.2.3.999', '$(cmd)', ','):
            with self.assertRaises(ValueError):
                configure_webrtc_hosts({}, invalid)


if __name__ == '__main__':
    unittest.main()
