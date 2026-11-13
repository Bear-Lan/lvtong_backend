"""数据库参数目录。首次以现有文件/环境变量建档，后续以config表为准。

保存只写库；重启后端时整批启用，避免检测半程切换地址、命令或距离。
导入此模块不访问数据库、不发设备命令，隔离测试可使用SQLite。
"""
from __future__ import annotations

import copy
import ipaddress
import json
import math
import re
import string
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import Boolean, Column, Integer, JSON, MetaData, String, Table, Text, select, update

metadata = MetaData()
config_table = Table('config', metadata,
    Column('key', String(128), primary_key=True), Column('value', JSON, nullable=False),
    Column('category', String(32), nullable=False), Column('description', Text, nullable=False),
    Column('revision', Integer, nullable=False, default=0),
    Column('restart_required', Boolean, nullable=False, default=True),
    Column('updated_by', String(128), nullable=False, default=''))
_active = {}
_store = None
_lock = threading.RLock()
RETIRED_DISTANCE_RULES = frozenset({'led_step5_plc_yellow', 'led_step6', 'led_step7'})


def retire_legacy_distance_rules(workflow):
    """检测中不再按旧近距阈值切黄灯或覆盖由语音步骤驱动的LED。"""
    result = copy.deepcopy(workflow)
    result['thresholds'] = [rule for rule in result.get('thresholds', [])
                            if rule.get('id') not in RETIRED_DISTANCE_RULES]
    return result


def defaults():
    from config import devices
    directory = Path(__file__).resolve().parents[2] / 'config'
    items = {}
    for name, value in vars(devices).items():
        if name.isupper() and name != 'IMAGE_STORAGE_ROOT':
            category = 'transport' if name.startswith(('MCAST_', 'RADAR_TRANSPORT')) else 'middleware'
            items[f'{category}.{name}'] = copy.deepcopy(value)
    scheduler = retire_legacy_distance_rules(json.loads((directory / 'scheduler_config.json').read_text(encoding='utf-8-sig')))
    scheduler['gateRelease']['requiredFrames'] = 5
    scheduler['tailCaptureOffsetMeters'] = 5.0
    items['scheduler.workflow'] = scheduler
    items['voice.announce'] = json.loads((directory / 'voice_announce.json').read_text(encoding='utf-8-sig'))
    items['voice.playbackDurationSec'] = 3.0
    items['voice.varmcFreshSec'] = 5.0
    from app.devices.plc import _BOOL_KEYS
    keys = set(_BOOL_KEYS) | {'camcap', 'geomcap', 'frontvoiceannounce', 'backvoiceannounce',
                             'frontleddisplay', 'backleddisplay', 'leddisplay'}
    items['commands.templates'] = {key: key + '={value}' for key in sorted(keys)}
    items['commands.templates'].update({'xraycap.start': 'xraycap=true&{length}&{vehicle_type}',
                                      'xraycap.stop': 'xraycap=false'})
    items['commands.prefix'] = '$MDRMC'
    items['led.steps'] = {'1': 'DISPLAY1', '2': 'DISPLAY2', '3': 'DISPLAY3', '4': 'DISPLAY4',
                          '5': 'DISPLAY5', '6': 'DISPLAY5', '7': 'DISPLAY4'}
    items.update({'capture.pollIntervalSec': .2, 'capture.maxPollSec': 180.0,
                  'capture.connectTimeoutSec': 3.0, 'capture.readTimeoutSec': 12.0})
    return items


def _finite(value, low, high):
    return type(value) in (float, int) and math.isfinite(value) and low <= value <= high


