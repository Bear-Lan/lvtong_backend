"""27米/零距离连续五帧放行；所有后续动作和网络隔离。"""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

from app.devices.radar import RadarReader
from app.services.scheduler import DistanceScheduler


class LaneOccupancyTest(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(DistanceScheduler, '_instance', None),
            patch('socket.socket', side_effect=AssertionError('network forbidden')),
            patch('app.api.booking.get_booking_state', return_value={'check_step': 1}),
            patch('app.api.booking.set_booking_state'),
            patch('app.services.scheduler.push_lane_occupied'),
            patch('app.services.scheduler.push_radar_distance'),
            patch('app.services.scheduler.push_detection_step'),
            patch('app.services.scheduler.threading.Thread', side_effect=lambda target, **kw: SimpleNamespace(start=target)),
            patch('app.services.device_action.execute_device_action', return_value=True),
            patch('app.services.geometry_capture.start_geometry_capture', return_value=True),
            patch('app.services.body_camera_fetch.trigger_body_camera_capture'),
            patch.object(DistanceScheduler, '_trigger_xray_start_scan'),
        ]
        mocks=[]
        for item in self.patches:
            mocks.append(item.start());self.addCleanup(item.stop)
        self.set_state=mocks[3];self.push=mocks[4];self.actions=mocks[8]
        self.geometry=mocks[9];self.body=mocks[10];self.xray=mocks[11]
        self.scheduler=DistanceScheduler()
        config=json.loads((Path(__file__).parents[1]/'config/scheduler_config.json').read_text(encoding='utf8'))
        self.scheduler.load_configuration_from_json(config)
        self.scheduler.set_detection_state(True)
        self.scheduler.start()

    def frame(self, distance, count=1, intrusion=None):
        self.scheduler._try_release_after_accept(count,distance,intrusion=intrusion)

    def assert_waiting(self, count):
        self.assertFalse(self.scheduler._gate_released)
        self.assertTrue(self.scheduler.capture_blocked)
        self.assertEqual(self.scheduler._release_clear_count,count)
        self.actions.assert_not_called();self.geometry.assert_not_called()
        self.body.assert_not_called();self.xray.assert_not_called()
        self.assertNotIn(call(False),self.push.call_args_list)

    def assert_released_once(self):
        self.assertTrue(self.scheduler._gate_released)
        self.assertFalse(self.scheduler.capture_blocked)
        self.assertEqual(self.actions.call_args_list,[
            call('plc_001','setPLC',{'redlight':False,'yellowlight':False,'greenlight':True}),
            call('gate_001','open')])
        self.geometry.assert_called_once();self.body.assert_called_once();self.xray.assert_called_once()
        self.assertEqual(self.push.call_args_list,[call(True),call(False)])
        self.set_state.assert_any_call(check_step=2,radar_count=0,opengate_time=unittest.mock.ANY)

    def test_initially_occupied_and_exact_boundary_releases_only_on_fifth_frame(self):
        self.assert_waiting(0)
        for n in range(1,5):
            self.frame(27,count=2);self.assert_waiting(n)
        self.frame(27,count=2);self.assert_released_once()
        for distance in (0,27,40,20):self.frame(distance)
        self.assert_released_once()

    def test_zero_means_infinite_even_with_zero_objects(self):
        for n in range(1,5):
            self.frame(0,count=0);self.assert_waiting(n)
        self.frame(0,count=0);self.assert_released_once()

    def test_zero_and_far_frames_can_mix_and_object_count_does_not_gate_release(self):
        for distance,count in [(27,0),(0,2),(40,0),(0,0)]:self.frame(distance,count)
        self.assert_waiting(4)
        self.frame(27.001,3);self.assert_released_once()

    def test_near_negative_and_invalid_frames_restart_full_five_frame_confirmation(self):
        for bad in (26.999,21,1,-1,float('nan'),float('inf')):
            with self.subTest(bad=bad):
                for _ in range(4):self.frame(30)
                self.frame(bad);self.assert_waiting(0)
        for _ in range(4):self.frame(27)
        self.assert_waiting(4)
        self.frame(0,0);self.assert_released_once()

    def test_explicit_legacy_intrusion_cannot_release_despite_far_distance(self):
        for _ in range(4):self.frame(30)
        self.frame(30,intrusion=True);self.assert_waiting(0)

    def test_zero_radar_packets_never_run_arrival_thresholds_or_tail_capture(self):
        radar=RadarReader('five-frame-test');radar.set_distance_callback(self.scheduler.on_radar_data)
        with patch.object(self.scheduler,'_process_distance_update') as thresholds, patch.object(self.scheduler,'_try_tail_capture') as tail:
            for n in range(4):
                radar._dispatch('$VMRMC,123456,0,0,0');self.assert_waiting(n+1)
            radar._dispatch('$VMRMC,123456,0,0,0');self.assert_released_once()
        thresholds.assert_not_called();tail.assert_not_called()

    def test_nonfinite_packet_breaks_consecutive_clear_streak_without_broadcast(self):
        for _ in range(4):self.frame(27)
        self.scheduler.on_radar_data(float('nan'))
        self.assert_waiting(0)
        for _ in range(4):self.frame(27)
        self.assert_waiting(4)

    def test_fifth_clear_frame_does_not_also_run_downstream_distance_actions(self):
        with patch.object(self.scheduler, '_process_distance_update') as thresholds, patch.object(self.scheduler, '_try_tail_capture') as tail:
            for _ in range(4):self.scheduler.on_radar_data(27)
            self.assert_waiting(4)
            self.scheduler.on_radar_data(27)
            self.assert_released_once()
            thresholds.assert_not_called();tail.assert_not_called()
            self.scheduler.on_radar_data(28)
            thresholds.assert_called_once_with(28);tail.assert_called_once_with(28)

    def test_other_booking_steps_and_completed_release_do_not_start_another_release(self):
        with patch('app.api.booking.get_booking_state',return_value={'check_step':2}):
            for _ in range(8):self.frame(0,0)
        self.assert_waiting(0)


if __name__=='__main__':unittest.main()
