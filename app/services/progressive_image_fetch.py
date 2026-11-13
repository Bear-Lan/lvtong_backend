"""面板拉图

触发：雷达距离 25–28m 时轮询 200 透视（:5003/xray/image），每 0.2s 一帧。
启扫：抬杆时组播 $MDRMC,1,xraycap=true&{车头长}&{车型}（与 camcap 同时）。

  200 透视：持续 GET :5003/xray/image，直到 Capture-Finish / JSON 或超时；160/拼接暂不接
  车身 cam（body1）：抬杆 capture + 雷达 25–28m 时 body_camera_fetch 拉 :5002
  车顶/车侧雷达（vtop/vside）改由前端切换显示时按需拉

预约雷达 appt 仍走 booking /radar-image，不在本模块。
"""
from __future__ import annotations

import logging
import threading
import time

from app.services import temp_image_cache
from app.services.image_preview import jpeg_preview_bytes
from app.services.middleware_http import middleware_get

logger = logging.getLogger('progressive_image_fetch')

POLL_INTERVAL_SEC = 0.2
POLL_TIMEOUT: tuple[float, float] = (3.0, 12.0)
MAX_POLL_SEC = 180.0
# 按需单帧拉取（雷达顶/侧）仍用递增超时重试
_ATTEMPT_TIMEOUTS: tuple[tuple[float, float], ...] = (
    (3.0, 12.0),
    (3.0, 20.0),
    (3.0, 30.0),
)
FAIL_RETRY_INTERVAL_SEC = 0.6


def _fetch_one(
    name: str,
    url: str,
    stop: threading.Event,
    *,
    timeouts: tuple[tuple[float, float], ...] = _ATTEMPT_TIMEOUTS,
    tag: str = '',
) -> str | None:
    """GET 成功返回 temp URL；按 timeouts 递增重试，仍不成返回 None。"""
    url = (url or '').strip()
    if not url:
        print(f'[PROG-IMG] {name} URL 空，跳过', flush=True)
        return None

    prefix = f'{tag} ' if tag else ''
    for attempt, timeout in enumerate(timeouts, start=1):
        if stop.is_set():
            return None
        try:
            print(
                f'[PROG-IMG] {prefix}{name} GET #{attempt}/{len(timeouts)} '
                f'read={timeout[1]:.0f}s {url}',
                flush=True,
            )
            t0 = time.monotonic()
            resp = middleware_get(url, timeout=timeout)
            elapsed = time.monotonic() - t0
            if resp.status_code == 200 and resp.content:
                preview = jpeg_preview_bytes(resp.content)
                temp_url = temp_image_cache.put_preview_with_original(
                    resp.content, preview,
                )
                print(
                    f'[PROG-IMG] {prefix}{name} ok {elapsed:.2f}s '
                    f'preview={len(preview)} raw={len(resp.content)} → {temp_url}',
                    flush=True,
                )
                return temp_url
            print(
                f'[PROG-IMG] {prefix}{name} #{attempt} HTTP {resp.status_code} '
                f'({elapsed:.2f}s)',
                flush=True,
            )
        except Exception as e:
            print(f'[PROG-IMG] {prefix}{name} #{attempt} 失败: {e}', flush=True)
        if attempt < len(timeouts) and not stop.wait(FAIL_RETRY_INTERVAL_SEC):
            continue
        break
    return None


def _panel_push_allowed() -> bool:
    """重置后 is_detection=False，丢弃仍在途的 progressive 帧。"""
    try:
        from app.api.booking import _booking_state
        return bool(_booking_state.get('is_detection'))
    except Exception:
        return False


