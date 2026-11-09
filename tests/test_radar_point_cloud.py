"""真实 LAS/LAZ 解码、HTTP 边界与预约响应回归；不连接现场硬件。"""
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import cv2
import laspy
import numpy as np
from flask import Flask
from requests import Response

from app.api import booking
from app.extensions.auth import create_token
from app.services import middleware_http
from app.services.radar_point_cloud import decode_radar_payload
from app.services import temp_image_cache


def cloud_bytes(compressed=True, shift=0., version='1.2', point_format=0):
    header = laspy.LasHeader(point_format=point_format, version=version)
    header.scales = np.array([.001, .002, .005])
    header.offsets = np.array([500000, 4000000, 100])
    cloud = laspy.LasData(header)
    cloud.x = np.array([500000., 500004., 500004., 500000.]) + shift
    cloud.y = np.array([4000000., 4000000., 4000002., 4000002.])
    cloud.z = np.array([100., 100., 102., 102.])
    stream = io.BytesIO()
    cloud.write(stream, do_compress=compressed)
    return stream.getvalue()


class PointCloudTest(unittest.TestCase):
    def test_real_compressed_laz_applies_scales_offsets_and_projection(self):
        for compressed, version, fmt in [(False, '1.2', 0), (True, '1.2', 3), (True, '1.4', 6)]:
            with self.subTest(compressed=compressed, version=version):
                frame = decode_radar_payload(cloud_bytes(compressed, version=version, point_format=fmt),
                                             {'Image-Envelope': 'old', 'Vehicle-Height': '3.2'})
                self.assertEqual(frame.metadata['sourceFormat'], 'laz' if compressed else 'las')
                self.assertEqual(frame.metadata['pointCount'], 4)
                self.assertEqual(frame.metadata['boundsMeters']['min'], [500000, 4000000, 100])
                self.assertEqual(frame.metadata['boundsMeters']['max'], [500004, 4000002, 102])
                image = cv2.imdecode(np.frombuffer(frame.image, np.uint8), cv2.IMREAD_COLOR)
                self.assertEqual(image.shape[:2], (201, 401))
                self.assertTrue(np.all(image[0, 0] < 255))
                self.assertTrue(np.all(image[-1, -1] < 255))
                self.assertEqual(frame.headers['Image-Resolution'], '0.01')
                self.assertEqual(frame.headers['Vehicle-Height'], '3.2')
                self.assertNotIn('Image-Envelope', frame.headers)

    def test_units_axes_and_resolution_limit_are_consistent(self):
        frame = decode_radar_payload(cloud_bytes(), settings={
            'RADAR_LAZ_AXES': 'yx', 'RADAR_LAZ_METERS_PER_UNIT': 100,
            'RADAR_LAZ_RESOLUTION': .0001,
        })
        image = cv2.imdecode(np.frombuffer(frame.image, np.uint8), cv2.IMREAD_COLOR)
        self.assertLessEqual(max(image.shape[:2]), 4096)
        mpp = float(frame.headers['Image-Resolution'])
        self.assertAlmostEqual((image.shape[0] - 1) * mpp, 400)
        self.assertLess(abs((image.shape[1] - 1) * mpp - 200), mpp)
        self.assertNotIn('Vehicle-Height', frame.headers)

    def test_empty_corrupt_truncated_and_oversized_inputs_fail(self):
        empty = laspy.LasData(laspy.LasHeader(point_format=0, version='1.2'))
        out = io.BytesIO(); empty.write(out)
        raw = cloud_bytes(False)
        for data in [b'', b'{"error":"","status":"false"}', b'<html>bad</html>',
                     b'LASFbroken', out.getvalue(), raw[:-10], cloud_bytes()[:240]]:
            with self.subTest(length=len(data)):
                with self.assertRaises(ValueError):
                    decode_radar_payload(data)
        with patch('app.services.radar_point_cloud.MAX_POINTS', 2):
            with self.assertRaisesRegex(ValueError, '点数'):
                decode_radar_payload(raw)
        for settings in [{'RADAR_LAZ_AXES': 'xx'}, {'RADAR_LAZ_METERS_PER_UNIT': float('nan')},
                         {'RADAR_LAZ_RESOLUTION': 0}]:
            with self.assertRaises(ValueError):
                decode_radar_payload(raw, settings=settings)

    def test_sampling_limits_points_without_changing_full_bounds(self):
        with patch('app.services.radar_point_cloud.MAX_RENDER_POINTS', 2):
            frame = decode_radar_payload(cloud_bytes())
        self.assertLessEqual(frame.metadata['renderedPointCount'], 2)
        self.assertEqual(frame.metadata['pointCount'], 4)
        self.assertEqual(frame.metadata['boundsMeters']['max'], [500004, 4000002, 102])


class RadarHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/empty':
                    body = b'{"error":"","status":"false"}'
                    extra = 14
                elif self.path == '/truncated':
                    body, extra = cloud_bytes()[:-10], 10
                else:
                    body, extra = cloud_bytes(), 0
                self.send_response(200)
                self.send_header('Content-Length', str(len(body) + extra))
                self.send_header('Connection', 'close')
                self.end_headers(); self.wfile.write(body)
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.worker.join()

    def test_actual_http_length_bug_is_only_tolerated_for_complete_empty_json(self):
        url = f'http://127.0.0.1:{self.server.server_port}'
        with patch.object(middleware_http, '_run_in_pool', side_effect=lambda fn: fn()):
            response = middleware_http.middleware_get_payload(url + '/empty')
            with self.assertRaisesRegex(ValueError, '尚未生成'):
                decode_radar_payload(response.content)
            with self.assertRaisesRegex(ValueError, '不完整'):
                middleware_http.middleware_get_payload(url + '/truncated')
            good = middleware_http.middleware_get_payload(url + '/good')
            self.assertEqual(decode_radar_payload(good.content).metadata['pointCount'], 4)
            with self.assertRaisesRegex(ValueError, '限制'):
                middleware_http.middleware_get_payload(url + '/good', max_bytes=16)


class BookingRadarTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(booking.booking_api)
        self.client = self.app.test_client()
        self.auth = {'Authorization': 'Bearer ' + create_token({'username': 'test'})}
        self.state = dict(booking.get_booking_state())
        self.addCleanup(lambda: booking.get_booking_state().update(self.state))
        self.addCleanup(temp_image_cache._CACHE.clear)

    @staticmethod
    def response(data):
        response = Response(); response.status_code = 200; response._content = data
        return response

    def test_endpoint_renders_laz_and_refresh_never_reuses_previous_cloud(self):
        raw = [cloud_bytes(), cloud_bytes(shift=1.)]
        with patch.object(middleware_http, 'middleware_get_payload', side_effect=[self.response(x) for x in raw]), \
             patch('app.services.radar_point_cloud.decode_radar_payload_in_pool', side_effect=decode_radar_payload), \
             patch('socket.socket', side_effect=AssertionError('network forbidden')):
            results = [self.client.get('/api/booking/radar-image', headers=self.auth) for _ in raw]
        for result in results:
            self.assertEqual(result.status_code, 200)
            body = result.json['data']
            self.assertEqual(body['originalImageWidth'], 401)
            self.assertEqual(body['pointCloud']['sourceFormat'], 'laz')
            self.assertEqual(body['metersPerPixel'], .01)
            image = temp_image_cache.get(temp_image_cache.parse_temp_id(body['imageUrl']))
            self.assertIsNotNone(image)
        self.assertNotEqual(results[0].json['data']['imageUrl'], results[1].json['data']['imageUrl'])
        self.assertNotEqual(results[0].json['data']['pointCloud']['boundsMeters'],
                            results[1].json['data']['pointCloud']['boundsMeters'])

    def test_empty_response_returns_clear_failure_and_no_image(self):
        with patch.object(middleware_http, 'middleware_get_payload', return_value=self.response(b'{"status":"false"}')), \
             patch('app.services.radar_point_cloud.decode_radar_payload_in_pool', side_effect=decode_radar_payload), \
             patch.object(booking.time, 'sleep'):
            result = self.client.get('/api/booking/radar-image', headers=self.auth)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json['data']['imageUrl'], '')
        self.assertIn('尚未生成有效点云', result.json['message'])

    def test_appointment_image_preserves_measurement_headers(self):
        image = np.full((240, 800, 3), 255, np.uint8)
        image[40:220, 100:650] = (120, 160, 200)
        encoded, png = cv2.imencode('.png', image)
        self.assertTrue(encoded)
        response = self.response(png.tobytes())
        response.headers.update({'Content-Type': 'image/png', 'Vehicle-Height': '3.73',
                                 'Vehicle-SX': '1.08', 'Image-Resolution': '0.01'})
        url = 'http://192.168.88.201:5001/appt/image'
        self.app.config['RADAR_HEAD_URL'] = url
        with patch.object(middleware_http, 'middleware_get_payload', return_value=response) as fetch, \
             patch('app.services.radar_point_cloud.decode_radar_payload_in_pool', side_effect=decode_radar_payload), \
             patch('socket.socket', side_effect=AssertionError('network forbidden')):
            result = self.client.get('/api/booking/radar-image', headers=self.auth)
        self.assertEqual(fetch.call_args.args[0], url)
        self.assertEqual(result.status_code, 200)
        data = result.json['data']
        self.assertEqual((data['originalImageWidth'], data['originalImageHeight']), (800, 240))
        self.assertEqual(data['vehicleHeight'], 3.73)
        self.assertEqual(data['vehicleSx'], 1.08)
        self.assertEqual(data['metersPerPixel'], .01)
        self.assertEqual(data['contentOriginX'], 108)
        self.assertEqual(data['pointCloud']['sourceFormat'], 'image')
        preview = temp_image_cache.get(temp_image_cache.parse_temp_id(data['imageUrl']))
        self.assertIsNotNone(preview)

    def test_invalid_measurements_do_not_replace_existing_height(self):
        encoded, png = cv2.imencode('.png', np.full((240, 800, 3), 255, np.uint8))
        self.assertTrue(encoded)
        booking.get_booking_state()['car_height'] = 3.2
        for height, sx, resolution in [('-7.06e-311', '6.25e-310', '.01'),
                                        ('nan', 'inf', 'nan'), ('inf', '-1', 'inf'),
                                        ('0', '9', '.01')]:
            with self.subTest(height=height, sx=sx):
                response = self.response(png.tobytes())
                response.headers.update({'Vehicle-Height': height, 'Vehicle-SX': sx,
                                         'Image-Resolution': resolution})
                with patch.object(middleware_http, 'middleware_get_payload', return_value=response), \
                     patch('app.services.radar_point_cloud.decode_radar_payload_in_pool', side_effect=decode_radar_payload):
                    result = self.client.get('/api/booking/radar-image', headers=self.auth)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json['data']['vehicleHeight'], 3.2)
                self.assertIsNone(result.json['data']['vehicleSx'])
                self.assertEqual(result.json['data']['metersPerPixel'], .01)

    def test_cropped_appointment_image_converts_world_sx_to_image_coordinates(self):
        image = np.full((500, 800, 3), 255, np.uint8)
        image[50:450, 524:540] = (200, 0, 200)
        _, png = cv2.imencode('.png', image)
        response = self.response(png.tobytes())
        response.headers.update({'Vehicle-SX': '9.24', 'Image-Envelope': '4,0,8,5',
                                 'Image-Resolution': '.01', 'Vehicle-Height': '4.87'})
        with patch.object(middleware_http, 'middleware_get_payload', return_value=response), \
             patch('app.services.radar_point_cloud.decode_radar_payload_in_pool', side_effect=decode_radar_payload):
            result = self.client.get('/api/booking/radar-image', headers=self.auth)
        data = result.json['data']
        self.assertAlmostEqual(data['vehicleSx'], 5.24)
        self.assertEqual(data['contentOriginX'], 524)
        self.assertEqual(data['imageOriginMeters'], 4)
        self.assertEqual(data['vehicleSourceSx'], 9.24)

    def test_image_origin_requires_valid_metric_extent_not_an_old_pixel_envelope(self):
        for envelope in ['100,0,650,240', '4,0,4,5', 'nan,0,8,5', '{"x":4}', '']:
            self.assertEqual(booking._image_origin_meters(envelope, 800, 500, .01), 0)
        self.assertEqual(booking._image_origin_meters('-4,0,8,5', 800, 500, .01), -4)


if __name__ == '__main__':
    unittest.main()
