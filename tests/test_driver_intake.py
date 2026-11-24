"""Offline end-to-end: actual receiver catalog -> sync/cache -> session binding.
No sockets, cloud credentials, production database, or device commands.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from flask import Flask
from sqlalchemy import create_engine, select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'station-intake-service'))
from receiver.app import create_app as receiver_app
from app.services.driver_intake import DriverIntake, records, sync_state, bindings, audit, SourceReset
from app.services.inspection_session import InspectionSessionStore, SessionConflict
from app.api.driver_intake import intake_api
from app.extensions.auth import create_token


class PhotoResponse:
    def __init__(self, raw): self.raw = raw
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size): yield self.raw


class LocalClient:
    def __init__(self, app):
        self.app = app
        self.client = app.test_client()
        self.bad_photo = False
        self.calls = 0
    def changes(self, source, cursor):
        self.calls += 1
        response = self.client.get('/api/v1/changes', query_string={'source_id': source, 'cursor': cursor, 'limit': 2},
            headers={'Authorization': 'Bearer ' + self.app.config['READ_KEY']})
        if response.status_code == 409: raise SourceReset()
        assert response.status_code == 200
        return response.json['data']
    def photo(self, rid, pid):
        response = self.client.get(f'/api/v1/records/{rid}/photos/{pid}',
            headers={'Authorization': 'Bearer ' + self.app.config['READ_KEY']})
        assert response.status_code == 200
        raw = response.data
        response.close()
        return PhotoResponse(b'corrupt' if self.bad_photo else raw)


class DriverIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        url = os.environ.get('INTAKE_TEST_DATABASE_URL')
        if url:
            from sqlalchemy import text
            from sqlalchemy.engine import make_url
            schema = 'intake_test_' + uuid.uuid4().hex
            bootstrap = create_engine(url)
            with bootstrap.begin() as conn:
                conn.execute(text('CREATE SCHEMA ' + schema))
            self.engine = create_engine(make_url(url).update_query_dict({'options': '-csearch_path=' + schema}))
            def cleanup_schema():
                self.engine.dispose()
                with bootstrap.begin() as conn:
                    conn.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
                bootstrap.dispose()
            self.addCleanup(cleanup_schema)
        else:
            self.engine = create_engine('sqlite:///' + str(root/'test.db'))
        self.addCleanup(self.engine.dispose)
        self.sessions = InspectionSessionStore(self.engine, root/'images'); self.sessions.initialize()
        self.app = receiver_app({'DATA_DIR': str(root/'receiver'), 'STATION_CODE': 'npu', 'READ_KEY': 'r'*32,
            'CONTROL_KEY': 'c'*32, 'CLOUD_ENABLED': False, 'WORKERS_ENABLED': False, 'OCR_URL': ''})
        self.catalog = self.app.extensions['catalog']
        self.transport = LocalClient(self.app)
        self.service = DriverIntake(self.sessions, root/'images',
            {'enabled': True, 'station_id': 'npu', 'base_url': 'http://127.0.0.1:8091', 'window_seconds': 480, 'poll_seconds': 0}, self.transport)
        self.sessions.record('booking_accepted', {})
        self.web = Flask(__name__); self.web.extensions['driver_intake'] = self.service
        self.web.register_blueprint(intake_api)
        self.webclient = self.web.test_client()
        self.auth = {'Authorization': 'Bearer ' + create_token({'username': 'operator', 'role': 1})}
        self.patch_broadcast = patch('app.api.driver_intake.broadcast')
        self.patch_broadcast.start(); self.addCleanup(self.patch_broadcast.stop)
        self.network = patch('socket.socket', side_effect=AssertionError('No network allowed'))
        self.network.start(); self.addCleanup(self.network.stop)

    def seed(self, age=60, plate='', pending=False):
        rid = str(uuid.uuid4()); folder = self.catalog.root/'records'/rid; folder.mkdir(parents=True)
        photos = []
        for kind in ('license_main', 'goods'):
            pid = str(uuid.uuid4()); raw = (kind + rid).encode()
            (folder/(pid+'.jpg')).write_bytes(raw)
            photos.append({'id': pid, 'kind': kind, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        self.catalog.ingest({'uuid': rid, 'cloud_confirmed': True, 'station_code': 'npu',
            'received_at': int(time.time())-age, 'phone': '13800000000', 'local_photos': photos})
        item = self.catalog.get(rid)
        if not pending:
            item.update(plate=plate, ocr_status='done'); self.catalog.put(item)
        return item

    def sync(self):
        while self.service.tick(): pass

    def associate(self, rid, **kwargs):
        snap = self.sessions.snapshot()
        return self.service.associate(rid, snap['sessionId'], snap['revision'], 'operator', **kwargs)

    def test_window_fixed_for_long_detection_and_earlier_search(self):
        recent = self.seed(age=470); older = self.seed(age=500)
        self.sync()
        with patch('app.services.driver_intake.time.time', return_value=time.time()+900):
            current = self.service.candidates()
        self.assertEqual([i['uuid'] for i in current['items']], [recent['uuid']])
        self.assertEqual(current['window_seconds'], 480)
        self.assertEqual(self.service.candidates(query=older['uuid'], earlier=True)['total'], 1)

    def test_ocr_after_download_updates_same_row_and_recommendation(self):
        item = self.seed(pending=True); self.sync()
        s = self.sessions.snapshot()
        self.sessions.patch(s['sessionId'], s['revision'], [{'path':['passcode'], 'value': {'vehicleDisplayId':'鄂A12345'}}])
        self.assertEqual(self.service.candidates()['items'][0]['recommendation'], 'pending')
        item.update(plate='鄂A12345', ocr_status='done'); self.catalog.put(item); self.sync()
        candidates = self.service.candidates()
        self.assertEqual(candidates['total'], 1)
        self.assertEqual(candidates['items'][0]['recommendation'], 'match')
        self.assertIsNone(candidates['selection'])
        self.assertNotIn('phone', self.sessions.snapshot()['draft'].get('form', {}))

    def test_failed_photo_keeps_cursor_retry_deduplicates(self):
        item = self.seed(); self.transport.bad_photo = True
        with self.assertRaises(ValueError): self.service.tick()
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(select(sync_state.c.cursor)).scalar_one(), 0)
            self.assertEqual(conn.execute(select(records)).fetchall(), [])
        self.transport.bad_photo = False; self.sync(); self.sync()
        self.assertEqual(self.service.candidates()['total'], 1)
        self.assertTrue(all(self.service.valid_photo(item,p) for p in item['photos']))

    def test_multiple_pages_and_source_restart_without_loss(self):
        for _ in range(4): self.seed()
        self.sync()
        self.assertGreaterEqual(self.transport.calls, 4)
        with self.engine.begin() as conn:
            conn.execute(update(sync_state).values(source_id=str(uuid.uuid4())))
        self.assertTrue(self.service.tick())
        self.sync()
        self.assertEqual(self.service.candidates()['total'], 4)

    def test_station_mismatch_and_mutated_original_rejected(self):
        item = self.seed(); self.sync()
        bad = copy.deepcopy(item); bad['station_id'] = 'whds'
        with self.assertRaises(ValueError): self.service.validate_record(bad)
        item['received_at'] += 1; self.catalog.put(item)
        with self.assertRaises(ValueError): self.service.tick()

    def test_bind_requires_complete_photos_and_version_for_two_terminals(self):
        item = self.seed(); self.sync(); old = self.sessions.snapshot()
        result = self.associate(item['uuid'])
        self.assertEqual(result['intakeSelection']['uuid'], item['uuid'])
        self.assertEqual(result['draft']['form']['phone'], item['phone'])
        main_url = result['draft']['licensePaths']['license']
        self.assertTrue(main_url.startswith('/api/images/driver-intake/npu/'))
        self.assertTrue((self.sessions.image_root / main_url.removeprefix('/api/images/')).is_file())
        self.assertEqual(result['draft']['captureThumbs']['license'], main_url)
        self.assertTrue(all(url.startswith('/api/images/driver-intake/npu/')
                            for url in result['draft']['captureLists']['goods']))
        with self.assertRaises(SessionConflict):
            self.service.associate(item['uuid'], old['sessionId'], old['revision'], 'pad')
        self.assertEqual(self.service.candidates()['selection']['uuid'], item['uuid'])
        self.assertEqual(self.service.candidates()['items'][0]['usage'], 'current')

    def test_mismatch_and_overwrite_need_explicit_confirmation(self):
        item = self.seed(plate='鄂B67890'); self.sync()
        s = self.sessions.snapshot()
        self.sessions.patch(s['sessionId'], s['revision'], [{'path':['passcode'], 'value':{'vehicleDisplayId':'鄂A12345'}}, {'path':['form','phone'], 'value':'13900000000'}])
        with self.assertRaises(SessionConflict): self.associate(item['uuid'])
        with self.assertRaises(SessionConflict): self.associate(item['uuid'], allow_mismatch=True)
        self.associate(item['uuid'], replace=True, allow_mismatch=True)
        self.assertEqual(self.sessions.snapshot()['draft']['passcode']['vehicleDisplayId'], '鄂A12345')

    def test_unlink_restores_baseline_but_preserves_later_manual_edits(self):
        item = self.seed(); self.sync(); selected = self.associate(item['uuid'])
        self.sessions.patch(selected['sessionId'], selected['revision'], [{'path':['form','phone'], 'value':'13900000000'}])
        result = self.associate(None)
        self.assertEqual(result['draft']['form']['phone'], '13900000000')
        self.assertFalse(result['draft']['captureLists'].get('goods'))
        self.assertNotIn('intakeSelection', result)
        self.assertEqual(result['draft']['licensePaths']['license'], '')
        self.assertFalse(result['draft']['manualPhotos']['license'])
        self.assertFalse(result['draft']['manualPhotos']['goods'])


    def test_unlink_keeps_manual_photo_and_its_protection(self):
        item = self.seed(); self.sync(); selected = self.associate(item['uuid'])
        self.sessions.patch(selected['sessionId'], selected['revision'],
            [{'path':['licensePaths','license'], 'value':'/api/images/manual-license.jpg'}])
        result = self.associate(None)
        self.assertEqual(result['draft']['licensePaths']['license'], '/api/images/manual-license.jpg')
        self.assertTrue(result['draft']['manualPhotos']['license'])
        self.assertFalse(result['draft']['manualPhotos']['goods'])

    def test_submission_history_stays_used_and_delayed_selection_rejected(self):
        item = self.seed(); self.sync(); bound = self.associate(item['uuid'])
        self.sessions.mark_submitted(bound['sessionId'], 123)
        self.sessions.record('booking_reset', {})
        self.sessions.record('booking_accepted', {})
        self.assertEqual(self.service.candidates()['items'][0]['usage'], 'used')
        with self.assertRaises(SessionConflict): self.associate(item['uuid'])
        with self.assertRaises(SessionConflict): self.service.associate(item['uuid'], bound['sessionId'], bound['revision'], 'pad')

    def test_canceled_session_can_reuse_record(self):
        item = self.seed(); self.sync(); self.associate(item['uuid'])
        self.sessions.record('booking_reset', {}); self.sessions.record('booking_accepted', {})
        self.assertEqual(self.service.candidates()['items'][0]['usage'], 'available')
        self.associate(item['uuid'])

    def test_binding_and_draft_rollback_together(self):
        item = self.seed(); self.sync(); before = self.sessions.snapshot()
        with patch.object(self.sessions, '_touch', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError): self.associate(item['uuid'])
        self.assertEqual(self.sessions.snapshot(), before)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(select(bindings)).fetchall(), [])
            self.assertEqual(conn.execute(select(audit)).fetchall(), [])

    def test_backend_api_login_pagination_and_photo(self):
        item = self.seed(); self.sync()
        self.assertEqual(self.webclient.get('/api/driver-intake/candidates').status_code, 401)
        self.assertEqual(self.webclient.get('/api/driver-intake/candidates?limit=0',headers=self.auth).status_code, 400)
        r = self.webclient.get('/api/driver-intake/candidates', headers=self.auth)
        self.assertEqual(r.status_code, 200)
        url = r.json['data']['items'][0]['photos'][0]['url']
        photo = self.webclient.get(url,headers=self.auth)
        self.assertEqual(photo.status_code, 200); photo.close()
        body = {'uuid':item['uuid'],'sessionId':r.json['data']['sessionId'],'revision':r.json['data']['revision']}
        self.assertEqual(self.webclient.post('/api/driver-intake/selection',headers=self.auth,json=body).status_code,200)
        self.assertEqual(self.webclient.post('/api/driver-intake/selection',headers=self.auth,json=body).status_code,409)

    def test_disabled_or_idle_never_calls_ai(self):
        self.sessions.record('booking_reset', {})
        self.assertFalse(self.service.tick())
        self.sessions.record('booking_accepted', {})
        self.service.cfg['enabled'] = False
        self.assertFalse(self.service.tick())
        self.assertEqual(self.transport.calls, 0)

    def test_sync_error_preserves_cache_and_reports_sanitized_type(self):
        self.seed(); self.sync()
        self.transport.changes = lambda *_: (_ for _ in ()).throw(RuntimeError('secret must not leak'))
        self.service.run_once()
        state = self.service.candidates()
        self.assertEqual(state['total'], 1)
        self.assertEqual(state['sync']['last_error'], 'RuntimeError')
        self.assertNotIn('secret', json.dumps(state))

    @unittest.skipUnless(os.environ.get('INTAKE_TEST_DATABASE_URL'), 'requires isolated PostgreSQL')
    def test_two_process_stores_compete_for_one_revision(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        item = self.seed(); self.sync(); snap = self.sessions.snapshot()
        second_store = InspectionSessionStore(self.engine, self.sessions.image_root)
        second = DriverIntake(second_store, self.sessions.image_root, self.service.cfg, self.transport)
        barrier = threading.Barrier(2)
        def choose(service):
            barrier.wait(timeout=5)
            try:
                service.associate(item['uuid'], snap['sessionId'], snap['revision'], 'terminal')
                return 'ok'
            except SessionConflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(choose, [self.service, second]))
        self.assertCountEqual(results, ['ok', 'conflict'])
        with self.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(bindings)).fetchall()), 1)

    @unittest.skipUnless(os.environ.get('INTAKE_TEST_DATABASE_URL'), 'requires isolated PostgreSQL')
    def test_sync_db_lock_prevents_duplicate_upstream_poll(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.seed()
        second = DriverIntake(InspectionSessionStore(self.engine, self.sessions.image_root), self.sessions.image_root, self.service.cfg, self.transport)
        entered, release = threading.Event(), threading.Event()
        original = self.transport.changes
        def slow(source, cursor):
            entered.set()
            if not release.wait(5): raise RuntimeError('test timeout')
            return original(source, cursor)
        self.transport.changes = slow
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.service.tick)
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(second.tick())
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(self.transport.calls, 1)

    def test_unlinked_removed_fields_reject_stale_terminal_patch(self):
        item = self.seed(); self.sync(); selected = self.associate(item['uuid'])
        self.associate(None)
        with self.assertRaises(SessionConflict):
            self.sessions.patch(selected['sessionId'], selected['revision'],
                [{'path':['form','phone'], 'value': item['phone']}])
