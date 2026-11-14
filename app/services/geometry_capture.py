"""放行开启几何采集，随车身图轮询真实 LAZ；取消/重置隔离在途响应。"""
import logging
import hashlib
import threading
import time

from app.devices.mcast_cmd import send_geomcap
from app.services.body_camera_fetch import (
    POLL_INTERVAL_SEC, POLL_TIMEOUT, MAX_POLL_SEC, _is_capture_finished,
)
from app.services.body_radar_projection import project_body_response
from app.services.capture_guard import CaptureBlockedError, ensure_capture_allowed
from app.services.middleware_http import middleware_get_payload

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_generation = 0
_armed = False
_started = False
_poll_requested = False
_poll_started = False
_stop = threading.Event()
_result = None
_state = 'idle'
_error = ''
_frame = 0


def _snapshot():
    return {**(_result or {}), 'capture': {'generation': _generation, 'state': _state,
            'frame': _frame, 'error': _error, 'hasData': bool(_result)}}


def get_geometry_snapshot() -> dict:
    """仅读取本轮缓存，不发送命令，也不额外访问中间件。"""
    with _lock:
        return _snapshot()


def refresh_geometry_snapshot(session_id: str) -> dict:
    """手动补读中间件当前LAZ并持久化；不发送geomcap或其他设备命令。"""
    global _result, _state, _error, _frame
    import config
    from app.services.inspection_session import get_store, SessionConflict, broadcast
    store = get_store()
    if store is None:
        raise RuntimeError('检测同步尚未初始化')

    def check_session():
        snapshot = store.snapshot()
        if not session_id or snapshot['sessionId'] != session_id or snapshot['phase'] != 'detecting':
            raise SessionConflict('请在当前检测中刷新点云')

    check_session()
    with _lock:
        generation, frame = _generation, _frame
    guard = ensure_capture_allowed()
    settings = {name: getattr(config, name) for name in
                ('RADAR_POINT_CLOUD_URL', 'RADAR_LAZ_METERS_PER_UNIT', 'RADAR_LAZ_RESOLUTION')}
    response = middleware_get_payload(settings['RADAR_POINT_CLOUD_URL'], timeout=POLL_TIMEOUT)
    if response.status_code != 200:
        raise ValueError(f'点云接口 HTTP {response.status_code}')
    if not response.content.startswith(b'LASF'):
        raise ValueError('点云接口尚未返回有效LAZ，请稍后重试')
    result = project_body_response(response, settings)
    ensure_capture_allowed(guard)
    with _lock, store.lock:
        check_session()
        if generation != _generation:
            raise SessionConflict('本次采集已切换，请刷新当前检测')
        # 自动采集已收到更新帧时，不能被较早发起的手动请求覆盖。
        if frame != _frame:
            return _snapshot()
        _result, _frame, _error = result, _frame + 1, ''
        if _state not in ('capturing', 'polling'):
            finished, _ = _is_capture_finished(response)
            _state = 'finished' if finished else 'ready'
        store.record('geometry', _snapshot())
        store.record('geometry_status', {'capture': _snapshot()['capture']})
        saved = store.snapshot()
        broadcast(saved)
        return _snapshot()


def _notify():
    from app.websocket.handler import socketio
    try:
        from app.services.inspection_session import record_event
        record_event('geometry_status', {'capture': _snapshot()['capture']})
        socketio.emit('message', {'type': 'body_radar', 'timestamp': int(time.time() * 1000),
                                 'data': _snapshot()['capture']})
    except Exception:
        logger.exception('LAZ 状态推送失败，客户端仍可读取缓存')


def prepare_geometry_capture() -> int:
    global _generation, _armed, _started, _poll_requested, _poll_started, _stop, _result, _state, _error, _frame
    with _lock:
        _stop.set()
        _generation += 1
        _armed, _started, _poll_requested, _poll_started = True, False, False, False
        _stop = threading.Event()
        _result, _state, _error, _frame = None, 'armed', '', 0
        _notify()
        return _generation


def _launch_poll():
    global _poll_started, _state
    if _poll_started or not _poll_requested or not _started:
        return
    _poll_started, _state = True, 'polling'
    _notify()
    threading.Thread(target=_poll_laz, args=(_generation, _stop), daemon=True,
                     name='geometry-laz-poll').start()


