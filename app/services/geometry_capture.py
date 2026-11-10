"""放行开启几何采集，随车身图轮询真实 LAZ；取消/重置隔离在途响应。"""
import logging
import hashlib
import threading
import time

from app.devices.mcast_cmd import send_geomcap
from app.services.body_camera_fetch import _header_capture_finish
from app.services.body_radar_projection import project_body_response
from app.services.capture_guard import CaptureBlockedError, ensure_capture_allowed
from app.services.middleware_http import middleware_get_payload

logger = logging.getLogger(__name__)
POLL_INTERVAL_SEC = 0.5
MAX_POLL_SEC = 180.0
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


def _notify():
    from app.websocket.handler import socketio
    try:
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
    global _result, _state, _error, _frame
    import config
    settings = {name: getattr(config, name) for name in
                ('RADAR_POINT_CLOUD_URL', 'RADAR_LAZ_METERS_PER_UNIT', 'RADAR_LAZ_RESOLUTION')}
    deadline = time.monotonic() + MAX_POLL_SEC
    attempts = 0
    last_digest = None
    logger.info('LAZ 开始轮询 generation=%s url=%s', generation, settings['RADAR_POINT_CLOUD_URL'])
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            with _lock:
                if generation == _generation and not stop.is_set():
                    _state = 'timeout'
                    _error = '三分钟内未收到 Capture-Finish: true' + ('，未获取到有效点云' if not _result else '')
                    _notify()
            return
        try:
            guard_generation = ensure_capture_allowed()
        except CaptureBlockedError:
            stop.wait(min(POLL_INTERVAL_SEC, remaining))
            continue
        attempts += 1
        try:
            response = middleware_get_payload(settings['RADAR_POINT_CLOUD_URL'],
                                              timeout=(min(3., remaining), min(8., remaining)))
            ensure_capture_allowed(guard_generation)
            if stop.is_set():
                return
            # JSON/status=false、空响应和 HTTP 错误都不是采集完成。
            finished = response.status_code == 200 and _header_capture_finish(response).lower() == 'true'
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
                _error = error
                if finished:
                    _state = 'finished'
                    if not _result and not error:
                        _error = '采集已结束，但未返回有效点云'
                if result or finished or error:
                    _notify()
            if finished:
                logger.info('LAZ 采集结束 generation=%s attempts=%s frames=%s', generation, attempts, _frame)
                return
        except CaptureBlockedError:
            stop.wait(POLL_INTERVAL_SEC)
            continue
        except Exception as exc:
            logger.warning('LAZ 拉取失败，继续等待 generation=%s: %s', generation, exc)
        stop.wait(POLL_INTERVAL_SEC)
    # 正常结束/超时均不发送 geomcap=false；中间件自行结束，只有取消/重置发送。
