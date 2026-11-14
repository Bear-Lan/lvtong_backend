"""图像采集服务

车头/车尾/车顶：分别从 cam_head / cam_lane / cam_road 的 RTSP 抽帧
  - 车道枪机抓拍：ISAPI /picture 走 4K 抓拍通道常全黑；码流预览正常
  - 因此统一用视频流截帧，不取 4K 抓拍静图
  - 自动拍与手动拍一致：WS 推 /api/temp-images/<id>（避免巨型 base64 堵链路），
    提交检测单时再 persist 落盘
其它类别：仍可用设备 config 里的 *_capture_url（同样推 temp URL，不预落盘）

X 光影像走另一路：http://.../xray/image?id=...（不在本服务）
"""
from __future__ import annotations

import base64
import logging
import os
import threading
from urllib.parse import quote

import cv2
import requests

from app.services.capture_guard import ensure_capture_allowed
from app.services.camera_stream import rtsp_channel

logger = logging.getLogger('capture_service')

# 与 Pad 的 hikvision.ts 对应；每类允许独立覆盖，不再共用一个车身相机。
# RTSP 通道：默认 101（通道1主码流）；抓拍机上 1/101、2/102 均可
HIK_RTSP_CHANNEL = os.environ.get('HIK_RTSP_CHANNEL')
JPEG_QUALITY = int(os.environ.get('CAPTURE_JPEG_QUALITY', '95'))
# 同机串行抽帧，避免多线程同时 VideoCapture 把 GIL/网络打满导致距离 WS 卡顿
_capture_lock = threading.Lock()
_RTSP_WARMUP_FRAMES = int(os.environ.get('CAPTURE_RTSP_WARMUP_FRAMES', '2'))

CATEGORY_DEVICE_MAP = {
    'head': os.environ.get('HEAD_CAPTURE_DEVICE_ID', 'cam_head'),
    'tail': os.environ.get('TAIL_CAPTURE_DEVICE_ID', 'cam_lane'),
    'top': os.environ.get('TOP_CAPTURE_DEVICE_ID', 'cam_road'),
}


def _load_camera_row(device_id: str) -> dict | None:
    try:
        from app.db.device import DBDevice
        return DBDevice().getDevice(device_id)
    except Exception as e:
        logger.warning('[CAPTURE] 读设备失败 %s: %s', device_id, e)
        return None


def _cfg_dict(row: dict) -> dict:
    cfg = row.get('config') or {}
    if isinstance(cfg, str):
        import json
        try:
            cfg = json.loads(cfg)
        except Exception:
            cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def _resolve_rtsp_channel(row: dict) -> int:
    """拍照始终读取主码流，旧配置仅用于选择视频通道。"""
    return rtsp_channel(row, 1, os.environ.get('HIK_RTSP_CHANNEL') or HIK_RTSP_CHANNEL)


def _rtsp_url(row: dict, channel: int) -> str:
    ip = (row.get('ip_address') or '').strip()
    if not ip:
        raise RuntimeError('设备未配置 IP')
    user = quote((row.get('username') or 'admin').strip(), safe='')
    pwd = quote(row.get('password') or '', safe='')
    return f'rtsp://{user}:{pwd}@{ip}:554/Streaming/Channels/{channel}'


def snapshot_from_camera(device_id: str, *, right_two_thirds: bool = False) -> bytes:
    """对指定海康设备做一次 RTSP 抽帧，返回 JPEG bytes。"""
    row = _load_camera_row(device_id)
    if not row:
        raise RuntimeError(f'设备不存在: {device_id}')

    channel = _resolve_rtsp_channel(row)
    url = _rtsp_url(row, channel)
    # 日志里不要带账号密码
    safe_url = f'rtsp://***@{ (row.get("ip_address") or "").strip() }:554/Streaming/Channels/{channel}'

    os.environ.setdefault('OPENCV_FFMPEG_CAPTURE_OPTIONS', 'rtsp_transport;tcp')

    with _capture_lock:
        ensure_capture_allowed()
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            raise RuntimeError(f'摄像头 RTSP 打开失败 ({device_id} {safe_url})')

        frame = None
        try:
            # 少读几帧：降低 OpenCV 占 GIL 时间，避免距离推送在前端“卡住”
            warmup = max(1, min(5, _RTSP_WARMUP_FRAMES))
            for _ in range(warmup):
                ok, buf = cap.read()
                if ok and buf is not None and getattr(buf, 'size', 0) > 0:
                    frame = buf
        finally:
            cap.release()

        if frame is None:
            raise RuntimeError(f'摄像头 RTSP 读帧失败 ({device_id} {safe_url})')

        if right_two_thirds:
            # 车顶相机横向画面过长；预览推送和最终存图共用这次实际像素裁剪。
            frame = frame[:, frame.shape[1] // 3:]

        ok, encoded = cv2.imencode(
            '.jpg',
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), max(1, min(100, JPEG_QUALITY))],
        )
        if not ok:
            raise RuntimeError(f'JPEG 编码失败 ({device_id})')
        data = encoded.tobytes()
        if not data.startswith(b'\xff\xd8'):
            raise RuntimeError(f'摄像头未返回 JPEG ({device_id} channel={channel})')
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
    """按类别采集一帧，推 temp URL 到前端；不落盘（提交时 persist）。"""
    generation = ensure_capture_allowed()
    category = (category or '').strip() or 'head'

    if category in CATEGORY_DEVICE_MAP:
        device_id = CATEGORY_DEVICE_MAP[category]
        # 允许设备 config 覆盖：head_device_id 等
        row = _load_camera_row(device_id)
        if row:
            override = _cfg_dict(row).get(f'{category}_device_id')
            if override:
                device_id = str(override)
        print(
            f'[CAPTURE] {category} ← RTSP 抽帧 device_id={device_id}（仅预览，提交再落盘）',
            flush=True,
        )
        data = (snapshot_from_camera(device_id, right_two_thirds=True)
                if category == 'top' else snapshot_from_camera(device_id))
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
        print(f'[CAPTURE] {category} ← HTTP {url}（仅预览，提交再落盘）', flush=True)
        data = fetch_url_bytes(url)

    # RTSP/HTTP期间可能收到占用报文；即使之后已恢复，也不接受跨占用周期的迟到图。
    ensure_capture_allowed(generation)
    # 非 JPEG 时仍按 jpeg mime 推（前端 <img> 多数可显示；提交时按 dataUrl mime 写盘）
    mime = 'image/jpeg' if data.startswith(b'\xff\xd8') else 'image/jpeg'
    if data.startswith(b'\x89PNG'):
        mime = 'image/png'

    # WS 只推短 URL：大图 base64 会堵 Socket.IO/主线程，导致小车卡顿
    from app.services import temp_image_cache
    preview_url = temp_image_cache.put_and_url(data, mime)
    _push_image_ready(category, preview_url)

    data_url = f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')
    logger.info(
        '[CAPTURE] %s → temp %s (%s bytes, 未落盘)',
        category, preview_url, len(data),
    )
    print(
        f'[CAPTURE] {category} 成功 → {preview_url} ({len(data)} bytes, 未落盘)',
        flush=True,
    )
    # dataUrl 仍返回给手动 HTTP 调用方；WS 已改用 temp URL
    return {'url': preview_url, 'dataUrl': data_url, 'size': len(data)}


def _push_image_ready(image_type: str, url: str) -> None:
    try:
        from app.websocket.handler import push_image_ready
        push_image_ready(image_type, url)
    except Exception:
        pass
