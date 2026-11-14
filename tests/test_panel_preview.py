"""缩略显示与原图证据分离；不访问设备或生产数据库。"""
import hashlib
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np
from flask import Flask
from sqlalchemy import create_engine

from app.api.panel_images import panel_images_api
from app.extensions.auth import create_token
from app.services import temp_image_cache
from app.services.image_preview import put_temp_preview_url
from app.services.inspection_session import InspectionSessionStore, configure_store
from app.services.panel_preview import crop_original, ensure_preview, preview_path


class PanelPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'images'
        self.engine = create_engine('sqlite:///' + str(Path(self.temp.name) / 'session.db'))
        self.addCleanup(self.engine.dispose)
        self.store = InspectionSessionStore(self.engine, self.root)
        self.store.initialize()
        configure_store(self.store)
        self.addCleanup(lambda: configure_store(None))
        self.image = np.zeros((1000, 4000, 3), np.uint8)
        self.image[:, :, 0] = np.arange(4000, dtype=np.uint16) % 256
        self.image[:, :, 1] = np.arange(1000, dtype=np.uint16)[:, None] % 256
        self.raw = cv2.imencode('.png', self.image)[1].tobytes()
        self.name = hashlib.sha256(self.raw).hexdigest() + '.png'
        self.ref = '/api/images/session_assets/' + self.name
        self.store.record('booking_accepted', {})
        app = Flask(__name__)
        app.register_blueprint(panel_images_api)
        self.client = app.test_client()
        self.auth = {'Authorization': 'Bearer ' + create_token({'username': 'test'})}

    def record(self, paired=True):
        url = put_temp_preview_url(self.raw) if paired else temp_image_cache.put_and_url(self.raw)
        return self.store.record('image_ready', {'imageType': 'body', 'urls': {'1': url}})

    def test_preview_and_original_survive_cache_loss_and_store_recreation(self):
        snap = self.record()
        self.assertEqual(snap['draft']['panelImages']['body']['1'], self.ref)
        original = self.root / 'session_assets' / self.name
        self.assertEqual(original.read_bytes(), self.raw)
        temp_image_cache._CACHE.clear()
        reopened = InspectionSessionStore(self.engine, self.root)
        self.assertEqual(reopened.snapshot(), snap)
        response = self.client.get('/api/panel-images/preview/' + self.name)
        self.assertEqual(response.status_code, 200)
        preview = cv2.imdecode(np.frombuffer(response.data, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(preview.shape[:2], (400, 1600))
        self.assertLess(len(response.data), len(self.raw))
        response.close()
        # 老会话没有sidecar也能按需生成。
        preview_path(self.root, self.name).unlink()
        self.assertTrue(ensure_preview(self.root, self.name).is_file())

    def test_unpaired_source_also_generates_small_preview(self):
        self.record(paired=False)
        preview = cv2.imread(str(preview_path(self.root, self.name)))
        self.assertEqual(preview.shape[:2], (400, 1600))

    def test_crop_uses_original_pixels_at_original_resolution(self):
        self.record()
        for box in [(200, 80, 400, 180), (400, 180, 200, 80)]:
            raw = crop_original(self.root, self.ref, box, 1600, 400)
            crop = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
            np.testing.assert_array_equal(crop, self.image[200:450, 500:1000])
            self.assertEqual(crop.shape[:2], (250, 500))
        crop = cv2.imdecode(np.frombuffer(crop_original(self.root, self.ref, (-20, -20, 40, 40), 1600, 400), np.uint8), -1)
        np.testing.assert_array_equal(crop, self.image[:100, :100])

    def test_invalid_crop_and_paths_are_rejected(self):
        self.record()
        for ref in ['/api/images/session_assets/../secret', '/api/images/session_assets/' + self.name + '?x=1', 'https://other/image', '/api/images/other.png']:
            with self.assertRaises(ValueError):
                crop_original(self.root, ref, (0, 0, 1, 1), 1600, 400)
        for box, w, h in [((0, 0, 0, 1), 1600, 400), ((0, 0, float('nan'), 1), 1600, 400), ((0, 0, 1, 1), 0, 400), ((0, 0, 1, 1), 1600, float('inf')), ((1800, 0, 2000, 20), 1600, 400)]:
            with self.assertRaises(ValueError):
                crop_original(self.root, self.ref, box, w, h)
        self.assertEqual(self.client.get('/api/panel-images/preview/not-a-hash.png').status_code, 404)

    def test_crop_api_requires_login_and_returns_materializable_original(self):
        self.record()
        body = dict(sourceUrl=self.ref, displayWidth=1600, displayHeight=400, x1=200, y1=80, x2=400, y2=180)
        self.assertEqual(self.client.post('/api/panel-images/crop', json=body).status_code, 401)
        response = self.client.post('/api/panel-images/crop', json=body, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        raw = temp_image_cache.get_for_persist(temp_image_cache.parse_temp_id(response.json['data']['url'])).data
        np.testing.assert_array_equal(cv2.imdecode(np.frombuffer(raw, np.uint8), -1), self.image[200:450, 500:1000])
        self.assertEqual(self.client.post('/api/panel-images/crop', json=[], headers=self.auth).status_code, 400)


if __name__ == '__main__':
    unittest.main()
