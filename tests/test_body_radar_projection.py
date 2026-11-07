"""同帧车顶/车侧投影、实时优先及示范来源回归；不连接设备。"""
from pathlib import Path
import base64
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from flask import Flask
from requests import Response

from app.api.device import device_api
from app.extensions.auth import create_token
from app.services import body_radar_projection as service, radar_point_cloud, temp_image_cache
from test_radar_point_cloud import cloud_bytes


class BodyProjectionTest(unittest.TestCase):
    def setUp(self):
        self.settings = {'RADAR_POINT_CLOUD_URL': 'http://test/geom/laz',
                         'RADAR_POINT_CLOUD_SAMPLE_FALLBACK': True,
                         'RADAR_LAZ_METERS_PER_UNIT': 1, 'RADAR_LAZ_RESOLUTION': .01}
        self.addCleanup(temp_image_cache._CACHE.clear)

    @staticmethod
    def response(data):
        response = Response(); response.status_code = 200; response._content = data
        return response

    def test_user_sample_projects_xy_and_xz_from_one_decode(self):
        data = service.SAMPLE_PATH.read_bytes()
        with patch.object(radar_point_cloud, '_read_las', wraps=radar_point_cloud._read_las) as read:
            frames = radar_point_cloud.decode_body_projection_pair(data)
        self.assertEqual(read.call_count, 1)
        for view, axes, expected_shape in [('top', 'xy', (201, 442)), ('side', 'xz', (182, 442))]:
            frame = frames[view]
            image = cv2.imdecode(np.frombuffer(frame.image, np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(image.shape[:2], expected_shape)
            self.assertLess(image.min(), 255)
            self.assertEqual(frame.metadata['pointCount'], 48852)
            self.assertEqual(frame.metadata['coordinateAxes'], axes)
            self.assertEqual(frame.metadata['metersPerPixel'], .01)

    def test_resolution_limit_is_shared_by_both_views(self):
        frames = radar_point_cloud.decode_body_projection_pair(cloud_bytes(), {
            'RADAR_LAZ_METERS_PER_UNIT': 100, 'RADAR_LAZ_RESOLUTION': .0001})
        self.assertEqual(frames['top'].metadata['metersPerPixel'], frames['side'].metadata['metersPerPixel'])
        for frame in frames.values():
            image = cv2.imdecode(np.frombuffer(frame.image, np.uint8), cv2.IMREAD_COLOR)
            self.assertLessEqual(max(image.shape), 4096)

    def test_three_dimensional_coordinates_keep_exact_full_measurements(self):
        _, geometry = radar_point_cloud.decode_body_radar_data(service.SAMPLE_PATH.read_bytes())
        self.assertEqual(geometry['pointCount'], 48852)
        self.assertAlmostEqual(geometry['dimensionsMeters']['length'], 4.404)
        self.assertAlmostEqual(geometry['dimensionsMeters']['width'], 1.992)
        self.assertAlmostEqual(geometry['dimensionsMeters']['height'], 1.810)
        xyz = np.frombuffer(base64.b64decode(geometry['positions']), dtype='<f4').reshape(-1, 3)
        self.assertTrue(np.isfinite(xyz).all())
        np.testing.assert_allclose(xyz.min(axis=0) + geometry['originMeters'], geometry['boundsMeters']['min'], atol=1e-6)
        np.testing.assert_allclose(xyz.max(axis=0) + geometry['originMeters'], geometry['boundsMeters']['max'], atol=1e-6)

    def test_display_sampling_preserves_dimensions_and_large_coordinate_precision(self):
        with patch.object(radar_point_cloud, 'MAX_RENDER_POINTS', 2):
            _, geometry = radar_point_cloud.decode_body_radar_data(cloud_bytes())
        self.assertEqual(geometry['pointCount'], 2)
        self.assertEqual(geometry['totalPointCount'], 4)
        self.assertEqual(geometry['dimensionsMeters'], {'length': 4., 'width': 2., 'height': 2.})
        xyz = np.frombuffer(base64.b64decode(geometry['positions']), dtype='<f4').reshape(-1, 3)
        self.assertLessEqual(abs(xyz).max(), 2.)

    def test_live_cloud_is_used_instead_of_sample(self):
        with patch.object(service, 'middleware_get_payload', return_value=self.response(cloud_bytes())) as fetch, \
             patch.object(service, '_run_in_pool', side_effect=lambda f: f()), \
             patch.object(Path, 'read_bytes', side_effect=AssertionError('sample must not be read')):
            result = service.fetch_body_projections(self.settings)
        self.assertEqual(fetch.call_args.args[0], self.settings['RADAR_POINT_CLOUD_URL'])
        self.assertEqual(result['source'], 'live')
        self.assertEqual(result['pointCloud']['pointCount'], 4)
        self.assertEqual(set(result['views']), {'top', 'side'})

    def test_empty_live_cloud_returns_labelled_sample_and_can_be_disabled(self):
        with patch.object(service, 'middleware_get_payload', return_value=self.response(b'{"status":"false"}')), \
             patch.object(service, '_run_in_pool', side_effect=lambda f: f()):
            result = service.fetch_body_projections(self.settings)
            self.assertEqual(result['source'], 'sample')
            self.assertEqual(result['sourceLabel'], '示范数据')
            self.assertIn('尚未生成', result['fallbackReason'])
            for view in result['views'].values():
                entry = temp_image_cache.get(temp_image_cache.parse_temp_id(view['imageUrl']))
                self.assertIsNotNone(entry)
                self.assertEqual(entry.mime, 'image/png')
            self.settings['RADAR_POINT_CLOUD_SAMPLE_FALLBACK'] = False
            with self.assertRaises(ValueError):
                service.fetch_body_projections(self.settings)

    def test_authenticated_api_returns_matching_pair_without_device_actions(self):
        app = Flask(__name__); app.register_blueprint(device_api)
        app.config.update(self.settings)
        client = app.test_client()
        self.assertEqual(client.post('/api/device/body-radar-image', json={'view': 'side'}).status_code, 401)
        auth = {'Authorization': 'Bearer ' + create_token({'username': 'projection_test'})}
        self.assertEqual(client.post('/api/device/body-radar-image', json={'view': 'bad'}, headers=auth).status_code, 400)
        with patch.object(service, 'middleware_get_payload', return_value=self.response(cloud_bytes())), \
             patch.object(service, '_run_in_pool', side_effect=lambda f: f()), \
             patch('socket.socket', side_effect=AssertionError('hardware forbidden')):
            response = client.post('/api/device/body-radar-image', json={'view': 'side'}, headers=auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['data']['imageUrl'], response.json['data']['views']['side']['imageUrl'])


if __name__ == '__main__':
    unittest.main()
