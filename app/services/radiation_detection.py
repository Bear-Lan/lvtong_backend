"""独立的六秒辐射检测；浏览器不承担停光源计时。

关闭回执来自新鲜 DEVICE 的两个光源状态位，不能以 UDP 发送成功代替。
未确认关闭时持续重发停止指令，并持有全局设备互斥；重启只恢复停机。
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

DURATION = 6.0
LIMIT = 15.0
STOP_COMMANDS = ('xraycap=false', 'xraysource200=false', 'xraysource160=false')


def _plc():
    from app.websocket.handler import get_last_plc_status
    return get_last_plc_status()


def _booking_idle():
    from app.api.booking import get_booking_state
    state = get_booking_state()
    return not any(state.get(key) for key in
                   ('is_detection', 'check_step', 'btn_prebook_state', 'booking_dialog_shown'))


def _send(*commands, **kwargs):
    from app.devices.mcast_cmd import send_mdrmc
    return send_mdrmc(*commands, **kwargs)


def _source_feedback(data):
    """只接受状态字段和独立在线位，不将未知/离线当作已关闭。"""
    if not data or data.get('fresh') is not True:
        return None
    online = {row.get('bit'): row.get('online') for row in data.get('sensors', [])}
    values = [data.get('lightSource200Status'), data.get('lightSource160Status')]
    if any(type(value) is not bool for value in values) or any(online.get(bit) is not True for bit in (9, 10)):
        return None
    return values


class RadiationDetection:
    def __init__(self, journal=None, *, sender=None, plc=None, idle=None,
                 clock=None, wall=None, background=True):
        self._lock = threading.RLock()
        self._sender = sender or _send
        self._plc = plc or _plc
        self._idle = idle or _booking_idle
        self._clock = clock or time.monotonic
        self._wall = wall or time.time
        self._journal = Path(journal) if journal else None
        self._background = background
        self._thread = None
        self._wake = threading.Event()
        self._state = {'runId': '', 'revision': 0, 'active': False, 'phase': 'idle',
                       'startedAt': None, 'finishedAt': None, 'samples': [],
                       'error': '', 'result': None, 'stopAttempts': 0,
                       'bothSourcesOn': False, 'bothSourcesOff': False}
        self._started = 0.0
        self._stop_after = None
        self._stop_issued = None
        self._next_stop = 0.0
        self._stop_sent = set()
        self._abort = ''
        self._latest = None

    def _save(self):
        if self._journal is None:
            return
        self._journal.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._journal.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(self._state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._journal)

    def recover(self):
        """应用启动调用；未完成记录仅恢复停机，绝不再次开启光源。"""
        with self._lock:
            if self._journal is None or not self._journal.exists():
                return
            try:
                previous = json.loads(self._journal.read_text(encoding='utf-8'))
                if not isinstance(previous, dict) or type(previous.get('active')) is not bool:
                    raise ValueError('无效的检测记录')
                self._state.update(previous)
            except Exception:
                self._state.update(active=True, runId=uuid.uuid4().hex)
            if self._state['active']:
                self._abort = '服务曾中断，已恢复停止光源，等待两路关闭确认'
                self._state.update(phase='stopping', error=self._abort)
                self._stop_locked()
                self._launch()

    def _launch(self):
        if self._background:
            self._thread = threading.Thread(target=self._run, args=(self._state['runId'],),
                                            daemon=True, name='radiation-watchdog')
            self._thread.start()

    def start(self):
        with self._lock:
            if self._state['active']:
                raise ValueError('已有辐射检测或光源关闭确认正在进行')
            data = self._plc()
            if _source_feedback(data) != [False, False]:
                raise ValueError('请先确认两个光源在线、均已关闭且状态反馈正常')
            online = {row.get('bit'): row.get('online') for row in data.get('sensors', [])}
            if data.get('stickDownStatus') is not False or online.get(6) is not True:
                raise ValueError('请先确认栏杆已落下且反馈正常')
            if data.get('urgentStopStatus') is not False or not self._idle():
                raise ValueError('请在无预约、无车辆检测且无急停的空闲状态下检测辐射值')
            self._started = self._clock()
            self._state = {'runId': uuid.uuid4().hex, 'revision': self._state['revision'] + 1,
                           'active': True, 'phase': 'measuring',
                           'startedAt': int(self._wall() * 1000), 'finishedAt': None,
                           'samples': [], 'error': '', 'result': None, 'stopAttempts': 0,
                           'bothSourcesOn': False, 'bothSourcesOff': False}
            self._stop_after = self._stop_issued = None
            self._next_stop = 0
            self._stop_sent = set()
            self._abort = ''
            self._wake.clear()
            # 写入持久化待关闭标记失败时禁止开光；先启动计时，再发送开启。
            try:
                self._save()
                self._launch()
            except Exception:
                self._state.update(active=False, phase='failed', error='无法建立检测停机保护，未开启光源')
                raise ValueError(self._state['error'])
            try:
                sent = self._sender('xraysource200=true', 'xraysource160=true',
                                    radiation_token=self._state['runId'])
            except Exception:
                sent = False
            if not sent:
                self._abort = '光源开启命令发送失败，正在停止两个光源'
                self._stop_locked()
            return self._snapshot()

    def record(self, value):
        with self._lock:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                return
            self._latest = {'value': value, 'receivedAt': int(self._wall() * 1000)}
            elapsed = self._clock() - self._started
            if self._state['phase'] == 'measuring' and self._state['active'] and 0 <= elapsed <= DURATION:
                self._state['samples'].append({'time': round(elapsed, 3), 'value': value})
                self._state['revision'] += 1

    def stop(self, run_id, reason='检测已提前停止'):
        with self._lock:
            if self._state['active'] and run_id == self._state['runId']:
                if self._state['phase'] == 'measuring':
                    self._abort = reason
                self._stop_locked()
                self._wake.set()
            return self._snapshot()

    def shutdown(self):
        with self._lock:
            if self._state['active']:
                self._abort = '服务关闭，检测已中断'
                self._stop_locked()

    def _stop_locked(self):
        self._state.update(phase='stopping', error=self._abort)
        if self._stop_issued is None:
            self._stop_issued = self._clock()
        # 任意一条失败也必须继续发送另外两条；每500ms重试直到真实关闭。
        self._state['stopAttempts'] += 1
        for command in STOP_COMMANDS:
            try:
                if self._sender(command):
                    self._stop_sent.add(command)
            except Exception:
                pass
        if self._stop_after is None:
            self._stop_after = int(self._wall() * 1000)
        self._next_stop = self._clock() + 0.5
        self._state['revision'] += 1
        try:
            self._save()
        except Exception:
            self._state['error'] = '停止状态保存失败，正在持续停止两个光源'

    def tick(self):
        with self._lock:
            if not self._state['active']:
                return
            now = self._clock()
            data = self._plc()
            sources = _source_feedback(data)
            if self._state['phase'] == 'measuring':
                elapsed = now - self._started
                if sources == [True, True] and data.get('receivedAt', 0) > self._state['startedAt']:
                    self._state['bothSourcesOn'] = True
                online = {row.get('bit'): row.get('online') for row in (data or {}).get('sensors', [])}
                if elapsed >= DURATION:
                    self._stop_locked()
                elif (sources is None or data.get('stickDownStatus') is not False or online.get(6) is not True
                      or data.get('urgentStopStatus') is not False or not self._idle()):
                    self._abort = '现场条件或设备反馈变化，检测已中断'
                    self._stop_locked()
                elif self._state['bothSourcesOn'] and sources != [True, True]:
                    self._abort = '光源状态变化，检测已中断'
                    self._stop_locked()
            if self._state['phase'] == 'stopping':
                # 必须是停止命令发出之后的新包；旧的关闭缓存不能完成检测。
                if (sources == [False, False] and data.get('receivedAt', 0) > self._stop_after
                        and len(self._stop_sent) == len(STOP_COMMANDS)):
                    samples = self._state['samples']
                    enough = (len(samples) >= 2 and samples[0]['time'] <= 1.5
                              and samples[-1]['time'] >= DURATION - 1
                              and self._state['bothSourcesOn'])
                    if self._abort:
                        phase, result = 'cancelled', None
                    elif not enough:
                        phase, result = 'failed', None
                        self._state['error'] = '辐射数据不足，无法判定结果'
                    else:
                        phase = 'completed'
                        self._state['error'] = ''
                        maximum = max(point['value'] for point in samples)
                        result = {'value': maximum, 'level': 'normal' if maximum <= LIMIT else 'high'}
                    self._state.update(active=False, phase=phase, result=result,
                                       bothSourcesOff=True, finishedAt=int(self._wall() * 1000))
                    self._state['revision'] += 1
                    try:
                        self._save()
                    except Exception:
                        # 已有真实关闭回执；残留active标记使下次启动再次停机。
                        self._state['error'] = '两个光源已关闭，但结果保存失败'
                elif now >= self._next_stop:
                    self._stop_locked()
                    if now - self._stop_issued >= 3:
                        self._state['error'] = '尚未确认两个光源关闭，正在持续发送停止命令，请检查现场'

    def _run(self, run_id):
        while True:
            with self._lock:
                if self._state['runId'] != run_id or not self._state['active']:
                    return
            try:
                self.tick()
            except Exception:
                with self._lock:
                    self._abort = '检测异常，正在停止两个光源'
                    self._stop_locked()
            with self._lock:
                if not self._state['active']:
                    return
            self._wake.wait(0.05)
            self._wake.clear()

    def _snapshot(self):
        state = copy.deepcopy(self._state)
        state.update(durationSeconds=DURATION, threshold=LIMIT, unit='μSv/h',
                     elapsedSeconds=round(min(DURATION, max(0, (self._stop_issued if self._stop_issued is not None else self._clock()) - self._started)), 2)
                     if state['startedAt'] else 0,
                     latest=copy.deepcopy(self._latest), sources=_source_feedback(self._plc()))
        return state

    def status(self):
        with self._lock:
            return self._snapshot()

    @contextmanager
    def command_guard(self, commands, token=None):
        # 与start持同一把锁直到发包结束，避免检查与开启之间的竞态。
        with self._lock:
            allowed = True
            if self._state['active']:
                for command in commands:
                    key, _, raw = command.lower().partition('=')
                    enabling = raw.split('&')[0] == 'true'
                    hazard = ((key.startswith(('xray', 'boombarrier')) and enabling)
                              or (key.startswith('interlock') and raw == 'false'))
                    own_start = (token == self._state['runId'] and self._state['phase'] == 'measuring'
                                 and self._clock() - self._started < DURATION
                                 and key in ('xraysource200', 'xraysource160'))
                    if hazard and not own_start:
                        allowed = False
            yield allowed


_service = RadiationDetection()


def get_radiation_detection():
    return _service


def init_radiation_detection():
    from config import IMAGE_STORAGE_ROOT
    _service._journal = Path(IMAGE_STORAGE_ROOT) / '_runtime' / 'radiation-detection.json'
    _service.recover()
