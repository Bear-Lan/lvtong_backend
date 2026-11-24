"""本站司机资料缓存、增量同步和人工关联。仅通过内网连接 AI 接收服务。"""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

import requests
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, select, update, delete
from app.services.inspection_session import SessionConflict
from app.services.system_config import value

metadata = MetaData()
records = Table('driver_intake_record', metadata,
    Column('key', String(140), primary_key=True), Column('station_id', String(64), nullable=False),
    Column('uuid', String(36), nullable=False), Column('received_at', Integer, nullable=False),
    Column('payload', Text, nullable=False))
sync_state = Table('driver_intake_sync', metadata,
    Column('station_id', String(64), primary_key=True), Column('source_id', String(36), nullable=False),
    Column('cursor', Integer, nullable=False), Column('last_success', Integer), Column('last_error', Text, nullable=False))
bindings = Table('driver_intake_binding', metadata,
    Column('key', String(140), primary_key=True), Column('session_id', String(64), nullable=False),
    Column('submitted_id', Integer), Column('actor', String(128), nullable=False))
audit = Table('driver_intake_audit', metadata,
    Column('id', String(36), primary_key=True), Column('session_id', String(64), nullable=False),
    Column('payload', Text, nullable=False))


def record_key(station, rid):
    return station + ':' + rid


def normalized_plate(plate):
    # Whitespace/case only: do not silently guess OCR character substitutions.
    return re.sub(r'\s+', '', str(plate or '')).upper()


def finalize_binding(conn, doc, record_id):
    if not doc.get('intakeSelection'):
        return
    conn.execute(update(bindings).where(bindings.c.session_id == doc['sessionId']).values(submitted_id=record_id))


class IntakeClient:
    def __init__(self, base, key, ca=True):
        from urllib.parse import urlsplit
        p = urlsplit(base)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError('AI接收服务地址无效')
        if len(key) < 32:
            raise ValueError('尚未配置AI接收服务只读凭证')
        self.base, self.key, self.ca = base.rstrip('/'), key, ca

    def get(self, path, stream=False):
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(self.base + path, headers={'Authorization': 'Bearer ' + self.key},
                timeout=(5, 20), allow_redirects=False, verify=self.ca, stream=stream)
        if response.status_code == 409:
            response.close()
            raise SourceReset()
        if response.status_code != 200:
            response.close()
            raise RuntimeError('AI接收服务HTTP ' + str(response.status_code))
        return response

    def changes(self, source, cursor):
        from urllib.parse import urlencode
        with self.get('/api/v1/changes?' + urlencode({'source_id': source, 'cursor': cursor, 'limit': 100})) as response:
            body = response.json()
        if body.get('code') != 'OK' or not isinstance(body.get('data'), dict):
            raise ValueError('接收服务响应无效')
        return body['data']

    def photo(self, rid, photo_id):
        return self.get(f'/api/v1/records/{rid}/photos/{photo_id}', stream=True)


class SourceReset(Exception):
    pass


