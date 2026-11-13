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

    def test_old_revision_allows_disjoint_changes_but_rejects_same_field(self):
        s = self.store.snapshot()
        self.change([{'path': ['form', 'plate'], 'value': '鄂A12345'}], s)
        self.change([{'path': ['submitRemark'], 'value': 'Pad填写'}], s)
        with self.assertRaises(SessionConflict):
            self.change([{'path': ['form', 'plate'], 'value': '鄂B12345'}], s)

    def test_retry_of_same_value_is_idempotent(self):
        s = self.store.snapshot()
        change = [{'path': ['submitRemark'], 'value': '测试'}]
        self.change(change, s)
        self.change(change, s)
        self.assertEqual(self.store.snapshot()['draft']['submitRemark'], '测试')

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
        fields = {'appointment_radar_image_path': 'appt-ref', 'radar_3d_image_path': '3d-ref'}
        with patch('app.api.inspection.persist_to_storage', side_effect=lambda ref, cat: cat + '/' + ref) as persist:
            _persist_images(fields)
        self.assertEqual(persist.call_args_list[0].args[1], 'appointment_radar')
        self.assertEqual(persist.call_args_list[1].args[1], 'radar_3d')
        with patch('app.api.inspection.db_path_to_api', side_effect=lambda ref: '/api/images/' + ref):
            result = _convert_image_paths(fields)
        self.assertTrue(result['radar_3d_image_path'].startswith('/api/images/radar_3d/'))


if __name__ == '__main__':
    unittest.main()
