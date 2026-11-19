import io
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.services import ai_client, ai_request_cache as cache


class AiRequestCacheTests(unittest.TestCase):
    def setUp(self):
        with cache._lock:
            cache._cache.clear(); cache._pending.clear(); cache._bytes = 0

    def test_same_image_across_two_terminals_only_runs_one_inference(self):
        entered = threading.Event()
        calls = []
        def infer():
            calls.append(1); entered.set(); time.sleep(.03)
            return {'code': 1, 'data': {'wheel_count': 2}}
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(cache.cached_inference, 'same-image', infer)
            self.assertTrue(entered.wait(1))
            b = pool.submit(cache.cached_inference, 'same-image', infer)
            x, y = a.result(), b.result()
        self.assertEqual(calls, [1]); self.assertEqual(x, y)
        x['data']['wheel_count'] = 9
        self.assertEqual(cache.cached_inference('same-image', infer)['data']['wheel_count'], 2)

    def test_files_use_bytes_not_url_or_filename_and_include_task_parameters(self):
        cfg = dict(base_url='https://example.test', username='u', password='s', ca_bundle='x', proxy='')
        with patch.object(ai_client, 'settings', return_value=cfg), patch.object(ai_client, '_request_json', return_value={'code': 1}) as call:
            for name in ('local.jpg', 'shared.jpg'):
                ai_client.request_json('/detect', files={'image': (name, io.BytesIO(b'same'))}, data={'task': 'truck'})
            self.assertEqual(call.call_count, 1)
            ai_client.request_json('/detect', files={'image': ('a', b'same')}, data={'task': 'other'})
            self.assertEqual(call.call_count, 2)

    def test_failure_is_retryable_and_expired_success_is_not_reused(self):
        with self.assertRaisesRegex(RuntimeError, 'offline'):
            cache.cached_inference('x', lambda: (_ for _ in ()).throw(RuntimeError('offline')))
        self.assertEqual(cache.cached_inference('x', lambda: {'code': 1}), {'code': 1})
        with patch.object(cache, 'TTL', -.1): cache.cached_inference('old', lambda: {'result': 'old'})
        self.assertEqual(cache.cached_inference('old', lambda: {'result': 'new'}), {'result': 'new'})
