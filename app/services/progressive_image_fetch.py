"""面板拉图（当前中间层为单帧）

触发：雷达距离 <33m（调度器 xray_capture_trigger）。
策略：各 type GET 成功拿到图即结束（不再轮询等 Capture_Finish）。
失败时短暂重试有限次。

  串行拉 cam / vtop / vside / x160 /（高车）x200
  （中间件单机并发弱，并行易整波 Read timeout；浏览器单开正常）
  merge：等高车且 x160、x200 都成功后再 GET 一次

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

# (connect, read)；中间件串行后一般很快，读超时缩短避免干等
REQUEST_TIMEOUT = (2.0, 8.0)
# 失败重试（成功即停）
FAIL_RETRY_COUNT = 3
FAIL_RETRY_INTERVAL_SEC = 0.3
# 两路之间略歇
BETWEEN_FETCH_SEC = 0.1
TALL_CAR_HEIGHT_M = 2.8


def _fetch_one(name: str, url: str, stop: threading.Event) -> str | None:
    """GET 成功返回 temp URL；失败重试后仍不成返回 None。"""
    url = (url or '').strip()
    if not url:
        print(f'[PROG-IMG] {name} URL 空，跳过', flush=True)
        return None

    for attempt in range(1, FAIL_RETRY_COUNT + 1):
        if stop.is_set():
            return None
        try:
            print(f'[PROG-IMG] {name} GET #{attempt} {url}', flush=True)
            t0 = time.monotonic()
            resp = middleware_get(url, timeout=REQUEST_TIMEOUT)
            elapsed = time.monotonic() - t0
            if resp.status_code == 200 and resp.content:
                preview = jpeg_preview_bytes(resp.content)
                temp_url = temp_image_cache.put_and_url(preview, 'image/jpeg')
                print(
                    f'[PROG-IMG] {name} ok {elapsed:.2f}s bytes={len(preview)} → {temp_url}',
                    flush=True,
                )
                return temp_url
            print(
                f'[PROG-IMG] {name} #{attempt} HTTP {resp.status_code} ({elapsed:.2f}s)',
                flush=True,
            )
        except Exception as e:
            print(f'[PROG-IMG] {name} #{attempt} 失败: {e}', flush=True)
        if attempt < FAIL_RETRY_COUNT and not stop.wait(FAIL_RETRY_INTERVAL_SEC):
            continue
        break
    return None


class ProgressivePanelFetcher:
    """车身 + 透视单帧拉图协调器（单例）。"""

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

        def _run() -> None:
            try:
                self._fetch_all(car_height=car_height, xray_enabled=xray_enabled)
            except Exception as e:
                logger.warning('[PROG-IMG] 拉图异常: %s', e)
                print(f'[PROG-IMG] 拉图异常: {e}', flush=True)
            finally:
                with self._guard:
                    self._started = False

        self._worker = threading.Thread(
            target=_run, daemon=True, name='panel-images-once',
        )
        self._worker.start()

    def _fetch_all(self, *, car_height: float, xray_enabled: bool) -> None:
        from config import (
            BODY_IMAGE_URL,
            BODY_SIDE_IMAGE_URL,
            BODY_TOP_IMAGE_URL,
            STITCH_IMAGE_URL,
            TRANSPARENT_IMAGE_URL,
            USE_PLACEHOLDER_IMAGES,
            YR_TRANSPARENT_IMAGE_URL,
        )
        if not USE_PLACEHOLDER_IMAGES:
            print('[PROG-IMG] USE_PLACEHOLDER_IMAGES=false，跳过', flush=True)
            return

        tall = float(car_height or 0) > TALL_CAR_HEIGHT_M
        slots: list[tuple[str, str]] = [
            ('body1', BODY_IMAGE_URL),
            ('body2', BODY_TOP_IMAGE_URL),
            ('body3', BODY_SIDE_IMAGE_URL),
        ]
        if xray_enabled:
            slots.append(('x160', YR_TRANSPARENT_IMAGE_URL))
            if tall:
                slots.append(('x200', TRANSPARENT_IMAGE_URL))
            else:
                print(
                    f'[PROG-IMG] 车高={car_height}≤{TALL_CAR_HEIGHT_M}，只拉 x160，不拉 merge',
                    flush=True,
                )

        print(
            f'[PROG-IMG] 串行拉取 {len(slots)} 路 '
            f'height={car_height} xray={xray_enabled} tall={tall}',
            flush=True,
        )

        results: dict[str, str] = {}
        for i, (name, url) in enumerate(slots):
            if self._stop_flag.is_set():
                break
            if not (url or '').strip():
                continue
            if i > 0 and BETWEEN_FETCH_SEC > 0:
                if self._stop_flag.wait(BETWEEN_FETCH_SEC):
                    break
            try:
                temp_url = _fetch_one(name, url, self._stop_flag)
            except Exception as e:
                print(f'[PROG-IMG] {name} 异常: {e}', flush=True)
                continue
            if temp_url:
                results[name] = temp_url
                self._push_slot(name, temp_url)

        # merge：高车且双路透视都成功后再拉一次
        stitch = (STITCH_IMAGE_URL or '').strip()
        if (
            xray_enabled
            and tall
            and stitch
            and results.get('x160')
            and results.get('x200')
            and not self._stop_flag.is_set()
        ):
            print('[PROG-IMG] x160+x200 已成功，拉取 merge', flush=True)
            if BETWEEN_FETCH_SEC > 0:
                self._stop_flag.wait(BETWEEN_FETCH_SEC)
            merge_url = _fetch_one('merge', stitch, self._stop_flag)
            if merge_url:
                results['merge'] = merge_url
                self._push_slot('merge', merge_url)
        elif xray_enabled and tall and stitch:
            print(
                '[PROG-IMG] 跳过 merge：'
                f'x160={"ok" if results.get("x160") else "缺"} '
                f'x200={"ok" if results.get("x200") else "缺"}',
                flush=True,
            )

        # 整包再推一次：分片推送时前端若漏接，仍能一次填满六格
        self._push_batch(results)
        print(f'[PROG-IMG] 本轮结束 got={list(results.keys())}', flush=True)

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
            print(f'[PROG-IMG] WS image_ready 已推 {name} → {temp_url}', flush=True)
        except Exception as e:
            print(f'[PROG-IMG] WS 推送失败 {name}: {e}', flush=True)

    def _push_batch(self, results: dict[str, str]) -> None:
        from app.websocket.handler import push_image_ready_group

        body = {
            k: results[src]
            for k, src in (('1', 'body1'), ('2', 'body2'), ('3', 'body3'))
            if results.get(src)
        }
        transparent = {
            k: results[src]
            for k, src in (('1', 'x200'), ('2', 'x160'), ('3', 'merge'))
            if results.get(src)
        }
        try:
            if body:
                push_image_ready_group('body', body, group=1)
                print(f'[PROG-IMG] WS 整包 body keys={list(body.keys())}', flush=True)
            if transparent:
                push_image_ready_group('transparent', transparent, group=1)
                print(
                    f'[PROG-IMG] WS 整包 transparent keys={list(transparent.keys())}',
                    flush=True,
                )
        except Exception as e:
            print(f'[PROG-IMG] WS 整包推送失败: {e}', flush=True)


def start_progressive_panel_fetch() -> None:
    """供调度器在距离 <33m 时调用。"""
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
