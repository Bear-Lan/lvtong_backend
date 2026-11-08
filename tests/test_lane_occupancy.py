"""车道占用判定离线测试；不连接现场设备。"""
import unittest
from unittest.mock import call, patch

from app.services.scheduler import DistanceScheduler


class LaneOccupancyTest(unittest.TestCase):
    def setUp(self):
        self.scheduler = DistanceScheduler()
        self.scheduler._is_detection = True
        self.scheduler._gate_released = False
        self.scheduler._gate_release_min_distance = 21.0
        self.scheduler._lane_state_debounce_frames = 3
        self.scheduler._release_clear_count = 0
        self.scheduler._reset_lane_occupied_state()

    def booking_patches(self):
        return (
            patch('app.api.booking.get_booking_state', return_value={'check_step': 1}),
            patch('app.api.booking.set_booking_state'),
            patch('app.services.scheduler.push_lane_occupied'),
        )

    def test_object_count_two_at_safe_distance_is_not_occupied(self):
        get_state, set_state, push = self.booking_patches()
        with get_state, set_state, push as push_occupied:
            for _ in range(4):
                self.scheduler._try_release_after_accept(2, 30.9)

        self.assertEqual(self.scheduler._release_clear_count, 4)
        push_occupied.assert_not_called()

    def test_occupied_and_clear_both_require_three_frames(self):
        get_state, set_state, push = self.booking_patches()
        with get_state, set_state, push as push_occupied:
            for _ in range(3):
                self.scheduler._try_release_after_accept(1, 20.0)
            for _ in range(3):
                self.scheduler._try_release_after_accept(2, 30.9)

        self.assertEqual(push_occupied.call_args_list, [call(True), call(False)])

    def test_zero_objects_is_unknown_and_does_not_release(self):
        self.scheduler._lane_occupied_active = True
        get_state, set_state, push = self.booking_patches()
        with get_state, set_state, push as push_occupied:
            self.scheduler._try_release_after_accept(0, 0.0)

        self.assertTrue(self.scheduler._lane_occupied_active)
        self.assertEqual(self.scheduler._release_clear_count, 0)
        push_occupied.assert_not_called()


if __name__ == '__main__':
    unittest.main()
