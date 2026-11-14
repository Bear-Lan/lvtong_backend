"""共享面板轻量预览；原图引用不变，框选坐标映射到同帧原图。"""
import math
import re
from pathlib import Path

from app.services.image_preview import jpeg_preview_bytes

ASSET_NAME = re.compile(r'[0-9a-f]{64}\.(?:png|jpg)\Z')


def run_image_task(fn):
    # 正式gevent服务让出主循环；原生线程服务器已在线程里，无需跨hub共用线程池。
    from gevent import get_hub, monkey
    if monkey.is_module_patched('threading'):
        return get_hub().threadpool.apply(fn)
    return fn()


def asset_path(root, name):
    if not ASSET_NAME.fullmatch(name):
        raise ValueError('无效的面板原图引用')
    directory = (Path(root) / 'session_assets').resolve()
    target = (directory / name).resolve()
    if target.parent != directory:
        raise ValueError('无效的面板原图路径')
    return target


def preview_path(root, name):
    return asset_path(root, name).with_suffix('.preview.jpg')


def ensure_preview(root, name, preview=None):
    source = asset_path(root, name)
    target = preview_path(root, name)
    if not source.is_file():
        raise ValueError('面板原图不存在')
    if not target.is_file():
        from uuid import uuid4
        data = preview if preview else jpeg_preview_bytes(source.read_bytes())
        temp = target.with_name(uuid4().hex + '.tmp')
        try:
            temp.write_bytes(data)
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
    return target


def crop_original(root, reference, box, display_width, display_height):
    import cv2
    import numpy as np
    prefix = '/api/images/session_assets/'
    if not isinstance(reference, str) or not reference.startswith(prefix):
        raise ValueError('只支持共享面板原图')
    source = asset_path(root, reference[len(prefix):])
    image = cv2.imdecode(np.frombuffer(source.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError('原图解码失败')
    oh, ow = image.shape[:2]
    pw, ph = float(display_width), float(display_height)
    coords = [float(v) for v in box]
    if not all(math.isfinite(v) for v in [pw, ph, *coords]) or pw <= 0 or ph <= 0:
        raise ValueError('框选坐标或预览尺寸无效')
    x1, y1, x2, y2 = coords
    ax1 = max(0, min(ow, math.floor(min(x1, x2) * ow / pw)))
    ay1 = max(0, min(oh, math.floor(min(y1, y2) * oh / ph)))
    ax2 = max(0, min(ow, math.ceil(max(x1, x2) * ow / pw)))
    ay2 = max(0, min(oh, math.ceil(max(y1, y2) * oh / ph)))
    if ax2 <= ax1 or ay2 <= ay1:
        raise ValueError('框选区域为空')
    ok, encoded = cv2.imencode('.png', image[ay1:ay2, ax1:ax2])
    if not ok:
        raise ValueError('原图裁块编码失败')
    return encoded.tobytes()
