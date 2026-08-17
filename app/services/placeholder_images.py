"""中间层出图（按 type 拉图）

  车身影像：cam / vtop / vside
  透视影像：x200 / x160（拼接图暂无）
  预约雷达：appt（booking /radar-image 另走 RADAR_HEAD_URL）

拉远程图 → 放内存临时缓存 → 推 image_ready（/api/temp-images/<id>）。
正式落盘仅在用户点提交时由 persist_to_storage 完成。
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from app.services import temp_image_cache

logger = logging.getLogger('placeholder_images')


def _fetch_url_bytes(url: str) -> bytes:
    target = (url or '').strip()
    if not target:
        raise RuntimeError('拉图 URL 为空')
    print(f'[IMAGE] GET {target}', flush=True)
    resp = requests.get(target, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f'拉图 HTTP {resp.status_code}: {target}')
    if not resp.content:
        raise RuntimeError(f'拉图为空: {target}')
    return resp.content


def fetch_placeholder_bytes(url: str | None = None) -> bytes:
    """兼容旧调用：未传 url 时默认拉 cam。"""
    if url:
        return _fetch_url_bytes(url)
    from config import BODY_IMAGE_URL, image_fetch_url
    return _fetch_url_bytes(BODY_IMAGE_URL or image_fetch_url('cam'))


def _preview_url_for(source_url: str) -> str:
    """拉图 → 预览压缩 → 临时缓存 URL；同 source_url 可复用预约已拉预览。"""
    from app.services.image_preview import get_remembered_preview, preview_from_raw

    preview = get_remembered_preview(source_url)
    if preview:
        print(
            f'[IMAGE] 复用预览 cache bytes={len(preview)} url={source_url}',
            flush=True,
        )
    else:
        data = _fetch_url_bytes(source_url)
        preview = preview_from_raw(data, source_url=source_url)
        print(
            f'[IMAGE] 预览 bytes={len(preview)} (raw={len(data)}) url={source_url}',
            flush=True,
        )
    return temp_image_cache.put_and_url(preview, 'image/jpeg')


def publish_placeholder_panel_images(scan_id: str | None = None) -> dict:
    """按 type 分别拉取并推送 body + transparent。

    body: 1=cam 车身, 2=vtop 车顶雷达, 3=vside 车侧雷达
    transparent: 1=x200, 2=x160, 3=拼接（暂空）

    返回 {body: {1,2,3}, transparent: {1,2,3}, sources}
    """
    from config import (
        BODY_IMAGE_URL,
        BODY_SIDE_IMAGE_URL,
        BODY_TOP_IMAGE_URL,
        TRANSPARENT_IMAGE_URL,
        YR_TRANSPARENT_IMAGE_URL,
    )

    # scan_id 保留参数兼容；新接口用 type，不再拼 id=
    _ = scan_id

    jobs = {
        'body1': BODY_IMAGE_URL,           # cam
        'body2': BODY_TOP_IMAGE_URL,       # vtop
        'body3': BODY_SIDE_IMAGE_URL,      # vside
        'x200': TRANSPARENT_IMAGE_URL,     # x200
        'x160': YR_TRANSPARENT_IMAGE_URL,  # x160
    }

    previews: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_preview_url_for, url): key
            for key, url in jobs.items()
            if (url or '').strip()
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                previews[key] = fut.result()
            except Exception as e:
                logger.warning('[IMAGE] %s 失败: %s', key, e)
                print(f'[IMAGE] {key} 失败: {e}', flush=True)

    body = {
        '1': previews.get('body1', ''),
        '2': previews.get('body2', ''),
        '3': previews.get('body3', ''),
    }
    transparent = {
        '1': previews.get('x200', ''),
        '2': previews.get('x160', ''),
        '3': '',  # 拼接图暂无
    }

    from app.websocket.handler import push_image_ready_group

    push_image_ready_group('body', body, group=1)
    import time as _time
    _time.sleep(0.05)
    push_image_ready_group('transparent', transparent, group=1)

    result = {
        'sources': {k: v for k, v in jobs.items() if (v or '').strip()},
        'body': body,
        'transparent': transparent,
    }
    print(
        f'[IMAGE] 已推送面板 '
        f'body={list(body.values())} transparent={list(transparent.values())}',
        flush=True,
    )
    return result


def publish_placeholder_async(scan_id: str | None = None) -> None:
    """后台线程拉取，避免阻塞受理接口"""
    try:
        from config import USE_PLACEHOLDER_IMAGES
        if not USE_PLACEHOLDER_IMAGES:
            return
    except ImportError:
        pass

    def _run():
        try:
            publish_placeholder_panel_images(scan_id)
        except Exception as e:
            logger.warning('[IMAGE] 推送失败: %s', e)
            print(f'[IMAGE] 推送失败: {e}', flush=True)

    threading.Thread(target=_run, daemon=True, name='panel-images').start()
