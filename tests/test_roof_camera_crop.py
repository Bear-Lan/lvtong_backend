"""真实像素裁剪回归；模拟RTSP帧，不连接相机或现场设备。"""
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from app.services import capture_service as capture


class RoofCameraCropTest(unittest.TestCase):
    def snapshot(self, width, crop):
        frame = np.zeros((90, width, 3), dtype=np.uint8)
        frame[:, :width // 3] = (255, 0, 0)
        frame[:, width // 3:2 * width // 3] = (0, 255, 0)
        frame[:, 2 * width // 3:] = (0, 0, 255)
        original = frame.copy()
        camera = Mock()
        camera.isOpened.return_value = True
        camera.read.return_value = (True, frame)
        with patch.object(capture, '_load_camera_row', return_value={'ip_address': 'test.invalid'}), \
             patch.object(capture, 'ensure_capture_allowed'), \
             patch.object(capture.cv2, 'VideoCapture', return_value=camera):
            data = capture.snapshot_from_camera('camera-test', right_two_thirds=crop)
        camera.release.assert_called_once()
        np.testing.assert_array_equal(frame, original)
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)

    def test_roof_keeps_right_two_thirds_with_full_height(self):
        image = self.snapshot(300, True)
        self.assertEqual(image.shape, (90, 200, 3))
        self.assertGreater(int(image[45, 10, 1]), 240)  # 原图中段绿色在裁剪后左侧。
        self.assertGreater(int(image[45, -10, 2]), 240)  # 原图最右侧红色完整保留。

    def test_other_cameras_keep_original_dimensions(self):
        self.assertEqual(self.snapshot(300, False).shape, (90, 300, 3))

    def test_non_divisible_width_does_not_drop_right_edge(self):
        image = self.snapshot(301, True)
        self.assertEqual(image.shape, (90, 201, 3))
        self.assertGreater(int(image[45, -10, 2]), 240)