def validate(key, value, baseline):
    if key not in baseline:
        raise ValueError('未知配置项：' + key)
    if key == 'scheduler.workflow':
        if not isinstance(value, dict) or not isinstance(value.get('thresholds'), list) or not isinstance(value.get('plcStateRules'), list):
            raise ValueError('调度配置须包含thresholds及plcStateRules数组')
        gate = value.get('gateRelease', {})
        if not _finite(gate.get('minClearDistance'), 0, 1000) or type(gate.get('requiredFrames')) is not int or not 1 <= gate['requiredFrames'] <= 100:
            raise ValueError('净空距离或连续帧数无效')
        if not _finite(value.get('tailCaptureOffsetMeters'), 0, 100):
            raise ValueError('车尾拍照偏移距离无效')
        ids = set()
        for threshold in value['thresholds']:
            if isinstance(threshold, dict) and threshold.get('id') in RETIRED_DISTANCE_RULES:
                raise ValueError('旧距离灯色/LED规则已停用；检测中不能由距离恢复空闲提示')
            if (not isinstance(threshold, dict) or not isinstance(threshold.get('id'), str) or threshold['id'] in ids
                    or not _finite(threshold.get('minDistance'), -1000, 10000)
                    or not _finite(threshold.get('maxDistance'), -1000, 10000)
                    or threshold['minDistance'] > threshold['maxDistance']
                    or type(threshold.get('isActive')) is not bool or not isinstance(threshold.get('deviceActions'), dict)):
                raise ValueError('距离触发规则无效或ID重复')
            ids.add(threshold['id'])
            for action in threshold['deviceActions'].values():
                if not isinstance(action, dict) or not isinstance(action.get('action'), str) or not isinstance(action.get('params', {}), dict):
                    raise ValueError('设备动作配置无效')
        rule_ids = set()
        for rule in value['plcStateRules']:
            if (not isinstance(rule, dict) or not isinstance(rule.get('ruleId'), str) or rule['ruleId'] in rule_ids
                    or type(rule.get('isActive')) is not bool or rule.get('ruleType') not in ('activation', 'trigger')
                    or not isinstance(rule.get('conditions'), list) or not isinstance(rule.get('deviceIds', []), list)):
                raise ValueError('PLC规则无效')
            rule_ids.add(rule['ruleId'])
            for condition in rule['conditions']:
                if not isinstance(condition, dict) or not isinstance(condition.get('stateKey'), str) or type(condition.get('expectedValue')) is not bool:
                    raise ValueError('PLC状态条件无效')
    elif key == 'commands.templates':
        if not isinstance(value, dict) or set(value) != set(baseline[key]):
            raise ValueError('命令模板须保留完整业务键')
        for name, template in value.items():
            allowed = {'length', 'vehicle_type'} if name == 'xraycap.start' else set() if name == 'xraycap.stop' else {'value'}
            if not isinstance(template, str) or not template or len(template) > 256 or any(c in template for c in '\r\n,'):
                raise ValueError('命令模板不可包含换行、逗号或为空')
            fields = set()
            for _, field, spec, conversion in string.Formatter().parse(template):
                if field is not None:
                    if spec or conversion: raise ValueError('命令模板不支持格式转换')
                    fields.add(field)
            if fields != allowed:
                raise ValueError(f'{name}的模板参数须为{sorted(allowed)}')
    elif key == 'commands.prefix':
        if not isinstance(value, str) or not re.fullmatch(r'\$[A-Z][A-Z0-9]{1,15}', value):
            raise ValueError('命令报文头无效')
    elif key == 'voice.announce':
        from app.services.command_announce import validate_config
        validate_config(value)
    elif key == 'led.steps':
        if not isinstance(value, dict) or set(value) != set(baseline[key]) or any(not isinstance(v, str) or not re.fullmatch(r'DISPLAY[1-9]', v) for v in value.values()):
            raise ValueError('LED步骤编号无效')
    elif key.startswith(('voice.', 'capture.')):
        bounds = (.02, 30) if key.endswith('pollIntervalSec') else (.1, 3600)
        if not _finite(value, *bounds): raise ValueError('时长参数无效')
    elif key.endswith('MCAST_GROUP'):
        if not ipaddress.IPv4Address(value).is_multicast: raise ValueError('必须是IPv4组播地址')
    elif key.endswith('MCAST_PORT'):
        if type(value) is not int or not 1 <= value <= 65535: raise ValueError('端口无效')
    elif key.endswith(('MCAST_IFACE_IP', 'MCAST_RECV_IFACE_IP')):
        if value: ipaddress.IPv4Address(value)
    elif key.endswith('RADAR_TRANSPORT'):
        if value not in ('multicast', 'unicast'): raise ValueError('雷达传输方式无效')
    elif key.endswith(('_URL', '_BASE')):
        if not isinstance(value, str) or (value and (urlsplit(value).scheme not in ('http', 'https') or not urlsplit(value).hostname)):
            raise ValueError('URL须为http/https地址或空字符串')
    elif isinstance(baseline[key], bool):
        if type(value) is not bool: raise ValueError('须为布尔值')
    elif type(baseline[key]) in (float, int):
        if not _finite(value, .000001, 10000): raise ValueError('数值参数无效')
    elif not isinstance(value, str) or len(value) > 2048:
        raise ValueError('文本参数无效')
    json.dumps(value, allow_nan=False)


