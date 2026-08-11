"""检测过程中的内存临时图缓存（预览用，不落 IMAGE_STORAGE_ROOT）。

用途：
  - 预约雷达图、车身/透视占位图：拉取后先放内存，前端用 /api/temp-images/<id> 显示
  - 用户点提交时由 persist_to_storage 从缓存读出，再写入统一路径

与 capture_service 的 dataUrl 预览同一原则：提交前不写正式库。
大图（十余 MB）不宜经 WebSocket 推 base64，故用内存 + HTTP 取图。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass


@dataclass
class TempImage:
    data: bytes
    mime: str
    created_at: float


# id -> TempImage
_CACHE: dict[str, TempImage] = {}
_LOCK = threading.Lock()
# 默认保留 2 小时，覆盖一轮检测 + 停顿
_TTL_SEC = 2 * 60 * 60


def _guess_mime(data: bytes) -> str:
    if data.startswith(b'\xff\xd8'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG'):
        return 'image/png'
    if data.startswith(b'GIF8'):
        return 'image/gif'
    if data.startswith(b'BM'):
        return 'image/bmp'
    if len(data) >= 12 and data[0:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'


def put(data: bytes, mime: str | None = None) -> str:
    """写入缓存，返回 temp id。"""
    if not data:
        raise ValueError('empty image bytes')
    _purge_expired()
    tid = uuid.uuid4().hex
    entry = TempImage(
        data=data,
        mime=(mime or _guess_mime(data)).strip() or 'image/jpeg',
        created_at=time.time(),
    )
    with _LOCK:
        _CACHE[tid] = entry
    return tid


def get(temp_id: str) -> TempImage | None:
    if not temp_id:
        return None
    with _LOCK:
        entry = _CACHE.get(temp_id)
        if entry is None:
            return None
        if time.time() - entry.created_at > _TTL_SEC:
            _CACHE.pop(temp_id, None)
            return None
        return entry


def api_url(temp_id: str) -> str:
    return f'/api/temp-images/{temp_id}'


def put_and_url(data: bytes, mime: str | None = None) -> str:
    """写入并返回前端可直接用于 <img src> 的相对 URL。"""
    return api_url(put(data, mime))


def _purge_expired() -> None:
    now = time.time()
    with _LOCK:
        dead = [k for k, v in _CACHE.items() if now - v.created_at > _TTL_SEC]
        for k in dead:
            _CACHE.pop(k, None)
