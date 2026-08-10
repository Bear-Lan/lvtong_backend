"""临时占位出图

现场中间层未齐时，以下六类暂时共用 PLACEHOLDER_IMAGE_URL：
  1. X光 200 透视
  2. X光 160 透视
  3. 拼接图
  4. 车身图
  5. 车身顶/侧（点云面板位）
  6. 预约雷达图（booking /radar-image 另走同 URL）

拉一次远程图，存多份分类，再按前端 mock 形态推 image_ready。
"""
from __future__ import annotations

import logging
import threading

import requests

from app.services.image_store import ImageStore

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
    """拉取占位图并推送 body + transparent 两组（共 6 个面板位）。

    返回 {body: {1,2,3}, transparent: {1,2,3}, sourceUrl}
    """
    from config import PLACEHOLDER_IMAGE_ID

    sid = (scan_id or PLACEHOLDER_IMAGE_ID or '').strip() or 'placeholder'
    # 若配置已是完整 URL 则直接用；否则拼 id
    url = _placeholder_url()
    if 'id=' not in url and sid:
        sep = '&' if '?' in url else '?'
        url = f'{url}{sep}id={sid}'

    data = fetch_placeholder_bytes(url)
    store = ImageStore()

    # 六类落盘（雷达留给 /api/booking/radar-image，此处面板 5+1 命名）
    body1 = store.save_image_bytes(data, 'body', prefix=sid)
    body2 = store.save_image_bytes(data, 'top', prefix=sid)
    body3 = store.save_image_bytes(data, 'side', prefix=sid)
    x200 = store.save_image_bytes(data, 'transparent200', prefix=sid)
    x160 = store.save_image_bytes(data, 'transparent160', prefix=sid)
    mosaic = store.save_image_bytes(data, 'mosaic', prefix=sid)

    from app.websocket.handler import push_image_ready_group

    push_image_ready_group('body', {'1': body1, '2': body2, '3': body3}, group=1)
    push_image_ready_group(
        'transparent', {'1': x200, '2': x160, '3': mosaic}, group=1
    )

    result = {
        'sourceUrl': url,
        'body': {'1': body1, '2': body2, '3': body3},
        'transparent': {'1': x200, '2': x160, '3': mosaic},
    }
    print(
        f'[PLACEHOLDER] 已推送六面板位 body={body1} xray200={x200}',
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
