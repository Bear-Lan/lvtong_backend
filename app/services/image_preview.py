"""预览图缩小：超宽 X 光/雷达原图可达万级像素，前端逐像素处理会卡死主线程。"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger('image_preview')

# 预览最长边；提交仍用缓存内这份（占位图本身非正式硬件成片）
DEFAULT_MAX_EDGE = 1600
DEFAULT_JPEG_QUALITY = 75

# 同源 URL 预览复用：预约雷达图与受理后六面板共用同一远程图，避免抓拍窗口再解 13MB
_preview_by_source: dict[str, bytes] = {}
_preview_lock = threading.Lock()


def jpeg_preview_bytes(
    data: bytes,
    max_edge: int = DEFAULT_MAX_EDGE,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> bytes:
    """解码后按最长边缩小并重编码为 JPEG；失败则返回原字节。"""
    if not data:
        return data
    try:
        import cv2
        import numpy as np
    except ImportError:
        return data

    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return data

    h, w = img.shape[:2]
    edge = max(h, w)
    if edge > max_edge > 0:
        scale = max_edge / float(edge)
        nw = max(1, int(round(w * scale)))
        nh = max(1, int(round(h * scale)))
        img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)

    q = max(1, min(95, int(quality)))
    ok, enc = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), q])
    if not ok:
        return data
    out = enc.tobytes()
    if len(out) < len(data):
        logger.info(
            '[PREVIEW] %sx%s %sB → %sx%s %sB',
            w, h, len(data), img.shape[1], img.shape[0], len(out),
        )
    return out


def remember_source_preview(source_url: str, preview: bytes) -> None:
    """记住某远程源对应的预览 JPEG，供后续占位推送复用。"""
    key = (source_url or '').strip()
    if not key or not preview:
        return
    with _preview_lock:
        _preview_by_source[key] = preview


def get_remembered_preview(source_url: str) -> bytes | None:
    key = (source_url or '').strip()
    if not key:
        return None
    with _preview_lock:
        return _preview_by_source.get(key)


def preview_from_raw(
    data: bytes,
    source_url: str = '',
    max_edge: int = DEFAULT_MAX_EDGE,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> bytes:
    """缩小并可选按 source_url 记住，避免同图重复 imdecode。"""
    preview = jpeg_preview_bytes(data, max_edge=max_edge, quality=quality)
    if source_url:
        remember_source_preview(source_url, preview)
    return preview