class SystemConfigStore:
    def __init__(self, engine):
        self.engine, self.baseline, self.lock = engine, defaults(), threading.RLock()

    def initialize(self):
        config_table.create(self.engine, checkfirst=True)
        with self.lock, self.engine.begin() as conn:
            existing = set(conn.execute(select(config_table.c.key)).scalars())
            for key, value in self.baseline.items():
                if key not in existing:
                    conn.execute(config_table.insert().values(key=key, value=value, category=key.split('.')[0],
                        description=key, revision=0, restart_required=True, updated_by='initial'))
            row = conn.execute(select(config_table).where(config_table.c.key == 'scheduler.workflow').with_for_update()).mappings().one()
            upgraded = retire_legacy_distance_rules(row['value'])
            if upgraded != row['value']:
                conn.execute(update(config_table).where(config_table.c.key == 'scheduler.workflow').values(
                    value=upgraded, revision=row['revision'] + 1, updated_by='migration:retire-distance-idle'))

    def rows(self):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(config_table).order_by(config_table.c.key)).mappings()]

    def save(self, values, actor='', revisions=None):
        if not isinstance(values, dict) or not values or len(values) > 100:
            raise ValueError('values须为非空配置字典')
        for key, value in values.items(): validate(key, value, self.baseline)
        with self.lock, self.engine.begin() as conn:
            rows = {row['key']: row for row in conn.execute(select(config_table).where(config_table.c.key.in_(values)).with_for_update()).mappings()}
            for key, value in values.items():
                row = rows[key]
                if revisions is not None and revisions.get(key) != row['revision']:
                    raise ConfigConflict('配置已被修改，请重新读取：' + key)
                conn.execute(update(config_table).where(config_table.c.key == key).values(value=value, revision=row['revision'] + 1, updated_by=actor))
        return self.rows()


class ConfigConflict(ValueError):
    pass


def activate(store):
    """只在服务初始化时调用；维护API不调用，运行中检测保持同一批参数。"""
    global _store, _active
    values = {row['key']: row['value'] for row in store.rows()}
    for key, value in values.items(): validate(key, value, store.baseline)
    with _lock:
        _store, _active = store, copy.deepcopy(values)
    import config
    import config.devices
    settings = {key.split('.', 1)[1]: value for key, value in values.items() if key.startswith(('middleware.', 'transport.'))}
    for module in [config, config.devices] + [sys.modules[name] for name in (
        'app.api.booking', 'app.api.file', 'app.services.goods_detect', 'app.services.mask_loading_rate', 'app.services.truck_real_detect') if name in sys.modules]:
        for name, value in settings.items():
            if hasattr(module, name): setattr(module, name, value)
    from app.services import command_announce, lane_announce, body_camera_fetch, progressive_image_fetch, geometry_capture
    command_announce.PLAYBACK_DURATION_SEC = values['voice.playbackDurationSec']
    lane_announce.VARMC_FRESH_SEC = values['voice.varmcFreshSec']
    for module in (body_camera_fetch, progressive_image_fetch, geometry_capture):
        module.POLL_INTERVAL_SEC = values['capture.pollIntervalSec']
        module.MAX_POLL_SEC = values['capture.maxPollSec']
        module.POLL_TIMEOUT = (values['capture.connectTimeoutSec'], values['capture.readTimeoutSec'])
    from app.services import capture_service
    capture_service.CATEGORY_DEVICE_MAP.update(head=config.HEAD_CAPTURE_DEVICE_ID, tail=config.TAIL_CAPTURE_DEVICE_ID, top=config.TOP_CAPTURE_DEVICE_ID)


def value(key, fallback=None):
    with _lock: return copy.deepcopy(_active.get(key, fallback))


def get_store():
    return _store


def configure_for_test(values=None):
    global _active, _store
    with _lock: _active, _store = copy.deepcopy(values or {}), None


def render_command(command):
    key, separator, raw = command.partition('=')
    templates = value('commands.templates', {})
    if key == 'xraycap' and separator:
        parts = raw.split('&')
        name = 'xraycap.start' if parts[0] == 'true' else 'xraycap.stop'
        template = templates.get(name)
        if template:
            if name.endswith('.start'):
                if len(parts) != 3: raise ValueError('光机启动参数不完整')
                return template.format(length=parts[1], vehicle_type=parts[2])
            return template
    template = templates.get(key)
    return template.format(value=raw) if template and separator else command
