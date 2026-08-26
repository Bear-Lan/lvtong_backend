"""预览图缩小：超宽 X 光/雷达原图可达万级像素，前端逐像素处理会卡死主线程。

原图仍需保留供裁切与落库；本模块只负责缩略 + 按 source_url 复用。
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger('image_preview')

# 预览最长边；提交仍用成对缓存的原图
DEFAULT_MAX_EDGE = 1600
DEFAULT_JPEG_QUALITY = 75

# 同源 URL：(original, preview) 复用，避免预约雷达与面板重复解大图
_pair_by_source: dict[str, tuple[bytes, bytes]] = {}
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


def remember_source_pair(source_url: str, original: bytes, preview: bytes) -> None:
    key = (source_url or '').strip()
    if not key or not original or not preview:
        return
    with _preview_lock:
        _pair_by_source[key] = (original, preview)


def get_remembered_pair(source_url: str) -> tuple[bytes, bytes] | None:
    key = (source_url or '').strip()
    if not key:
        return None
    with _preview_lock:
        return _pair_by_source.get(key)


def remember_source_preview(source_url: str, preview: bytes) -> None:
    """兼容旧调用：仅记预览时无法成对复用原图，不写入 pair。"""
    _ = (source_url, preview)


def get_remembered_preview(source_url: str) -> bytes | None:
    pair = get_remembered_pair(source_url)
    return pair[1] if pair else None


def preview_from_raw(
    data: bytes,
    source_url: str = '',
    max_edge: int = DEFAULT_MAX_EDGE,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> bytes:
    """缩小并可选按 source_url 记住 (原图, 预览)。"""
    preview = jpeg_preview_bytes(data, max_edge=max_edge, quality=quality)
    if source_url and data and preview:
        remember_source_pair(source_url, data, preview)
    return preview


def put_temp_preview_url(original: bytes, source_url: str = '') -> str:
    """原图 → 预览 → 成对进 temp 缓存，返回预览 URL。"""
    from app.services import temp_image_cache

    pair = get_remembered_pair(source_url) if source_url else None
    if pair:
        original, preview = pair
    else:
        preview = jpeg_preview_bytes(original)
        if source_url:
            remember_source_pair(source_url, original, preview)
    return temp_image_cache.put_preview_with_original(original, preview)
