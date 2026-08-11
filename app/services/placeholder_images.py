"""临时占位出图

现场中间层未齐时，以下六类暂时共用 PLACEHOLDER_IMAGE_URL：
  1. X光 200 透视
  2. X光 160 透视
  3. 拼接图
  4. 车身图
  5. 车身顶/侧（点云面板位）
  6. 预约雷达图（booking /radar-image 另走同 URL）

拉一次远程图 → 放内存临时缓存 → 推 image_ready（/api/temp-images/<id>）。
正式落盘仅在用户点提交时由 persist_to_storage 完成。
"""
from __future__ import annotations

import logging
import threading

import requests

from app.services import temp_image_cache

logger = logging.getLogger('placeholder_images')


def _placeholder_url() -> str:
    from config import PLACEHOLDER_IMAGE_URL, PLACEHOLDER_IMAGE_ID
    url = (PLACEHOLDER_IMAGE_URL or '').strip()
    if url:
        return url
    return f'http://192.168.88.201:5006/xray/image?id={PLACEHOLDER_IMAGE_ID}'


def fetch_placeholder_bytes(url: str | None = None) -> bytes:
    target = (url or _placeholder_url()).strip()
    print(f'[PLACEHOLDER] GET {target}', flush=True)
    resp = requests.get(target, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f'占位图 HTTP {resp.status_code}: {target}')
    if not resp.content:
        raise RuntimeError(f'占位图为空: {target}')
    return resp.content


def publish_placeholder_panel_images(scan_id: str | None = None) -> dict:
    """拉取占位图并推送 body + transparent 两组（共 6 个面板位，预览不落盘）。

    返回 {body: {1,2,3}, transparent: {1,2,3}, sourceUrl}
    """
    from config import PLACEHOLDER_IMAGE_ID

    sid = (scan_id or PLACEHOLDER_IMAGE_ID or '').strip() or 'placeholder'
    url = _placeholder_url()
    if 'id=' not in url and sid:
        sep = '&' if '?' in url else '?'
        url = f'{url}{sep}id={sid}'

    data = fetch_placeholder_bytes(url)
    # 同一份字节只缓存一次；六面板共用 URL，提交时再按字段分别落正式库
    preview_url = temp_image_cache.put_and_url(data)

    body = {'1': preview_url, '2': preview_url, '3': preview_url}
    transparent = {'1': preview_url, '2': preview_url, '3': preview_url}

    from app.websocket.handler import push_image_ready_group

    push_image_ready_group('body', body, group=1)
    push_image_ready_group('transparent', transparent, group=1)

    result = {
        'sourceUrl': url,
        'body': body,
        'transparent': transparent,
    }
    print(
        f'[PLACEHOLDER] 已推送六面板位(预览未落盘) url={preview_url} bytes={len(data)}',
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
            logger.warning('[PLACEHOLDER] 推送失败: %s', e)
            print(f'[PLACEHOLDER] 推送失败: {e}', flush=True)

    threading.Thread(target=_run, daemon=True, name='placeholder-images').start()
