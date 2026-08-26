"""检测过程中的内存临时图缓存（不落 IMAGE_STORAGE_ROOT）。

用途：
  - 预约雷达图、车身/透视占位图：拉原图后生成缩略预览，前端显示预览 URL
  - 原图与预览成对缓存；提交 persist 写原图；框图裁切也从原图取
  - 用户点提交时由 persist_to_storage 从缓存读出，再写入统一路径

大图不宜经 WebSocket 推 base64，故用内存 + HTTP 取图。
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
    # 若本条是缩略预览，指向成对的原图 id；裁切结果无此字段
    original_id: str | None = None
    preview_w: int = 0
    preview_h: int = 0
    original_w: int = 0
    original_h: int = 0


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


def _image_size(data: bytes) -> tuple[int, int]:
    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return 0, 0
        h, w = img.shape[:2]
        return int(w), int(h)
    except Exception:
        return 0, 0


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


def get_for_persist(temp_id: str) -> TempImage | None:
    """提交落库：优先成对原图，否则本条字节。"""
    entry = get(temp_id)
    if entry is None:
        return None
    if entry.original_id:
        orig = get(entry.original_id)
        if orig is not None:
            return orig
    return entry


def api_url(temp_id: str) -> str:
    return f'/api/temp-images/{temp_id}'


def put_and_url(data: bytes, mime: str | None = None) -> str:
    """写入并返回前端可直接用于 <img src> 的相对 URL。"""
    return api_url(put(data, mime))


def put_preview_with_original(
    original: bytes,
    preview: bytes,
    *,
    original_mime: str | None = None,
) -> str:
    """成对写入原图 + 缩略预览，返回**预览** URL（左侧面板用）。"""
    if not original or not preview:
        raise ValueError('empty original/preview bytes')
    _purge_expired()

    ow, oh = _image_size(original)
    pw, ph = _image_size(preview)
    orig_mime = (original_mime or _guess_mime(original)).strip() or 'image/jpeg'

    orig_id = uuid.uuid4().hex
    prev_id = uuid.uuid4().hex
    now = time.time()
    with _LOCK:
        _CACHE[orig_id] = TempImage(
            data=original,
            mime=orig_mime,
            created_at=now,
            original_w=ow,
            original_h=oh,
        )
        _CACHE[prev_id] = TempImage(
            data=preview,
            mime='image/jpeg',
            created_at=now,
            original_id=orig_id,
            preview_w=pw,
            preview_h=ph,
            original_w=ow,
            original_h=oh,
        )
    return api_url(prev_id)


def parse_temp_id(reference: str) -> str:
    """从 /api/temp-images/<id> 或裸 id 抽出 temp id。"""
    s = (reference or '').strip()
    if not s:
        return ''
    if s.startswith('/api/temp-images/'):
        s = s[len('/api/temp-images/'):]
    s = s.split('?', 1)[0].split('#', 1)[0].strip('/')
    return s


def crop_from_preview_box(
    preview_ref: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    jpeg_quality: int = 92,
) -> str:
    """按预览图坐标系裁切，映射到原图后裁切，返回新 temp URL（原图分辨率裁块）。

    无原图时退化为从预览本身裁切。
    """
    tid = parse_temp_id(preview_ref)
    entry = get(tid)
    if entry is None:
        raise ValueError('临时预览图不存在或已过期')

    src_entry = entry
    if entry.original_id:
        orig = get(entry.original_id)
        if orig is not None:
            src_entry = orig

    import cv2
    import numpy as np

    arr = np.frombuffer(src_entry.data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError('原图解码失败')

    oh, ow = img.shape[:2]
    pw = entry.preview_w or entry.original_w or ow
    ph = entry.preview_h or entry.original_h or oh
    if entry.original_id and (entry.preview_w <= 0 or entry.preview_h <= 0):
        # 尺寸未缓存时从预览字节补测
        pw2, ph2 = _image_size(entry.data)
        if pw2 > 0 and ph2 > 0:
            pw, ph = pw2, ph2

    sx = ow / float(pw) if pw > 0 else 1.0
    sy = oh / float(ph) if ph > 0 else 1.0

    ax1 = int(round(min(x1, x2) * sx))
    ay1 = int(round(min(y1, y2) * sy))
    ax2 = int(round(max(x1, x2) * sx))
    ay2 = int(round(max(y1, y2) * sy))
    ax1 = max(0, min(ow - 1, ax1))
    ay1 = max(0, min(oh - 1, ay1))
    ax2 = max(ax1 + 1, min(ow, ax2))
    ay2 = max(ay1 + 1, min(oh, ay2))

    crop = img[ay1:ay2, ax1:ax2]
    if crop.size == 0:
        raise ValueError('无效裁切区域')

    q = max(1, min(95, int(jpeg_quality)))
    ok, enc = cv2.imencode('.jpg', crop, [int(cv2.IMWRITE_JPEG_QUALITY), q])
    if not ok:
        raise ValueError('裁切 JPEG 编码失败')
    return put_and_url(enc.tobytes(), 'image/jpeg')


def _purge_expired() -> None:
    now = time.time()
    with _LOCK:
        dead = [k for k, v in _CACHE.items() if now - v.created_at > _TTL_SEC]
        for k in dead:
            _CACHE.pop(k, None)
