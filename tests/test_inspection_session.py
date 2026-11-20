"""PC/Pad共用草稿、持久图片、旧会话隔离。测试禁止发送任何设备命令。"""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from sqlalchemy import create_engine
from app.services.inspection_session import InspectionSessionStore, SessionConflict, configure_store
from app.services import temp_image_cache
from app.api.session import session_api, init_session_sync
from app.extensions.auth import create_token

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


class InspectionSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = create_engine('sqlite:///' + str(Path(self.temp.name) / 'draft.db'))
        self.addCleanup(self.engine.dispose)
        self.store = InspectionSessionStore(self.engine, Path(self.temp.name) / 'images')
        self.store.initialize()
        self.addCleanup(lambda: configure_store(None))
        self.broadcast = patch('app.services.inspection_session.broadcast')
        self.broadcast.start()
        self.addCleanup(self.broadcast.stop)
        self.api_broadcast = patch('app.api.session.broadcast')
        self.api_broadcast.start()
        self.addCleanup(self.api_broadcast.stop)
        self.network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def change(self, changes, snapshot=None):
        s = snapshot or self.store.snapshot()
        return self.store.patch(s['sessionId'], s['revision'], changes, 'operator')

    def test_pc_detect_then_pad_append_then_pc_restore(self):
        self.store.record('booking_accepted', {'booking': {'is_detection': True, 'check_step': 1}})
        pc = self.change([{'path': ['form', 'plate'], 'value': '鄂A12345'},
                          {'path': ['captureLists', 'goods'], 'value': ['/api/images/original.jpg']}])
        self.store.record('detection_step', {'step': 3, 'message': '检测中', 'gateReleased': True})
        pad = self.store.snapshot()
        self.assertEqual(pad['draft']['form']['plate'], '鄂A12345')
        photo = 'data:image/png;base64,' + base64.b64encode(PNG).decode()
        result = self.change([{'path': ['captureLists', 'goods'], 'value': pc['draft']['captureLists']['goods'] + [photo]}], pad)
        self.assertEqual(len(result['draft']['captureLists']['goods']), 2)
        self.assertTrue(result['draft']['captureLists']['goods'][1].startswith('/api/images/session_assets/'))
        reopened = InspectionSessionStore(self.engine, self.store.image_root).snapshot()
        self.assertEqual(reopened, result)
        self.assertTrue(reopened['workflow']['gateReleased'])

    def test_old_frames_cannot_overwrite_current_session(self):
        s = self.store.snapshot()
        self.store.record('booking_reset', {})
        with self.assertRaises(SessionConflict):
            self.change([{'path': ['submitRemark'], 'value': '旧车备注'}], s)
        self.assertEqual(self.store.snapshot()['draft'], {})

    def test_ai_marks_restore_per_field_and_crop_references_match_shared_images(self):
        photo = 'data:image/png;base64,' + base64.b64encode(PNG).decode()
        snap = self.change([
            {'path': ['form', 'plate'], 'value': '鄂A12345'},
            {'path': ['aiMarks', 'plate'], 'value': '鄂A12345'},
            {'path': ['aiMarks', 'weightRange'], 'value': '(13000-16800)'},
            {'path': ['licensePaths', 'license'], 'value': photo},
            {'path': ['aiMarks', 'license'], 'value': photo},
        ])
        self.assertEqual(snap['draft']['aiMarks']['license'],snap['draft']['licensePaths']['license'])
        restored = InspectionSessionStore(self.engine, self.store.image_root).snapshot()
        self.assertEqual(restored['draft']['aiMarks'], snap['draft']['aiMarks'])
        removed=self.change([{'path': ['aiMarks', 'plate'], 'value': ''}])
        self.assertEqual(removed['draft']['aiMarks']['plate'],'')
        for key,value in [('badField','x'),('plate',True),('plate','x'*2049)]:
            with self.assertRaises(ValueError):self.change([{'path':['aiMarks',key],'value':value}])

    def test_old_revision_allows_disjoint_changes_but_rejects_same_field(self):
        s = self.store.snapshot()
        self.change([{'path': ['form', 'plate'], 'value': '鄂A12345'}], s)
        self.change([{'path': ['submitRemark'], 'value': 'Pad填写'}], s)
        with self.assertRaises(SessionConflict):
            self.change([{'path': ['form', 'plate'], 'value': '鄂B12345'}], s)

    def test_manual_choice_wins_over_late_ai_even_after_conflict_rebase(self):
        start = self.change([{'path': ['panelImages', 'body'], 'value': {'1': '/api/images/body.png'}},
                             {'path': ['form', 'plate'], 'value': '鄂A12345'}, {'path': ['form', 'plateColor'], 'value': '0'}])
        self.change([{'path': ['form', 'truckType'], 'value': '16'}, {'path': ['manualFields', 'truckType'], 'value': True}])
        ai = [dict(path=p, value=v, origin='ai', aiGroup='truck', aiBaseRevision=start['revision']) for p, v in [
            (['aiContext', 'truck'], {'source': '/api/images/body.png', 'wheelCount': 2, 'baseRevision': start['revision']}),
            (['form', 'truckType'], '11'), (['aiMarks', 'truckType'], '11'),
            (['form', 'containerType'], '4.1'), (['aiMarks', 'containerType'], '4.1')]]
        for base in (start, self.store.snapshot()):
            out = self.change(ai, base)
            self.assertEqual(out['draft']['form']['truckType'], '16')
            self.assertEqual(out['draft']['aiMarks']['truckType'], '')
            self.assertTrue(out['draft']['manualFields']['truckType'])
            self.assertEqual(out['draft']['form']['containerType'], '4.1')
            self.assertEqual(out['draft']['aiContext']['truck']['wheelCount'], 2)

    def test_manually_confirming_same_value_or_clearing_locks_ai_out(self):
        for field in ('plate', 'truckType', 'containerType', 'loadRate'):
            self.change([{'path': ['form', field], 'value': ''}, {'path': ['manualFields', field], 'value': True}])
            s = self.store.snapshot()
            group = {'plate': 'license', 'truckType': 'truck', 'containerType': 'truck', 'loadRate': 'load'}[field]
            out = self.change([{'path': ['form', field], 'value': 'AI', 'origin': 'ai', 'aiGroup': group, 'aiBaseRevision': s['revision']},
                               {'path': ['aiMarks', field], 'value': 'AI', 'origin': 'ai', 'aiGroup': group, 'aiBaseRevision': s['revision']}])
            self.assertEqual(out['draft']['form'][field], '')
            self.assertEqual(out['draft']['aiMarks'][field], '')
        self.change([{'path': ['manualFields', 'truckType'], 'value': False}])
        self.assertTrue(self.store.snapshot()['draft']['manualFields']['truckType'])

    def test_new_photo_rejects_old_ai_crop_and_ocr_as_one_optional_update(self):
        start = self.change([{'path': ['licensePaths', 'license'], 'value': '/api/images/old.jpg'}])
        self.change([{'path': ['licensePaths', 'license'], 'value': '/api/images/new.jpg'}])
        fields = [(['aiContext', 'license'], {'inputSource': '/api/images/old.jpg', 'source': '/api/images/cropped.jpg', 'baseRevision': start['revision']}),
                  (['licensePaths', 'license'], '/api/images/cropped.jpg'), (['aiMarks', 'license'], '/api/images/cropped.jpg'),
                  (['form', 'plate'], 'OLD'), (['aiMarks', 'plate'], 'OLD'), (['aiMarks', 'weightRange'], '(1000-2000)')]
        out = self.change([dict(path=p, value=v, origin='ai', aiGroup='license', aiBaseRevision=start['revision']) for p, v in fields] + [{'path': ['submitRemark'], 'value': '人工备注'}])
        self.assertEqual(out['draft']['licensePaths']['license'], '/api/images/new.jpg')
        self.assertNotIn('plate', out['draft'].get('form', {}))
        self.assertNotIn('license', out['draft'].get('aiContext', {}))
        self.assertEqual(out['draft']['submitRemark'], '人工备注')

    def test_ai_truck_type_uses_latest_plate_color_from_other_terminal(self):
        start = self.change([{'path': ['panelImages', 'body'], 'value': {'1': '/api/images/body.jpg'}},
                             {'path': ['form', 'plate'], 'value': '鄂A12345'}, {'path': ['form', 'plateColor'], 'value': '0'}])
        self.change([{'path': ['form', 'plateColor'], 'value': '1'}])
        values = [(['aiContext', 'truck'], {'source': '/api/images/body.jpg', 'wheelCount': 2, 'baseRevision': start['revision']}),
                  (['form', 'truckType'], '11'), (['aiMarks', 'truckType'], '11')]
        out = self.change([dict(path=p, value=v, origin='ai', aiGroup='truck', aiBaseRevision=start['revision']) for p, v in values], start)
        self.assertEqual(out['draft']['form']['truckType'], '12')
        self.assertEqual(out['draft']['aiMarks']['truckType'], '12')

    def test_retry_of_same_value_is_idempotent(self):
        s = self.store.snapshot()
        change = [{'path': ['submitRemark'], 'value': '测试'}]
        self.change(change, s)
        self.change(change, s)
        self.assertEqual(self.store.snapshot()['draft']['submitRemark'], '测试')

    def test_manual_photos_survive_delayed_auto_events_refresh_and_clear_on_next_vehicle(self):
        self.store.record('booking_accepted', {})
        for kind in ('head', 'tail', 'top', 'goods', 'evidence', 'license'):
            self.store.record('image_ready', {'imageType': kind, 'url': '/api/images/auto-before.jpg'})
            self.change([{'path': ['manualPhotos', kind], 'value': True},
                         {'path': ['captureThumbs', kind], 'value': '/api/images/manual-'+kind+'.jpg'}])
            self.store.record('lane_occupied', {'occupied': False})
            self.store.record('image_ready', {'imageType': kind, 'url': '/api/images/auto-after.jpg'})
            snap = self.store.snapshot()
            self.assertEqual(snap['draft']['captureThumbs'][kind], '/api/images/manual-'+kind+'.jpg')
            self.assertTrue(snap['draft']['manualPhotos'][kind])
        reopened = InspectionSessionStore(self.engine, self.store.image_root)
        self.assertEqual(reopened.snapshot()['draft']['manualPhotos'], snap['draft']['manualPhotos'])
        self.change([{'path': ['manualPhotos', 'head'], 'value': False}])
        self.assertTrue(self.store.snapshot()['draft']['manualPhotos']['head'])
        self.store.record('booking_reset', {})
        self.store.record('booking_accepted', {})
        snap = self.store.record('image_ready', {'imageType': 'head', 'url': '/api/images/next.jpg'})
        self.assertEqual(snap['draft']['captureThumbs']['head'], '/api/images/next.jpg')
        self.assertNotIn('manualPhotos', snap['draft'])

    def test_manual_single_endpoint_origin_and_mobile_upload_protect_photos(self):
        start = self.store.record('booking_accepted', {})
        self.store.record('image_ready', {'imageType': 'top', 'url': '/api/images/manual.jpg', 'origin': 'manual', 'sessionId': start['sessionId']})
        self.store.record('mobile_upload', {'head_image_path': '/api/images/mobile-head.jpg', 'goods_image_path': '/api/images/goods.jpg', 'license_image_path1': '/api/images/license.jpg'})
        for kind in ('top', 'head', 'goods', 'license'):
            before = self.store.snapshot()['draft']['captureThumbs'][kind]
            self.store.record('image_ready', {'imageType': kind, 'url': '/api/images/auto.jpg'})
            after = self.store.snapshot()
            self.assertEqual(after['draft']['captureThumbs'][kind], before)
        self.store.record('booking_reset', {})
        self.store.record('booking_accepted', {})
        self.store.record('image_ready', {'imageType': 'top', 'url': '/api/images/late.jpg', 'origin': 'manual', 'sessionId': start['sessionId']})
        after = self.store.snapshot()
        self.assertNotIn('top', after['draft'].get('captureThumbs', {}))

    def test_manual_delete_is_not_refilled_by_auto_photo(self):
        self.store.record('booking_accepted', {})
        self.change([{'path': ['captureThumbs', 'head'], 'value': ''}])
        self.store.record('image_ready', {'imageType': 'head', 'url': '/api/images/auto.jpg'})
        snap = self.store.snapshot()
        self.assertEqual(snap['draft']['captureThumbs']['head'], '')

    def test_ocr_plate_source_is_shared_and_restored_independently_of_new_crop(self):
        photo = 'data:image/png;base64,' + base64.b64encode(PNG).decode()
        snap = self.change([
            {'path': ['aiContext', 'license'], 'value': {'source': '/api/images/new-crop.jpg', 'plateSource': photo, 'stitchedSource': photo, 'ocrDone': False}},
        ])
        context = snap['draft']['aiContext']['license']
        self.assertTrue(context['plateSource'].startswith('/api/images/session_assets/'))
        self.assertEqual(context['source'], '/api/images/new-crop.jpg')
        self.assertFalse(context['ocrDone'])
        self.assertEqual(context['stitchedSource'], context['plateSource'])
        self.assertEqual(InspectionSessionStore(self.engine, self.store.image_root).snapshot()['draft']['aiContext']['license'], context)

    def test_automatic_camera_frames_restore_without_temp_cache(self):
        self.store.record('booking_accepted', {})
        url = temp_image_cache.put_and_url(PNG)
        snap = self.store.record('image_ready', {'imageType': 'body', 'urls': {'1': url}})
        image = snap['images']['body']['1']
        temp_image_cache._CACHE.clear()
        path = self.store.image_root / image.removeprefix('/api/images/')
        self.assertEqual(path.read_bytes(), PNG)

    def test_geometry_is_shared_and_survives_store_recreation(self):
        self.store.record('booking_accepted', {})
        sample = {'geometry': {'positions': [1, 2, 3]}, 'views': {'top': {'imageUrl': temp_image_cache.put_and_url(PNG)}}}
        snapshot = self.store.record('geometry', sample)
        path = self.store.image_root / snapshot['geometryUrl'].removeprefix('/api/images/')
        result = json.loads(path.read_text(encoding='utf8'))
        self.assertEqual(result['geometry'], sample['geometry'])
        self.assertNotIn('positions', json.dumps(snapshot))

    def test_new_geometry_invalidates_old_3d_screenshot(self):
        self.store.record('booking_accepted', {})
        self.change([{'path': ['radarCloudImageUrl'], 'value': '/api/images/previous-frame.png'}])
        snapshot = self.store.record('geometry', {'geometry': {'positions': [1, 2, 3]}})
        self.assertEqual(snapshot['draft']['radarCloudImageUrl'], '')

    def test_cloud_size_is_persisted_for_all_clients_without_changing_raw_width(self):
        self.store.record('booking_accepted', {})
        self.change([{'path': ['form', 'size'], 'value': '历史尺寸'}])
        for width, expected in [(2.5, '2.50'), (2.54, '2.54'), (2.549, '2.54'), (2.55, '2.54'), (3.2, '2.54')]:
            with self.subTest(width=width):
                before = self.store.snapshot()
                sample = {'geometry': {'boundsMeters': {'min': [0, 0, 0], 'max': [12.346, width, 3.678]}}}
                snapshot = self.store.record('geometry', sample)
                self.assertEqual(snapshot['draft']['form']['size'], f'长12.35m|宽{expected}m|高3.68m')
                stored = json.loads((self.store.image_root / snapshot['geometryUrl'].removeprefix('/api/images/')).read_text(encoding='utf8'))
                self.assertEqual(stored['geometry'], sample['geometry'])
                self.assertEqual(InspectionSessionStore(self.engine, self.store.image_root).snapshot(), snapshot)
                if before['draft']['form']['size'] != snapshot['draft']['form']['size']:
                    with self.assertRaises(SessionConflict):
                        self.change([{'path': ['form', 'size'], 'value': '迟到的旧尺寸'}], before)
        self.store.record('booking_reset', {})
        self.assertEqual(self.store.snapshot()['draft'], {})
        # 重置后的迟到点云不得把上一辆车的尺寸带入新单。
        self.store.record('geometry', sample)
        self.assertEqual(self.store.snapshot()['draft'], {})

    def test_invalid_cloud_bounds_leave_existing_form_size_untouched(self):
        self.store.record('booking_accepted', {})
        self.change([{'path': ['form', 'size'], 'value': '长12.00m|宽2.50m|高3.80m'}])
        for geometry in [None, {}, {'boundsMeters': {'min': [0, 0, 0], 'max': [12, 0, 3]}},
                         {'boundsMeters': {'min': [0, 0, 0], 'max': [12, -2, 3]}},
                         {'boundsMeters': {'min': [0, 0, 0], 'max': [12, 'bad', 3]}}]:
            with self.subTest(geometry=geometry):
                snapshot = self.store.record('geometry', {'geometry': geometry})
                self.assertEqual(snapshot['draft']['form']['size'], '长12.00m|宽2.50m|高3.80m')

    def test_same_frame_top_side_persist_and_next_frame_replaces_them(self):
        self.store.record('booking_accepted', {})
        top = temp_image_cache.put_and_url(PNG)
        side = temp_image_cache.put_and_url(PNG + b'side')
        snapshot = self.store.record('geometry', {'geometry': {'positions': [1, 2, 3]},
            'views': {'top': {'imageUrl': top}, 'side': {'imageUrl': side}}})
        shared = json.loads((self.store.image_root / snapshot['geometryUrl'].removeprefix('/api/images/')).read_text())
        for view, field in [('top', 'radarCloudTopImageUrl'), ('side', 'radarCloudSideImageUrl')]:
            ref = snapshot['draft'][field]
            self.assertEqual(ref, shared['views'][view]['imageUrl'])
            self.assertTrue((self.store.image_root / ref.removeprefix('/api/images/')).is_file())
        temp_image_cache._CACHE.clear()
        self.assertEqual(InspectionSessionStore(self.engine, self.store.image_root).snapshot(), snapshot)
        next_frame = self.store.record('geometry', {'geometry': {'positions': [4, 5, 6]},
            'views': {'top': {'imageUrl': temp_image_cache.put_and_url(PNG + b'new')}}})
        self.assertNotEqual(next_frame['draft']['radarCloudTopImageUrl'], snapshot['draft']['radarCloudTopImageUrl'])
        self.assertEqual(next_frame['draft']['radarCloudSideImageUrl'], '')

    def test_top_side_sync_materializes_images_and_rejects_non_text(self):
        photo = 'data:image/png;base64,' + base64.b64encode(PNG).decode()
        for field in ['radarCloudTopImageUrl', 'radarCloudSideImageUrl']:
            s = self.change([{'path': [field], 'value': photo}])
            self.assertTrue(s['draft'][field].startswith('/api/images/session_assets/'))
            with self.assertRaises(ValueError):
                self.change([{'path': [field], 'value': {}}])

    def test_idle_does_not_accept_late_camera_frame(self):
        self.store.record('image_ready', {'imageType': 'head', 'url': 'data:image/png;base64,' + base64.b64encode(PNG).decode()})
        self.assertNotIn('captureThumbs', self.store.snapshot()['draft'])

    def test_submission_freezes_draft_and_remembers_record(self):
        s = self.store.snapshot()
        result = self.store.mark_submitted(s['sessionId'], 123)
        self.assertEqual(result['submittedId'], 123)
        with self.assertRaises(SessionConflict):
            self.change([{'path': ['submitRemark'], 'value': '提交后修改'}])

    def test_blob_url_is_not_shareable(self):
        with self.assertRaises(ValueError):
            self.change([{'path': ['captureThumbs', 'goods'], 'value': 'blob:pc-only'}])

    def test_panel_removal_and_submit_choices_survive_client_switch(self):
        self.store.record('booking_accepted', {})
        self.store.record('image_ready', {'imageType': 'transparent', 'urls': {'1': temp_image_cache.put_and_url(PNG)}})
        snap = self.change([{'path': ['panelImages', 'transparent'], 'value': {'1': '', '2': '', '3': ''}},
                            {'path': ['submissionDraft', 'result_status'], 'value': 1},
                            {'path': ['submissionDraft', 'inspector_phone'], 'value': '13812345678'}])
        reopened = InspectionSessionStore(self.engine, self.store.image_root).snapshot()
        self.assertEqual(reopened, snap)
        self.assertEqual(reopened['draft']['panelImages']['transparent']['1'], '')
        self.assertEqual(reopened['draft']['submissionDraft']['result_status'], 1)

    def test_startup_restores_detection_without_replaying_commands(self):
        from app.api import booking
        original = dict(booking.get_booking_state())
        self.addCleanup(lambda: booking.get_booking_state().update(original))
        self.store.record('booking_accepted', {'booking': {'is_detection': True, 'check_step': 1}})
        self.store.record('detection_step', {'step': 3})
        booking.set_booking_state(is_detection=False, check_step=0)
        with patch('app.services.device_action.execute_device_action', side_effect=AssertionError('不可重放设备动作')):
            init_session_sync(Flask(__name__), self.store)
        self.assertTrue(booking.get_booking_state()['is_detection'])
        self.assertEqual(booking.get_booking_state()['check_step'], 3)

    def test_shape_and_private_fields_cannot_be_patched(self):
        for path in (['phase'], ['form'], ['form', '__proto__'], ['captureLists', 'goods', '0']):
            with self.assertRaises(ValueError):
                self.change([{'path': path, 'value': 'invalid'}])

    def test_rest_login_two_clients_and_stale_operation(self):
        app = Flask(__name__)
        app.register_blueprint(session_api)
        init_session_sync(app, self.store)
        @app.post('/api/booking/reset')
        def dummy_reset():
            return {'code': 0}
        pc, pad = app.test_client(), app.test_client()
        self.assertEqual(pc.get('/api/inspection-session/current').status_code, 401)
        headers = {'Authorization': 'Bearer ' + create_token({'username': 'pc'})}
        s = pc.get('/api/inspection-session/current', headers=headers).json['data']
        response = pad.patch('/api/inspection-session/current', headers=headers, json={'sessionId': s['sessionId'], 'revision': s['revision'], 'changes': [{'path': ['submitRemark'], 'value': 'Pad备注'}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(pc.get('/api/inspection-session/current', headers=headers).json['data']['draft']['submitRemark'], 'Pad备注')
        self.store.record('booking_reset', {})
        response = pc.post('/api/booking/reset', headers={**headers, 'X-Inspection-Session': s['sessionId']})
        self.assertEqual(response.status_code, 409)

    def test_new_radar_fields_persist_and_convert(self):
        from app.api.inspection import _persist_images, _convert_image_paths
        fields = {'appointment_radar_image_path': 'appt-ref', 'radar_3d_image_path': '3d-ref',
                  'radar_3d_top_image_path': 'top-ref', 'radar_3d_side_image_path': 'side-ref'}
        with patch('app.api.inspection.persist_to_storage', side_effect=lambda ref, cat: cat + '/' + ref) as persist:
            _persist_images(fields)
        self.assertEqual(persist.call_args_list[0].args[1], 'appointment_radar')
        self.assertEqual(persist.call_args_list[1].args[1], 'radar_3d')
        self.assertEqual(persist.call_args_list[2].args[1], 'radar_3d_top')
        self.assertEqual(persist.call_args_list[3].args[1], 'radar_3d_side')
        with patch('app.api.inspection.db_path_to_api', side_effect=lambda ref: '/api/images/' + ref):
            result = _convert_image_paths(fields)
        self.assertTrue(result['radar_3d_image_path'].startswith('/api/images/radar_3d/'))
        self.assertTrue(result['radar_3d_top_image_path'].startswith('/api/images/radar_3d_top/'))
        self.assertTrue(result['radar_3d_side_image_path'].startswith('/api/images/radar_3d_side/'))

    def test_submission_without_client_views_uses_persisted_session_views(self):
        from app.api.inspection import inspection_api
        app = Flask(__name__)
        app.register_blueprint(inspection_api)
        configure_store(self.store)
        self.store.record('booking_accepted', {})
        s = self.store.record('geometry', {'geometry': {'positions': [1, 2, 3]},
            'views': {'top': {'imageUrl': temp_image_cache.put_and_url(PNG)},
                      'side': {'imageUrl': temp_image_cache.put_and_url(PNG + b'side')}}})
        with patch('app.api.inspection._persist_images'), \
             patch('app.api.inspection.DBVehicle.addInspection', return_value=123) as save, \
             patch('app.db.user.DBUser.getUserByUsername', return_value={}):
            result = app.test_client().post('/api/inspection/submit',
                headers={'Authorization': 'Bearer ' + create_token({'username': 'operator', 'real_name': '测试'})},
                json={'inspection_session_id': s['sessionId'], 'plate_number': 'TEST'})
        self.assertEqual(result.status_code, 200)
        body = save.call_args.args[0]
        self.assertEqual(body['radar_3d_top_image_path'], s['draft']['radarCloudTopImageUrl'])
        self.assertEqual(body['radar_3d_side_image_path'], s['draft']['radarCloudSideImageUrl'])


if __name__ == '__main__':
    unittest.main()
