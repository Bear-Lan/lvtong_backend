"""同一车道的一份持久检测草稿。仅保存/广播数据，不派发设备命令。"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Column, Integer, MetaData, Table, Text, select, update

metadata = MetaData()
session_table = Table('inspection_session', metadata,
                      Column('id', Integer, primary_key=True), Column('payload', Text, nullable=False))
_store = None


class SessionConflict(ValueError):
    pass


def _new_document(revision=0):
    return {'sessionId': uuid.uuid4().hex, 'revision': revision, 'phase': 'idle',
            'booking': {}, 'workflow': {}, 'draft': {}, 'geometryUrl': '',
            'updatedBy': '', 'submittedId': None, '_versions': {}}


class InspectionSessionStore:
    def __init__(self, engine, image_root):
        self.engine = engine
        self.image_root = Path(image_root).resolve()
        self.lock = threading.RLock()

    def initialize(self):
        session_table.create(self.engine, checkfirst=True)
        with self.lock, self.engine.begin() as conn:
            if conn.execute(select(session_table.c.id).where(session_table.c.id == 1)).first() is None:
                conn.execute(session_table.insert().values(id=1, payload=json.dumps(_new_document())))

    @contextmanager
    def transaction(self):
        with self.lock, self.engine.begin() as conn:
            row = conn.execute(select(session_table.c.payload).where(session_table.c.id == 1).with_for_update()).scalar_one()
            doc = json.loads(row)
            yield doc
            conn.execute(update(session_table).where(session_table.c.id == 1).values(
                payload=json.dumps(doc, ensure_ascii=False, allow_nan=False)))

    def snapshot(self):
        with self.lock, self.engine.connect() as conn:
            doc = json.loads(conn.execute(select(session_table.c.payload).where(session_table.c.id == 1)).scalar_one())
        doc.pop('_versions', None)
        return doc

    def assert_current(self, session_id):
        if session_id and self.snapshot()['sessionId'] != session_id:
            raise SessionConflict('本次检测已结束或切换，请刷新当前检测内容')

    def _touch(self, doc, actor=''):
        doc['revision'] += 1
        doc['updatedAt'] = int(time.time() * 1000)
        if actor:
            doc['updatedBy'] = actor

    def _share_image(self, reference):
        if not reference:
            return ''
        if not isinstance(reference, str):
            raise ValueError('图片引用必须是字符串')
        if reference.startswith('blob:'):
            raise ValueError('本地照片尚未上传，不能同步 blob 地址')
        raw = None
        if reference.startswith('data:image/'):
            if len(reference) > 48 * 1024 * 1024:
                raise ValueError('单张照片超过同步大小限制')
            raw = base64.b64decode(reference.split(',', 1)[1], validate=True)
        elif '/api/temp-images/' in reference:
            from app.services.temp_image_cache import get_for_persist, parse_temp_id
            entry = get_for_persist(parse_temp_id(reference))
            if entry is None:
                raise ValueError('图片缓存已失效，请重新拍摄或截图')
            raw = entry.data
        if raw is None:
            # 已上传的共享URL，去掉旧页面附加的时间戳。
            if '/api/images/' in reference:
                return '/api/images/' + reference.split('/api/images/', 1)[1].split('?', 1)[0]
            return reference
        return self._write_asset(raw, '.png' if raw.startswith(b'\x89PNG') else '.jpg')

    def _write_asset(self, raw, suffix):
        digest = hashlib.sha256(raw).hexdigest()
        directory = self.image_root / 'session_assets'
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (digest + suffix)
        if not target.exists():
            temp = directory / (uuid.uuid4().hex + '.tmp')
            try:
                temp.write_bytes(raw)
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
        return '/api/images/session_assets/' + target.name

    def share_value(self, path, value):
        if path[0] == 'panelImages':
            if path[1] not in ('body', 'transparent') or not isinstance(value, dict) or any(k not in ('1', '2', '3') for k in value):
                raise ValueError('面板图片字段无效')
            return {k: self._share_image(v) for k, v in value.items()}
        if path[0] in ('captureThumbs', 'licensePaths') or path[0] in ('radarHeadImageUrl', 'radarCloudImageUrl', 'radarCloudTopImageUrl', 'radarCloudSideImageUrl'):
            return self._share_image(value)
        if path == ['bookingDraft', 'radarImageUrl']:
            return self._share_image(value)
        if path[0] in ('captureLists', 'cropGoodsImages'):
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError('照片列表无效或超过100张')
            return list(dict.fromkeys(self._share_image(v) for v in value if v))
        return copy.deepcopy(value)

    def patch(self, session_id, base_revision, changes, actor='', client_id=''):
        allowed = {'form', 'captureThumbs', 'captureLists', 'licensePaths', 'passcode',
                   'submitRemark', 'previousSelection', 'radarHeadImageUrl', 'radarCloudImageUrl', 'radarCloudTopImageUrl', 'radarCloudSideImageUrl', 'bookingDraft', 'cropGoodsImages', 'evidenceSourceKeys', 'panelImages', 'submissionDraft'}
        if not isinstance(changes, list) or len(changes) > 100:
            raise ValueError('同步字段数量无效')
        with self.transaction() as doc:
            if doc['sessionId'] != session_id or doc['phase'] == 'submitted':
                raise SessionConflict('本次检测已结束或切换，请重新读取当前检测')
            if not isinstance(base_revision, int) or base_revision < 0 or base_revision > doc['revision']:
                raise ValueError('同步版本无效')
            prepared = []
            for change in changes:
                path = change.get('path')
                if (not isinstance(path, list) or len(path) not in (1, 2)
                        or path[0] not in allowed or any(not isinstance(k, str) or k.startswith('_') or k in ('__proto__', 'constructor', 'prototype') for k in path)):
                    raise ValueError('不支持的同步字段')
                key = '.'.join(path)
                containers = {'form', 'captureThumbs', 'captureLists', 'licensePaths', 'bookingDraft', 'panelImages', 'submissionDraft'}
                if (path[0] in containers) != (len(path) == 2):
                    raise ValueError('同步字段结构无效')
                if path[0] in ('submitRemark', 'radarHeadImageUrl', 'radarCloudImageUrl', 'radarCloudTopImageUrl', 'radarCloudSideImageUrl') and not isinstance(change.get('value'), str):
                    raise ValueError('文本或图片字段格式无效')
                current = doc['draft']
                for k in path:
                    current = current.get(k) if isinstance(current, dict) else None
                value = self.share_value(path, change.get('value'))
                if doc['_versions'].get(key, 0) > base_revision and current != value:
                    raise SessionConflict('该内容已被其他设备修改，请刷新后核对再操作')
                prepared.append((path, key, value))
            self._touch(doc, actor)
            if client_id:
                doc['activeClientId'] = client_id
            for path, key, value in prepared:
                target = doc['draft']
                for k in path[:-1]:
                    target = target.setdefault(k, {})
                target[path[-1]] = value
                doc['_versions'][key] = doc['revision']
        return self.snapshot()

    def record(self, event, data):
        with self.transaction() as doc:
            old_draft = copy.deepcopy(doc['draft'])
            if event == 'booking_reset' or event == 'booking_rejected':
                revision = doc['revision']
                doc.clear()
                doc.update(_new_document(revision))
            elif event == 'booking_open':
                doc['phase'] = 'booking'
            elif event == 'booking_accepted':
                doc['phase'] = 'detecting'
                doc['workflow'] = {'step': 1, 'message': '', 'gateReleased': False, 'laneOccupied': True}
            elif event == 'detection_step':
                doc['workflow'].update({'step': data['step'], 'message': data.get('message', '')})
                if data.get('gateReleased'):
                    doc['workflow'].update(gateReleased=True, laneOccupied=False)
            elif event == 'lane_occupied':
                doc['workflow']['laneOccupied'] = bool(data.get('occupied'))
            elif event == 'image_ready':
                if doc['phase'] != 'detecting':
                    return
                kind = data.get('imageType')
                if data.get('url') and kind in ('head', 'tail', 'top', 'goods', 'evidence', 'license'):
                    doc['draft'].setdefault('captureThumbs', {})[kind] = self._share_image(data['url'])
                elif kind in ('body', 'transparent') and data.get('urls'):
                    image_group = doc.setdefault('images', {}).setdefault(kind, {})
                    for k, v in data['urls'].items():
                        if v:
                            image_group[str(k)] = self._share_image(v)
                    doc['draft'].setdefault('panelImages', {})[kind] = copy.deepcopy(image_group)
            elif event == 'geometry':
                if doc['phase'] != 'detecting':
                    return
                shared = copy.deepcopy(data)
                for view in shared.get('views', {}).values():
                    view['imageUrl'] = self._share_image(view['imageUrl'])
                doc['geometryUrl'] = self._write_asset(json.dumps(shared, ensure_ascii=False, allow_nan=False).encode(), '.json')
                doc['draft']['radarCloudImageUrl'] = '' # 新点云不能提交上一帧的3D截图。
                # 与本帧三维坐标同源的正交视图，不依赖终端当前旋转角度。
                for view, field in (('top', 'radarCloudTopImageUrl'), ('side', 'radarCloudSideImageUrl')):
                    doc['draft'][field] = shared.get('views', {}).get(view, {}).get('imageUrl', '')
            elif event == 'geometry_status':
                doc['geometryCapture'] = data.get('capture')
            elif event == 'mobile_upload':
                for source, target in (('head_image_path', 'head'), ('tail_image_path', 'tail'), ('top_image_path', 'top'), ('passcode_image_path', 'passcode')):
                    if data.get(source):
                        doc['draft'].setdefault('captureThumbs', {})[target] = data[source]
                for source, target in (('goods_image_path', 'goods'), ('evidences_image_path', 'evidence')):
                    if data.get(source):
                        lists = doc['draft'].setdefault('captureLists', {})
                        lists[target] = list(dict.fromkeys(lists.get(target, []) + data[source].split('|')))
                        doc['draft'].setdefault('captureThumbs', {})[target] = lists[target][-1]
                for source, target in (('license_image_path1', 'license'), ('license_image_path2', 'licenseGc')):
                    if data.get(source):
                        doc['draft'].setdefault('licensePaths', {})[target] = data[source]
                        doc['draft']['licensePaths']['licenseStitched'] = ''
                        doc['draft'].setdefault('captureThumbs', {})['license'] = data[source]
                for source, target in (('phone', 'phone'), ('plate_number_gc', 'plateGc'), ('goods_type', 'goodsProductCode')):
                    if data.get(source):
                        doc['draft'].setdefault('form', {})[target] = data[source]
                if data.get('passcode', {}).get('valid'):
                    pc = data['passcode']
                    doc['draft']['passcode'] = copy.deepcopy(pc)
                    form = doc['draft'].setdefault('form', {})
                    form.update(plate=pc.get('vehicleDisplayId', ''), plateColor=pc.get('vehicleColorName', ''), weight=pc.get('exWeight', ''))
            if data.get('booking') is not None:
                doc['booking'] = copy.deepcopy(data['booking'])
            if data.get('activeClientId') and event in ('booking_open', 'booking_accepted'):
                doc['activeClientId'] = data['activeClientId']
            self._touch(doc)
            for field, value in doc['draft'].items():
                old_value = old_draft.get(field)
                if isinstance(value, dict) and field in ('form', 'captureThumbs', 'licensePaths', 'captureLists', 'bookingDraft', 'panelImages', 'submissionDraft'):
                    for key, entry in value.items():
                        if not isinstance(old_value, dict) or old_value.get(key) != entry:
                            doc['_versions'][field + '.' + key] = doc['revision']
                elif old_value != value:
                    doc['_versions'][field] = doc['revision']
        return self.snapshot()

    def mark_submitted(self, session_id, record_id):
        with self.transaction() as doc:
            if doc['sessionId'] != session_id:
                raise SessionConflict('检测会话已切换')
            doc['phase'], doc['submittedId'] = 'submitted', record_id
            self._touch(doc)
        return self.snapshot()


def configure_store(store):
    global _store
    _store = store


def get_store():
    return _store


def broadcast(snapshot):
    if snapshot:
        from app.websocket.handler import socketio
        socketio.emit('message', {'type': 'inspection_session', 'timestamp': int(time.time() * 1000), 'data': snapshot}, to='inspection_sessions')


def record_event(event, data=None):
    if _store is None:
        return
    import logging
    try:
        payload = dict(data or {})
        from app.api.booking import get_booking_state
        payload['booking'] = dict(get_booking_state())
        broadcast(_store.record(event, payload))
    except Exception:
        logging.getLogger(__name__).exception('检测会话保存失败：%s', event)