def start_geometry_capture(generation: int | None) -> bool:
    global _started, _state, _error
    with _lock:
        if not _armed or generation != _generation:
            return False
        if _started:
            return True
        if not send_geomcap(True):
            _error = '几何采集开启命令发送失败'
            _notify()
            return False
        _started, _state, _error = True, 'capturing', ''
        _notify()
        _launch_poll()
        return True


def start_geometry_poll(generation: int | None) -> None:
    global _poll_requested
    with _lock:
        if not _armed or generation != _generation or _stop.is_set():
            return
        _poll_requested = True
        # 距离线程可能先于放行后台线程到达；待 geomcap=true 成功后再拉取。
        _launch_poll()


def stop_geometry_capture() -> bool:
    global _generation, _armed, _started, _result, _state, _error, _frame
    with _lock:
        _stop.set()
        _armed, _started = False, False
        _generation += 1
        _result, _state, _error, _frame = None, 'stopped', '', 0
        _notify()
        return send_geomcap(False)


def _poll_laz(generation: int, stop: threading.Event) -> None:
    completed = False
    try:
        completed = bool(_poll_laz_frames(generation, stop))
    finally:
        # 正常结束只退出轮询；超时或异常退出才停止采集。
        # 取消/新一轮已单独处理，旧线程不能误停下一辆车。
        with _lock:
            if not completed and generation == _generation and not stop.is_set() and _started:
                if not send_geomcap(False):
                    logger.warning('LAZ 停止采集命令发送失败 generation=%s', generation)


def _poll_laz_frames(generation: int, stop: threading.Event) -> bool | None:
    global _result, _state, _error, _frame
    import config
    settings = {name: getattr(config, name) for name in
                ('RADAR_POINT_CLOUD_URL', 'RADAR_LAZ_METERS_PER_UNIT', 'RADAR_LAZ_RESOLUTION')}
    deadline = time.monotonic() + MAX_POLL_SEC
    attempts = 0
    last_digest = None
    waiting_logged = False
    logger.info('LAZ 开始轮询 generation=%s url=%s', generation, settings['RADAR_POINT_CLOUD_URL'])
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            with _lock:
                if generation == _generation and not stop.is_set():
                    _state = 'timeout'
                    _error = '三分钟内未收到采集结束标志' + ('，未获取到有效点云' if not _result else '')
                    _notify()
            return
        try:
            guard_generation = ensure_capture_allowed()
        except CaptureBlockedError:
            stop.wait(min(POLL_INTERVAL_SEC, remaining))
            continue
        attempts += 1
        try:
            response = middleware_get_payload(settings['RADAR_POINT_CLOUD_URL'], timeout=POLL_TIMEOUT)
            ensure_capture_allowed(guard_generation)
            if stop.is_set():
                return
            # 与车身图共用结束规则：Capture-Finish 或 JSON 响应。
            finished, reason = _is_capture_finished(response)
            result, error = None, ''
            if response.status_code == 200 and response.content.startswith(b'LASF'):
                try:
                    digest = hashlib.sha256(response.content).digest()
                    if digest != last_digest:
                        result = project_body_response(response, settings)
                        ensure_capture_allowed(guard_generation)
                        last_digest = digest
                except CaptureBlockedError:
                    continue
                except Exception as exc:
                    error = f'点云解析失败：{exc}'
            elif response.status_code != 200:
                error = f'点云接口 HTTP {response.status_code}'
            with _lock:
                if generation != _generation or stop.is_set():
                    return
                if result:
                    _result, _frame = result, _frame + 1
                    from app.services.inspection_session import record_event
                    record_event('geometry', _snapshot())
                _error = error
                # 中间件在车辆仍扫描时也会返回无数据JSON/结束标记。
                # 第一份有效LAZ到达前不能因此结束，否则后面生成的文件永远漏取。
                if finished and not _result:
                    if not waiting_logged:
                        logger.warning('LAZ尚未就绪，继续等待 generation=%s reason=%s status=%s bytes=%s',
                                       generation, reason, response.status_code, len(response.content))
                        waiting_logged = True
                    finished = False
                    _error = error or '点云尚未就绪，继续等待'
                if finished:
                    _state = 'finished'
                if result or finished or error:
                    _notify()
            if finished:
                logger.info('LAZ 采集结束 generation=%s reason=%s attempts=%s frames=%s', generation, reason, attempts, _frame)
                return True
        except CaptureBlockedError:
            stop.wait(POLL_INTERVAL_SEC)
            continue
        except Exception as exc:
            logger.warning('LAZ 拉取失败，继续等待 generation=%s: %s', generation, exc)
        stop.wait(POLL_INTERVAL_SEC)
