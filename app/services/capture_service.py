"""图像采集服务

车头/车尾/车顶：从海康摄像头 ISAPI 抓图（默认 camera2，与前端约定一致）
其它类别：仍可用设备 config 里的 *_capture_url

X 光影像走另一路：http://.../xray/image?id=...（不在本服务）
"""
from __future__ import annotations

import logging
import os

import requests
from requests.auth import HTTPDigestAuth

from app.services.image_store import ImageStore

logger = logging.getLogger('capture_service')

# 与 vue_frontend/src/config/hikvision.ts 一致：车身三类共用枪机 camera2
BODY_CAPTURE_DEVICE_ID = os.environ.get('BODY_CAPTURE_DEVICE_ID', 'camera2')
# 海康抓图通道：本现场 camera2 实测 channels/1 返回 JPEG（101 返回空 XML）
HIK_SNAPSHOT_CHANNEL = os.environ.get('HIK_SNAPSHOT_CHANNEL', '1')

CATEGORY_DEVICE_MAP = {
    'head': BODY_CAPTURE_DEVICE_ID,
    'tail': BODY_CAPTURE_DEVICE_ID,
    'top': BODY_CAPTURE_DEVICE_ID,
}


def _load_camera_row(device_id: str) -> dict | None:
    try:
        from app.db.device import DBDevice
        return DBDevice().getDevice(device_id)
    except Exception as e:
        logger.warning('[CAPTURE] 读设备失败 %s: %s', device_id, e)
        return None


def _hik_snapshot_url(ip: str, port: int | None, channel: str) -> str:
    port = int(port or 80)
    # 80 可省略端口；其它端口写上
    host = ip if port in (80, 0) else f'{ip}:{port}'
    return f'http://{host}/ISAPI/Streaming/channels/{channel}/picture'


def snapshot_from_camera(device_id: str) -> bytes:
    """对指定海康设备做一次 ISAPI 抓图，返回 JPEG bytes"""
    row = _load_camera_row(device_id)
    if not row:
        raise RuntimeError(f'设备不存在: {device_id}')

    ip = (row.get('ip_address') or '').strip()
    if not ip:
        raise RuntimeError(f'设备 {device_id} 未配置 IP')

    username = (row.get('username') or 'admin').strip()
    password = row.get('password') or ''
    cfg = row.get('config') if isinstance(row.get('config'), dict) else {}
    channel = str(
        cfg.get('snapshot_channel')
        or os.environ.get('HIK_SNAPSHOT_CHANNEL')
        or HIK_SNAPSHOT_CHANNEL
        or '1'
    )

    url = _hik_snapshot_url(ip, row.get('port'), channel)
    try:
        resp = requests.get(
            url,
            auth=HTTPDigestAuth(username, password),
            timeout=10,
        )
    except requests.RequestException as e:
        raise RuntimeError(f'摄像头抓图连接失败 ({device_id} {url}): {e}') from e

    if resp.status_code != 200:
        raise RuntimeError(
            f'摄像头抓图失败 ({device_id}): HTTP {resp.status_code} {url}'
        )

    data = resp.content or b''
    ctype = (resp.headers.get('Content-Type') or '').lower()
    if not data.startswith(b'\xff\xd8') and 'jpeg' not in ctype and 'image' not in ctype:
        preview = data[:200].decode('utf-8', errors='replace')
        raise RuntimeError(
            f'摄像头未返回 JPEG ({device_id} channel={channel}): {ctype} {preview}'
        )

    return data


def fetch_url_bytes(url: str) -> bytes:
    """普通 HTTP 拉图（无认证），例如后续扩展用"""
    try:
        resp = requests.get(url, timeout=15)
    except requests.RequestException as e:
        raise RuntimeError(f'硬件连接失败: {e}') from e
    if resp.status_code != 200:
        raise RuntimeError(f'硬件返回状态码 {resp.status_code}')
    return resp.content


def capture_category(category: str) -> dict:
    """按类别完成一次采集并推送前端"""
    category = (category or '').strip() or 'head'
    store = ImageStore()

    if category in CATEGORY_DEVICE_MAP:
        device_id = CATEGORY_DEVICE_MAP[category]
        # 允许设备 config 覆盖：head_device_id 等
        row = _load_camera_row(device_id)
        if row and isinstance(row.get('config'), dict):
            override = row['config'].get(f'{category}_device_id') or row['config'].get(
                'body_capture_device_id'
            )
            if override:
                device_id = str(override)
        print(
            f'[CAPTURE] {category} ← 海康抓图 device_id={device_id}',
            flush=True,
        )
        data = snapshot_from_camera(device_id)
    else:
        # goods / license / evidence：若 config 有 URL 则拉，否则报错
        url = ''
        try:
            from app.db.device import DBDevice
            for d in DBDevice().getDevicesByType('camera'):
                cfg = d.get('config') if isinstance(d.get('config'), dict) else {}
                url = cfg.get(f'{category}_capture_url', '') or url
        except Exception:
            pass
        if not url:
            raise ValueError(f'采集类别 "{category}" 未配置摄像头或 URL')
        print(f'[CAPTURE] {category} ← HTTP {url}', flush=True)
        data = fetch_url_bytes(url)

    api_path = store.save_image_bytes(data, category)
    _push_image_ready(category, api_path)
    logger.info('[CAPTURE] %s → %s (%s bytes)', category, api_path, len(data))
    print(f'[CAPTURE] {category} 成功 → {api_path} ({len(data)} bytes)', flush=True)
    return {'apiPath': api_path, 'size': len(data)}


def _push_image_ready(image_type: str, url: str) -> None:
    try:
        from app.websocket.handler import push_image_ready
        push_image_ready(image_type, url)
    except Exception:
        pass