class DriverIntake:
    def __init__(self, sessions, image_root, settings=None, client=None):
        self.sessions, self.engine = sessions, sessions.engine
        self.root = Path(image_root).resolve() / 'driver-intake'
        self.root.mkdir(parents=True, exist_ok=True)
        self.cfg = settings or {'enabled': value('intake.enabled', False), 'station_id': value('intake.station_id', ''),
            'base_url': value('intake.base_url', ''), 'window_seconds': value('intake.windowSeconds', 480),
            'poll_seconds': value('intake.pollIntervalSec', 2)}
        self.client = client
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.RLock()
        metadata.create_all(self.engine)
        station = self.cfg['station_id']
        if station:
            with self.engine.begin() as conn:
                if conn.execute(select(sync_state).where(sync_state.c.station_id == station)).first() is None:
                    conn.execute(sync_state.insert().values(station_id=station, source_id='', cursor=0, last_error=''))

    def connection(self):
        if self.client is None:
            secret_file = Path(os.environ.get('INTAKE_CONNECTION_FILE',
                str(Path(__file__).resolve().parents[2] / 'config/intake.local.json')))
            secret = json.loads(secret_file.read_text()) if secret_file.exists() else {}
            self.client = IntakeClient(self.cfg['base_url'], os.environ.get('INTAKE_READ_KEY', secret.get('read_key', '')),
                os.environ.get('INTAKE_CA_BUNDLE', secret.get('ca_bundle', '')) or True)
        return self.client

    def photo_path(self, item, photo):
        return self.root / item['station_id'] / item['uuid'] / (photo['id'] + '.jpg')

    def photo_url(self, item, photo):
        relative = self.photo_path(item, photo).relative_to(self.sessions.image_root)
        return '/api/images/' + relative.as_posix()

    def valid_photo(self, item, photo):
        p = self.photo_path(item, photo)
        return p.is_file() and p.stat().st_size == photo['size'] and hashlib.sha256(p.read_bytes()).hexdigest() == photo['sha256']

    def materialize(self, item):
        for photo in item['photos']:
            if self.valid_photo(item, photo):
                continue
            path = self.photo_path(item, photo)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(photo['id'] + '-' + uuid.uuid4().hex + '.part')
            digest, count = hashlib.sha256(), 0
            try:
                with self.connection().photo(item['uuid'], photo['id']) as response, temp.open('wb') as output:
                    started = time.monotonic()
                    for chunk in response.iter_content(65536):
                        count += len(chunk)
                        if count > photo['size'] or time.monotonic() - started > 90:
                            raise ValueError('照片下载超限')
                        digest.update(chunk)
                        output.write(chunk)
                    if count != photo['size'] or digest.hexdigest() != photo['sha256']:
                        raise ValueError('照片完整性校验失败')
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp, path)
                if os.name != 'nt':
                    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            finally:
                temp.unlink(missing_ok=True)

    def validate_record(self, item):
        if not isinstance(item, dict) or item.get('station_id') != self.cfg['station_id']:
            raise ValueError('接收资料站点不一致')
        if str(uuid.UUID(item['uuid'])) != item['uuid'] or type(item['received_at']) is not int or item['received_at'] < 0:
            raise ValueError('接收资料标识或时间无效')
        if not isinstance(item.get('phone'), str) or not re.fullmatch(r'1[3-9]\d{9}', item['phone']):
            raise ValueError('手机号无效')
        photos = item.get('photos')
        if not isinstance(photos, list) or not 2 <= len(photos) <= 10 or len({p['id'] for p in photos}) != len(photos):
            raise ValueError('照片列表无效')
        kinds = [p.get('kind') for p in photos]
        if kinds.count('license_main') != 1 or kinds.count('license_trailer') > 1 or 'goods' not in kinds:
            raise ValueError('行驶证或货物照不完整')
        for p in photos:
            if str(uuid.UUID(p['id'])) != p['id'] or p.get('kind') not in ('license_main', 'license_trailer', 'goods'):
                raise ValueError('照片标识无效')
            if type(p['size']) is not int or not 0 < p['size'] <= 16*1024**2 or not re.fullmatch(r'[a-f0-9]{64}', p['sha256']):
                raise ValueError('照片校验字段无效')
        return item

    def tick(self):
        snap = self.sessions.snapshot()
        if not self.cfg['enabled'] or snap['phase'] != 'detecting':
            return False
        lower = snap.get('detectionStartedAt', int(time.time())) - self.cfg['window_seconds']
        station = self.cfg['station_id']
        # A shared DB row serializes sync across backend processes; browser count
        # never changes the number of upstream polls. Each page commits atomically.
        with self.lock, self.engine.begin() as conn:
            state = conn.execute(select(sync_state).where(sync_state.c.station_id == station).with_for_update(skip_locked=True)).mappings().first()
            if state is None:
                return False
            if state['last_success'] and state['last_success'] + self.cfg['poll_seconds'] > time.time():
                return False
            client = self.connection()
            try:
                page = client.changes(state['source_id'], state['cursor'])
            except SourceReset:
                # Existing cache/bindings remain; the replacement source replays
                # from zero. UUIDs stay immutable and deduplicated.
                conn.execute(update(sync_state).where(sync_state.c.station_id == station).values(source_id='', cursor=0, last_success=None))
                return True
            if page.get('station_id') != station or not isinstance(page.get('items'), list):
                raise ValueError('接收服务站点或增量格式无效')
            if not isinstance(page.get('source_id'), str) or str(uuid.UUID(page['source_id'])) != page['source_id']:
                raise ValueError('接收服务来源无效')
            if state['source_id'] and page['source_id'] != state['source_id']:
                raise ValueError('增量来源改变')
            previous = state['cursor']
            for event in page['items']:
                if type(event.get('seq')) is not int or event['seq'] <= previous:
                    raise ValueError('增量序号无效')
                previous = event['seq']
                item = self.validate_record(event['record'])
                key = record_key(station, item['uuid'])
                old = conn.execute(select(records.c.payload).where(records.c.key == key)).scalar_one_or_none()
                if old:
                    old = json.loads(old)
                    if any(old[k] != item[k] for k in ('station_id', 'uuid', 'received_at', 'phone', 'photos')):
                        raise ValueError('上传记录的原始内容发生变化')
                if item['received_at'] >= lower:
                    self.materialize(item)
                if old is None:
                    conn.execute(records.insert().values(key=key, station_id=station, uuid=item['uuid'], received_at=item['received_at'], payload=json.dumps(item, ensure_ascii=False)))
                else:
                    conn.execute(update(records).where(records.c.key == key).values(payload=json.dumps(item, ensure_ascii=False)))
            if type(page.get('cursor')) is not int or page['cursor'] != previous:
                raise ValueError('增量游标无效')
            conn.execute(update(sync_state).where(sync_state.c.station_id == station).values(
                source_id=page['source_id'], cursor=page['cursor'], last_success=None if page.get('has_more') else int(time.time()), last_error=''))
        return bool(page.get('has_more'))

    def run_once(self):
        try:
            return self.tick()
        except Exception as exc:
            # No raw URLs/headers/payloads in status: they can contain credentials.
            with self.engine.begin() as conn:
                conn.execute(update(sync_state).where(sync_state.c.station_id == self.cfg['station_id']).values(last_error=type(exc).__name__))
            return False

    def start(self):
        if not self.cfg['enabled'] or self.thread:
            return
        def loop():
            while not self.stop_event.is_set():
                more = self.run_once()
                self.stop_event.wait(.05 if more else self.cfg['poll_seconds'])
        self.thread = threading.Thread(target=loop, name='driver-intake-sync', daemon=True)
        self.thread.start()

    def get(self, rid):
        with self.engine.connect() as conn:
            payload = conn.execute(select(records.c.payload).where(records.c.key == record_key(self.cfg['station_id'], rid))).scalar_one_or_none()
        if payload is None:
            raise KeyError('资料不存在或不属于本站')
        return json.loads(payload)

    def candidates(self, query='', earlier=False, offset=0, limit=50):
        snap = self.sessions.snapshot()
        lower = snap.get('detectionStartedAt', int(time.time())) - self.cfg['window_seconds']
        scan = snap.get('draft', {}).get('passcode', {})
        plate = normalized_plate(scan.get('vehicleDisplayId') or snap.get('draft', {}).get('form', {}).get('plate'))
        with self.engine.connect() as conn:
            stmt = select(records).where(records.c.station_id == self.cfg['station_id'])
            if not earlier:
                stmt = stmt.where(records.c.received_at >= lower)
            rows = conn.execute(stmt).mappings().all()
            used = {r['key']: dict(r) for r in conn.execute(select(bindings)).mappings()}
            state = conn.execute(select(sync_state).where(sync_state.c.station_id == self.cfg['station_id'])).mappings().first()
        items = []
        for row in rows:
            item = json.loads(row['payload'])
            if query and not any(query.upper() in str(item.get(k, '')).upper() for k in ('uuid', 'plate', 'phone', 'trailer_plate')):
                continue
            recognized = normalized_plate(item.get('plate'))
            item['recommendation'] = 'match' if plate and recognized == plate else 'mismatch' if plate and recognized else 'pending'
            use = used.get(row['key'])
            # Canceled/reset sessions release automatically; submitted history stays used.
            item['usage'] = 'current' if use and use['session_id'] == snap['sessionId'] else 'used' if use and use['submitted_id'] is not None else 'available'
            item['photos'] = [{**p, 'url': f"/api/driver-intake/records/{item['uuid']}/photos/{p['id']}"} for p in item['photos']]
            items.append(item)
        items.sort(key=lambda x: ({'match': 0, 'pending': 1, 'mismatch': 2}[x['recommendation']], -x['received_at'], x['uuid']))
        return {'items': items[offset:offset+limit], 'total': len(items), 'sessionId': snap['sessionId'],
            'revision': snap['revision'], 'phase': snap['phase'], 'selection': snap.get('intakeSelection'),
            'plate': plate, 'window_seconds': self.cfg['window_seconds'], 'window_start': lower,
            'enabled': self.cfg['enabled'], 'station_id': self.cfg['station_id'], 'sync': dict(state) if state else None}

    def associate(self, rid, session_id, revision, actor, replace=False, allow_mismatch=False):
        item = self.get(rid) if rid else None
        if item:
            self.materialize(item)  # Never hold the detection row lock over network I/O.
        with self.sessions.transaction(include_connection=True) as (doc, conn):
            if doc['phase'] != 'detecting' or doc['sessionId'] != session_id or doc['revision'] != revision:
                raise SessionConflict('本次检测或版本已变化，请刷新后重新选择')
            before_draft = copy.deepcopy(doc['draft'])
            previous = doc.get('intakeSelection')
            if item:
                key = record_key(self.cfg['station_id'], rid)
                use = conn.execute(select(bindings).where(bindings.c.key == key)).mappings().first()
                if use and use['submitted_id'] is not None:
                    raise SessionConflict('资料已用于已提交的检测')
                current_plate = normalized_plate(doc.get('draft', {}).get('passcode', {}).get('vehicleDisplayId') or doc.get('draft', {}).get('form', {}).get('plate'))
                if current_plate and item.get('plate') and normalized_plate(item['plate']) != current_plate and not allow_mismatch:
                    raise SessionConflict('车牌与当前检测不一致，请核对后明确确认')
                if previous and previous['uuid'] != rid and not replace:
                    raise SessionConflict('当前检测已有资料，请明确选择更换')
                # Use an explicit overwrite decision if any target field/photo exists.
                draft = doc['draft']
                if not replace and (draft.get('form', {}).get('phone') or any(draft.get('licensePaths', {}).get(k) for k in ('license', 'licenseGc')) or draft.get('captureLists', {}).get('goods') or draft.get('captureThumbs', {}).get('goods')):
                    raise SessionConflict('当前已有手机号或照片，请确认替换对应资料')
                if use:
                    conn.execute(delete(bindings).where(bindings.c.key == key))
                conn.execute(bindings.insert().values(key=key, session_id=session_id, actor=actor))
            if previous and (not item or previous['uuid'] != rid):
                conn.execute(delete(bindings).where(bindings.c.key == record_key(self.cfg['station_id'], previous['uuid']), bindings.c.submitted_id.is_(None)))
            if item:
                # Save pre-import values for a reversible unlink. A replacement
                # retains the original baseline, rather than the previous import.
                baseline = previous.get('baseline') if previous else None
                fields = [('form', 'phone'), ('licensePaths', 'license'), ('licensePaths', 'licenseGc'),
                          ('licensePaths', 'licenseStitched'), ('captureThumbs', 'license'), ('captureThumbs', 'goods'), ('captureLists', 'goods'),
                          ('manualPhotos', 'license'), ('manualPhotos', 'goods')]
                if baseline is None:
                    baseline = {a+'.'+b: copy.deepcopy(doc['draft'].get(a, {}).get(b)) for a,b in fields}
                main = next(p for p in item['photos'] if p['kind'] == 'license_main')
                trailer = next((p for p in item['photos'] if p['kind'] == 'license_trailer'), None)
                goods = [self.photo_url(item,p) for p in item['photos'] if p['kind'] == 'goods']
                values = {'form.phone': item['phone'], 'licensePaths.license': self.photo_url(item,main),
                    'licensePaths.licenseGc': self.photo_url(item,trailer) if trailer else '',
                    'licensePaths.licenseStitched': '', 'captureThumbs.license': self.photo_url(item,main),
                    'captureThumbs.goods': goods[0], 'captureLists.goods': goods,
                    'manualPhotos.license': True, 'manualPhotos.goods': True}
                for field, val in values.items():
                    a,b = field.split('.')
                    doc['draft'].setdefault(a,{})[b] = val
                doc['intakeSelection'] = {'uuid': rid, 'station_id': self.cfg['station_id'], 'phone': item['phone'],
                    'plate': item.get('plate', ''), 'selectedBy': actor, 'selectedAt': int(time.time()), 'baseline': baseline, 'imported': values}
            else:
                # Preserve subsequent human changes; only undo untouched imported values.
                if previous:
                    for field, imported in previous.get('imported', {}).items():
                        a,b = field.split('.')
                        container = doc['draft'].setdefault(a,{})
                        if a == 'manualPhotos':
                            related = ['licensePaths.license', 'licensePaths.licenseGc', 'licensePaths.licenseStitched', 'captureThumbs.license'] if b == 'license' else ['captureThumbs.goods', 'captureLists.goods']
                            if any(before_draft.get(x.split('.')[0], {}).get(x.split('.')[1]) != previous['imported'].get(x) for x in related):
                                continue
                        if container.get(b) == imported:
                            old = previous.get('baseline', {}).get(field)
                            # Explicit empty values also clear clients that merge
                            # partial form/image maps when applying shared drafts.
                            container[b] = old if old is not None else ([] if a == 'captureLists' else False if a == 'manualPhotos' else '')
                doc.pop('intakeSelection', None)
            self.sessions._touch(doc, actor)
            # Protect every modified draft path from stale PC/Pad edits.
            for a in ('form', 'licensePaths', 'captureThumbs', 'captureLists', 'manualPhotos'):
                for b in set(before_draft.get(a, {})) | set(doc['draft'].get(a, {})):
                    doc['_versions'][a+'.'+b] = doc['revision']
            conn.execute(audit.insert().values(id=str(uuid.uuid4()), session_id=session_id,
                payload=json.dumps({'action': 'associate' if item else 'unlink', 'uuid': rid,
                    'previous_uuid': previous['uuid'] if previous else None, 'actor': actor,
                    'allow_mismatch': allow_mismatch, 'replace': replace, 'at': int(time.time())})))
        return self.sessions.snapshot()