class ProgressivePanelFetcher:
    """200 透视轮询协调器（单例）。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, '_initialized', False):
            return
        self._initialized = True
        self._started = False
        self._guard = threading.Lock()
        self._stop_flag = threading.Event()
        self._worker: threading.Thread | None = None
        self._frame_count = 0

    def stop(self) -> None:
        with self._guard:
            self._stop_flag.set()
            self._started = False
            print('[PROG-IMG] 停止', flush=True)

    def start_for_detection(
        self,
        *,
        car_height: float = 3.0,
        xray_enabled: bool = True,
    ) -> None:
        with self._guard:
            if self._started:
                print('[PROG-IMG] 已在拉图，忽略重复启动', flush=True)
                return
            self._started = True
            self._stop_flag.clear()
            self._frame_count = 0

        def _run() -> None:
            try:
                self._fetch_all(xray_enabled=xray_enabled)
            except Exception as e:
                logger.warning('[PROG-IMG] 拉图异常: %s', e)
                print(f'[PROG-IMG] 拉图异常: {e}', flush=True)
            finally:
                with self._guard:
                    self._started = False

        self._worker = threading.Thread(
            target=_run, daemon=True, name='x200-image-poll',
        )
        self._worker.start()

    def _fetch_all(self, *, xray_enabled: bool) -> None:
        from config import TRANSPARENT_IMAGE_URL, USE_PLACEHOLDER_IMAGES

        if not USE_PLACEHOLDER_IMAGES:
            print('[PROG-IMG] USE_PLACEHOLDER_IMAGES=false，跳过', flush=True)
            return
        if not xray_enabled:
            print('[PROG-IMG] xray 未启用，跳过', flush=True)
            return

        x200_url = (TRANSPARENT_IMAGE_URL or '').strip()
        if not x200_url:
            print('[PROG-IMG] TRANSPARENT_IMAGE_URL 空，跳过 200 透视', flush=True)
            return

        self._poll_x200(x200_url)

    def _poll_x200(self, url: str) -> None:
        from app.services.body_camera_fetch import _is_capture_finished, _looks_like_image

        t0 = time.monotonic()
        print(
            f'[PROG-IMG] 开始 200 透视轮询 {url} '
            f'interval={POLL_INTERVAL_SEC}s',
            flush=True,
        )

        completed = False
        try:
            while not self._stop_flag.is_set():
                if time.monotonic() - t0 > MAX_POLL_SEC:
                    print(
                        f'[PROG-IMG] 200 透视超时结束 frames={self._frame_count} '
                        f'limit={MAX_POLL_SEC:.0f}s',
                        flush=True,
                    )
                    break

                try:
                    resp = middleware_get(url, timeout=POLL_TIMEOUT)
                except Exception as e:
                    print(f'[PROG-IMG] x200 GET 失败: {e}', flush=True)
                    if self._stop_flag.wait(POLL_INTERVAL_SEC):
                        break
                    continue

                finished, reason = _is_capture_finished(resp)
                if finished:
                    if _looks_like_image(resp):
                        self._push_x200_frame(resp.content)
                    print(
                        f'[PROG-IMG] 200 透视采集结束 reason={reason} '
                        f'frames={self._frame_count}',
                        flush=True,
                    )
                    completed = True
                    break

                if resp.status_code == 200 and _looks_like_image(resp):
                    self._push_x200_frame(resp.content)
                elif resp.status_code != 200:
                    print(
                        f'[PROG-IMG] x200 HTTP {resp.status_code}，继续轮询',
                        flush=True,
                    )

                if self._stop_flag.wait(POLL_INTERVAL_SEC):
                    break
        finally:
            try:
                from app.devices.xray import send_xray_capture
                if not completed:
                    send_xray_capture(False)
            except Exception as e:
                print(f'[PROG-IMG] xraycap=false 失败: {e}', flush=True)

    def _push_x200_frame(self, raw: bytes) -> None:
        if not _panel_push_allowed():
            print('[PROG-IMG] 200 透视帧丢弃（非检测中）', flush=True)
            return
        from app.services import temp_image_cache
        from app.services.image_preview import jpeg_preview_bytes

        try:
            preview = jpeg_preview_bytes(raw)
            temp_url = temp_image_cache.put_preview_with_original(raw, preview)
            self._frame_count += 1
            self._push_slot('x200', temp_url)
            if self._frame_count == 1 or self._frame_count % 10 == 0:
                print(
                    f'[PROG-IMG] 推送 200 透视帧 #{self._frame_count} → {temp_url}',
                    flush=True,
                )
        except Exception as e:
            print(f'[PROG-IMG] 200 透视推送失败: {e}', flush=True)

    def _push_slot(self, name: str, temp_url: str) -> None:
        from app.websocket.handler import push_image_ready_group

        try:
            if name.startswith('body'):
                push_image_ready_group('body', {name[-1]: temp_url}, group=1)
            elif name == 'x200':
                push_image_ready_group('transparent', {'1': temp_url}, group=1)
            elif name == 'x160':
                push_image_ready_group('transparent', {'2': temp_url}, group=1)
            elif name == 'merge':
                push_image_ready_group('transparent', {'3': temp_url}, group=1)
            else:
                return
        except Exception as e:
            print(f'[PROG-IMG] WS 推送失败 {name}: {e}', flush=True)


def start_progressive_panel_fetch() -> None:
    """雷达 25–28m 时以 0.2s 间隔轮询 200 透视图。"""
    try:
        from app.api.booking import _booking_state
        car_height = float(_booking_state.get('car_height') or 3.0)
        xray_enabled = bool(_booking_state.get('is_check_xray', True))
    except Exception:
        car_height = 3.0
        xray_enabled = True
    ProgressivePanelFetcher().start_for_detection(
        car_height=car_height,
        xray_enabled=xray_enabled,
    )


def stop_progressive_panel_fetch() -> None:
    ProgressivePanelFetcher().stop()
    try:
        from app.devices.xray import send_xray_capture
        send_xray_capture(False)
    except Exception as e:
        print(f'[PROG-IMG] xraycap=false 失败: {e}', flush=True)


def fetch_body_radar_image(view: str) -> str | None:
    """按需拉车顶/车侧雷达图，推 WS 并返回 temp URL。view: top | side"""
    from config import BODY_SIDE_IMAGE_URL, BODY_TOP_IMAGE_URL, USE_PLACEHOLDER_IMAGES

    if not USE_PLACEHOLDER_IMAGES:
        return None

    v = (view or '').strip().lower()
    if v == 'top':
        name, url, slot = 'body2', BODY_TOP_IMAGE_URL, '2'
    elif v == 'side':
        name, url, slot = 'body3', BODY_SIDE_IMAGE_URL, '3'
    else:
        return None

    stop = threading.Event()
    temp_url = _fetch_one(name, url, stop)
    if not temp_url:
        return None

    from app.websocket.handler import push_image_ready_group
    try:
        push_image_ready_group('body', {slot: temp_url}, group=1)
    except Exception as e:
        print(f'[PROG-IMG] WS 推送 {name} 失败: {e}', flush=True)
    return temp_url
