"""数据库参数持久化、整批启用和命令模板；所有设备发送均隔离。"""
import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from flask import Flask
from sqlalchemy import create_engine, update
from app.services import system_config as config
from app.devices.mcast_cmd import send_mdrmc
from app.extensions.auth import create_token
from app.api.system_config import system_config_api


class SystemConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = create_engine('sqlite:///' + str(Path(self.temp.name) / 'config.db'))
        self.addCleanup(self.engine.dispose)
        self.store = config.SystemConfigStore(self.engine)
        self.store.initialize()
        config.configure_for_test()
        self.addCleanup(config.configure_for_test)
        self.net = patch('socket.socket', side_effect=AssertionError('禁止真实网络'))
        self.net.start(); self.addCleanup(self.net.stop)

    def test_seed_keeps_current_values_and_restart_preserves_edit(self):
        self.store.save({'middleware.RADAR_POINT_CLOUD_URL': 'http://127.0.0.1:5001/new/laz'})
        self.store.initialize()
        value = {row['key']: row['value'] for row in self.store.rows()}['middleware.RADAR_POINT_CLOUD_URL']
        self.assertEqual(value, 'http://127.0.0.1:5001/new/laz')
        self.assertIsNone(config.value('middleware.RADAR_POINT_CLOUD_URL'))

    def test_invalid_batch_is_atomic_and_unknown_items_are_rejected(self):
        original = self.store.rows()
        for values in ({'transport.MCAST_PORT': 0}, {'unknown': 1}, {'middleware.RADAR_POINT_CLOUD_URL': 'javascript:bad'},
                       {'voice.playbackDurationSec': float('nan')}, {'transport.MCAST_GROUP': '192.168.88.1'}):
            with self.assertRaises(ValueError): self.store.save(values)
        self.assertEqual(original, self.store.rows())

    def test_revision_prevents_overwriting_another_edit(self):
        self.store.save({'voice.varmcFreshSec': 6}, revisions={'voice.varmcFreshSec': 0})
        with self.assertRaises(config.ConfigConflict):
            self.store.save({'voice.varmcFreshSec': 8}, revisions={'voice.varmcFreshSec': 0})

    def test_existing_workflow_migration_only_removes_retired_distance_rules_once(self):
        old = copy.deepcopy(self.store.baseline['scheduler.workflow'])
        old['gateRelease']['minClearDistance'] = 29
        old['thresholds'][0]['maxDistance'] = 88
        for name in config.RETIRED_DISTANCE_RULES:
            old['thresholds'].append({'id': name, 'isActive': True, 'minDistance': 3, 'maxDistance': 9, 'deviceActions': {}})
        with self.engine.begin() as conn:
            conn.execute(update(config.config_table).where(config.config_table.c.key == 'scheduler.workflow').values(value=old))
        self.store.initialize()
        first = next(row for row in self.store.rows() if row['key'] == 'scheduler.workflow')
        self.assertEqual(first['value']['gateRelease']['minClearDistance'], 29)
        self.assertEqual(first['value']['thresholds'][0]['maxDistance'], 88)
        self.assertFalse(set(t['id'] for t in first['value']['thresholds']) & config.RETIRED_DISTANCE_RULES)
        self.assertEqual(first['revision'], 1)
        self.store.initialize()
        self.assertEqual(first, next(row for row in self.store.rows() if row['key'] == 'scheduler.workflow'))
        with self.assertRaises(ValueError): self.store.save({'scheduler.workflow': old})

    def test_prompt_timing_is_validated_before_saving(self):
        for name, value in (('prompt_clip_sec', .5), ('prompt_gap_sec', 31)):
            settings = copy.deepcopy(self.store.baseline['voice.announce'])
            settings[name] = value
            with self.assertRaises(ValueError):
                self.store.save({'voice.announce': settings})

    def test_real_packet_uses_configured_templates_and_prefix(self):
        templates = copy.deepcopy(self.store.baseline['commands.templates'])
        templates.update(camcap='linecapture={value}', **{'xraycap.start': 'xscan=true&{length}&{vehicle_type}', 'xraycap.stop': 'xscan=false'})
        config.validate('commands.templates', templates, self.store.baseline)
        config.configure_for_test({'commands.templates': templates, 'commands.prefix': '$TEST'})
        with patch('app.devices.mcast_cmd.publish_mdrmc', return_value=True) as sender:
            self.assertTrue(send_mdrmc('camcap=true', 'xraycap=true&1.8&large'))
            self.assertEqual(sender.call_args.args[0], '$TEST,2,linecapture=true,xscan=true&1.8&large')
            self.assertTrue(send_mdrmc('xraycap=false'))
            self.assertEqual(sender.call_args.args[0], '$TEST,1,xscan=false')

    def test_scheduler_required_frames_and_tail_offset_load_from_parameters(self):
        from app.services.scheduler import DistanceScheduler
        scheduler = DistanceScheduler()
        workflow = copy.deepcopy(self.store.baseline['scheduler.workflow'])
        workflow['gateRelease'].update(minClearDistance=30, requiredFrames=7)
        workflow['tailCaptureOffsetMeters'] = 6.5
        config.validate('scheduler.workflow', workflow, self.store.baseline)
        scheduler.load_configuration_from_json(workflow)
        self.assertEqual(scheduler._gate_release_required_frames, 7)
        self.assertEqual(scheduler._tail_capture_offset_meters, 6.5)
        self.assertEqual(scheduler._gate_release_min_distance, 30)
        scheduler.load_configuration_from_json(self.store.baseline['scheduler.workflow'])

    def test_startup_applies_url_polling_and_voice_parameters_to_consumers(self):
        import config as app_config
        import config.devices as devices
        from app.services import command_announce, lane_announce, body_camera_fetch, progressive_image_fetch, geometry_capture
        self.store.save({'middleware.RADAR_POINT_CLOUD_URL': 'http://127.0.0.1:6001/geom/laz',
                         'capture.pollIntervalSec': .4, 'voice.playbackDurationSec': 4.0})
        with ExitStack() as stack:
            for module, names in [(app_config, ['RADAR_POINT_CLOUD_URL']), (devices, ['RADAR_POINT_CLOUD_URL']),
                                  (command_announce, ['PLAYBACK_DURATION_SEC']), (lane_announce, ['VARMC_FRESH_SEC'])] + [
                                      (m, ['POLL_INTERVAL_SEC', 'MAX_POLL_SEC', 'POLL_TIMEOUT']) for m in (body_camera_fetch, progressive_image_fetch, geometry_capture)]:
                for name in names: stack.enter_context(patch.object(module, name, getattr(module, name)))
            config.activate(self.store)
            self.assertEqual(app_config.RADAR_POINT_CLOUD_URL, 'http://127.0.0.1:6001/geom/laz')
            self.assertEqual(geometry_capture.POLL_INTERVAL_SEC, .4)
            self.assertEqual(command_announce.PLAYBACK_DURATION_SEC, 4.0)
            self.assertEqual(command_announce.load_config()['steps']['1'], 'VOICE1')

    def test_api_requires_admin_and_save_does_not_activate_or_dispatch(self):
        app = Flask(__name__); app.register_blueprint(system_config_api)
        client = app.test_client()
        admin = {'Authorization': 'Bearer ' + create_token({'username': 'admin', 'role': 0})}
        ordinary = {'Authorization': 'Bearer ' + create_token({'username': 'operator', 'role': 1})}
        with patch.object(config, '_store', self.store), patch('app.api.system_config.get_store', return_value=self.store):
            self.assertEqual(client.get('/api/config').status_code, 401)
            self.assertEqual(client.get('/api/config', headers=ordinary).status_code, 403)
            response = client.patch('/api/config', headers=admin, json={'values': {'voice.varmcFreshSec': 6}})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(next(row for row in response.json['data'] if row['key'] == 'voice.varmcFreshSec')['pendingRestart'])
            self.assertIsNone(config.value('voice.varmcFreshSec'))


if __name__ == '__main__': unittest.main()
