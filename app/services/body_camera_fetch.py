"""车身相机（:5002 拉图）

  1. 抬杆放行 → 组播 $MDRMC,1,camcap=true（开始采集）
  2. 雷达距离 25–28m → 持续 GET /camera/body/image，每 0.2s 一帧推 WS body.1
  3. 正常拉取结束只退出轮询；超时、异常或手动停止才发送 camcap=false
"""
from __future__ import annotations

import logging
import threading
import time

from app.services.capture_guard import CaptureBlockedError, ensure_capture_allowed

logger = logging.getLogger('body_camera_fetch')

POLL_INTERVAL_SEC = 0.2
POLL_TIMEOUT: tuple[float, float] = (3.0, 12.0)
# 安全上限，避免无限拉
MAX_POLL_SEC = 180.0


class BodyCameraFetcher:
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
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._started = False
        self._worker: threading.Thread | None = None
        self._frame_count = 0

    def stop(self) -> None:
        with self._guard:
            self._stop.set()
            self._started = False
            print('[BODY-CAM] 停止拉图', flush=True)
        _send_camcap(False)

    def trigger_capture(self) -> None:
        """抬杆放行时调用：仅发 capture，不启动轮询。"""
        def _run() -> None:
            try:
                self._trigger_capture()
            except Exception as e:
                logger.warning('[BODY-CAM] capture 异常: %s', e)
                print(f'[BODY-CAM] capture 异常: {e}', flush=True)

        threading.Thread(
            target=_run, daemon=True, name='body-camera-capture',
        ).start()

    def start_poll(self) -> None:
        """雷达距离 25–28m 时调用：后台持续拉 image。"""
        try:
            ensure_capture_allowed()
        except CaptureBlockedError:
            return
        with self._guard:
            if self._started:
                print('[BODY-CAM] 已在拉图，忽略重复启动', flush=True)
                return
            self._started = True
            self._stop.clear()
            self._frame_count = 0

        def _run() -> None:
            try:
                self._poll_images()
            except Exception as e:
                logger.warning('[BODY-CAM] 异常: %s', e)
                print(f'[BODY-CAM] 异常: {e}', flush=True)
            finally:
                with self._guard:
                    self._started = False

        self._worker = threading.Thread(
            target=_run, daemon=True, name='body-camera-poll',
        )
        self._worker.start()

    def _trigger_capture(self) -> bool:
        try:
            ensure_capture_allowed()
        except CaptureBlockedError:
            return False
        ok = _send_camcap(True)
        if ok:
            print('[BODY-CAM] camcap=true 组播已发', flush=True)
        else:
            print('[BODY-CAM] camcap=true 组播失败', flush=True)
        return ok

    def _poll_images(self) -> None:
        from config.devices import BODY_CAMERA_IMAGE_URL
        from app.services.middleware_http import middleware_get
        from app.services import temp_image_cache
        from app.services.image_preview import jpeg_preview_bytes
        from app.websocket.handler import push_image_ready_group

        url = (BODY_CAMERA_IMAGE_URL or '').strip()
        if not url:
            print('[BODY-CAM] BODY_CAMERA_IMAGE_URL 空，跳过', flush=True)
            return

        t0 = time.monotonic()
        print(f'[BODY-CAM] 开始持续拉图 {url}', flush=True)

        completed = False
        try:
            while not self._stop.is_set():
                if time.monotonic() - t0 > MAX_POLL_SEC:
                    print(
                        f'[BODY-CAM] 超时结束 frames={self._frame_count} '
                        f'limit={MAX_POLL_SEC:.0f}s',
                        flush=True,
                    )
                    break

                try:
                    generation = ensure_capture_allowed()
                except CaptureBlockedError:
                    if self._stop.wait(POLL_INTERVAL_SEC):
                        break
                    continue

                try:
                    resp = middleware_get(url, timeout=POLL_TIMEOUT)
                    ensure_capture_allowed(generation)
                except CaptureBlockedError:
                    continue
                except Exception as e:
                    print(f'[BODY-CAM] image GET 失败: {e}', flush=True)
                    if self._stop.wait(POLL_INTERVAL_SEC):
                        break
                    continue

                finished, reason = _is_capture_finished(resp)
                if finished:
                    if _looks_like_image(resp):
                        self._push_frame(
                            resp.content, temp_image_cache, jpeg_preview_bytes,
                            push_image_ready_group, generation,
                        )
                    print(
                        f'[BODY-CAM] 采集结束 reason={reason} '
                        f'frames={self._frame_count}',
                        flush=True,
                    )
                    completed = True
                    break

                if resp.status_code == 200 and _looks_like_image(resp):
                    self._push_frame(
                        resp.content, temp_image_cache, jpeg_preview_bytes,
                        push_image_ready_group, generation,
                    )
                elif resp.status_code != 200:
                    print(
                        f'[BODY-CAM] image HTTP {resp.status_code}，继续轮询',
                        flush=True,
                    )

                if self._stop.wait(POLL_INTERVAL_SEC):
                    break
        finally:
            if not completed:
                _send_camcap(False)

    def _push_frame(self, raw: bytes, cache, jpeg_preview_bytes, push_fn, generation=None) -> None:
        try:
            ensure_capture_allowed(generation)
            preview = jpeg_preview_bytes(raw)
            ensure_capture_allowed(generation)
            temp_url = cache.put_preview_with_original(raw, preview)
            self._frame_count += 1
            push_fn('body', {'1': temp_url}, group=1)
            if self._frame_count == 1 or self._frame_count % 10 == 0:
                print(
                    f'[BODY-CAM] 推送车身帧 #{self._frame_count} → {temp_url}',
                    flush=True,
                )
        except Exception as e:
            print(f'[BODY-CAM] 推送失败: {e}', flush=True)


def _header_capture_finish(resp) -> str:
    for k, v in (resp.headers or {}).items():
        if str(k).lower().replace('_', '-') == 'capture-finish':
            return str(v or '').strip()
    return ''


def _is_capture_finished(resp) -> tuple[bool, str]:
    flag = _header_capture_finish(resp).lower()
    if flag in ('true', '1', 'yes', 'on'):
        return True, f'header Capture-Finish={flag}'

    ctype = (resp.headers.get('Content-Type') or '').lower()
    if 'json' in ctype:
        return True, f'content-type={ctype}'

    raw = resp.content or b''
    head = raw.lstrip()[:1]
    if head in (b'{', b'['):
        try:
            resp.json()
            return True, 'body-json'
        except Exception:
            pass
    return False, ''


def _looks_like_image(resp) -> bool:
    raw = resp.content or b''
    if len(raw) < 24:
        return False
    ctype = (resp.headers.get('Content-Type') or '').lower()
    if 'json' in ctype or 'text/' in ctype:
        return False
    if 'image/' in ctype:
        return True
    # JPEG / PNG 魔数
    if raw[:2] == b'\xff\xd8' or raw[:8] == b'\x89PNG\r\n\x1a\n':
        return True
    return False


def _send_camcap(enabled: bool) -> bool:
    from app.devices.mcast_cmd import send_camcap
    return send_camcap(enabled)


def trigger_body_camera_capture() -> None:
    """抬杆放行 → 组播 camcap=true。"""
    BodyCameraFetcher().trigger_capture()


def start_body_camera_poll() -> None:
    """雷达 25–28m → 开始 0.2s 轮询拉图。"""
    BodyCameraFetcher().start_poll()


def stop_body_camera_fetch() -> None:
    BodyCameraFetcher().stop()
