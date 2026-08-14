"""文件代理读取端点（对齐 Qt 直接从 D:/soft/FtpLvTong 读图片）

前端 <img :src="..."> 通过 GET /api/image?path=<绝对路径> 加载本地文件。
只允许访问白名单目录下的文件（防路径穿越）。
"""
import logging
import os
import time
from pathlib import Path

import cv2
import numpy as np
from flask import Blueprint, jsonify, request, send_file

logger = logging.getLogger('file_api')

file_api = Blueprint('file_api', __name__, url_prefix='/api')

# 白名单：只允许访问这些目录下的文件
ALLOWED_ROOTS = [
    os.environ.get('MOBILE_UPLOAD_DIR', './uploads/mobile'),
    os.environ.get('MOCK_PHOTO_DIR', 'D:/soft/FtpLvTong'),
]


def _is_path_allowed(abs_path: Path) -> bool:
    """检查路径是否在白名单根目录下"""
    try:
        abs_resolved = abs_path.resolve()
        for root in ALLOWED_ROOTS:
            try:
                root_resolved = Path(root).resolve()
                abs_resolved.relative_to(root_resolved)
                return True
            except (ValueError, OSError):
                continue
    except (ValueError, OSError):
        pass
    return False


@file_api.route('/image')
def get_image():
    """GET /api/image?path=<绝对路径> — 读取本地图片

    给前端 <img :src="..."> 用，把移动 app 上传到 D:/soft/FtpLvTong 的图片代理给前端。
    """
    raw_path = request.args.get('path', '')
    if not raw_path:
        return jsonify({'code': 400, 'message': '缺少 path 参数', 'data': None}), 400

    abs_path = Path(raw_path)
    if not _is_path_allowed(abs_path):
        logger.warning(f'[IMAGE] 拒绝访问: {raw_path}')
        return jsonify({'code': 403, 'message': '访问被拒绝', 'data': None}), 403

    if not abs_path.is_file():
        return jsonify({'code': 404, 'message': '文件不存在', 'data': None}), 404

    return send_file(str(abs_path), conditional=True)


@file_api.route('/image/list')
def list_images():
    """GET /api/image/list?dir=<目录> — 列出某目录下所有图片（调试用）"""
    raw_dir = request.args.get('dir', '')
    if not raw_dir:
        return jsonify({'code': 400, 'message': '缺少 dir 参数', 'data': None}), 400

    dir_path = Path(raw_dir)
    if not _is_path_allowed(dir_path):
        return jsonify({'code': 403, 'message': '访问被拒绝', 'data': None}), 403

    if not dir_path.is_dir():
        return jsonify({'code': 404, 'message': '目录不存在', 'data': None}), 404

    files = sorted(
        [str(f) for f in dir_path.iterdir() if f.is_file()],
        key=lambda p: p,
    )
    return jsonify({
        'code': 0,
        'message': 'success',
        'data': {'count': len(files), 'files': files},
    })


# ============ 行驶证图合并（对齐 Qt mergeLicense1AndLicense2Img） ============

@file_api.route('/license/merge', methods=['POST'])
def merge_license_images():
    """POST /api/license/merge — 合并行驶证图 1+2 为一张

    对齐 LvTongPro::mergeLicense1AndLicense2Img()（LvTongPro.cpp:6570）：
      1. 两张图都是横图（如果 w<h 旋转 -90°）
      2. 统一尺寸 = max(w1, w2), max(h1, h2)
      3. 缩放保持比例
      4. 裁剪中间 2/3（去掉上下各 1/6 边距）
      5. 上下拼接到 result 图，保存
      6. 只有一张就保存该张

    Body: { license1: <abs_path>, license2: <abs_path>, output: <abs_path> }
    """
    body = request.get_json(silent=True) or {}
    lic1 = body.get('license1', '')
    lic2 = body.get('license2', '')
    output = body.get('output', '')

    if not output:
        return jsonify({'code': 400, 'message': '缺少 output 路径', 'data': None}), 400

    output_path = Path(output)
    if not _is_path_allowed(output_path):
        return jsonify({'code': 403, 'message': 'output 路径不被允许', 'data': None}), 403

    img1 = cv2.imread(lic1) if lic1 else None
    img2 = cv2.imread(lic2) if lic2 else None

    try:
        if img1 is not None and img2 is not None:
            # 如果竖图（w<h），旋转 -90°（Qt 行为）
            if img1.shape[1] < img1.shape[0]:
                img1 = cv2.rotate(img1, cv2.ROTATE_90_COUNTERCLOCKWISE)
            if img2.shape[1] < img2.shape[0]:
                img2 = cv2.rotate(img2, cv2.ROTATE_90_COUNTERCLOCKWISE)

            h1, w1 = img1.shape[:2]
            h2, w2 = img2.shape[:2]

            width = max(w1, w2)
            height = max(h1, h2)

            # 缩放保持比例（Qt KeepAspectRatio + SmoothTransformation）
            def resize_keep_aspect(img, w, h):
                ih, iw = img.shape[:2]
                scale = min(w / iw, h / ih)
                return cv2.resize(img, (int(iw * scale), int(ih * scale)), interpolation=cv2.INTER_CUBIC)

            img1 = resize_keep_aspect(img1, width, height)
            img2 = resize_keep_aspect(img2, width, height)

            # 裁剪中间 2/3（去掉上下各 1/6 边距）
            y1 = height // 6
            y2 = y1 + 2 * height // 3
            img1 = img1[y1:y2, :]
            img2 = img2[y1:y2, :]

            # 上下拼接
            result = np.vstack([img1, img2])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(output_path), result)
        elif img1 is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(output_path), img1)
        elif img2 is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(output_path), img2)
        else:
            return jsonify({'code': 400, 'message': 'license1 和 license2 都不存在', 'data': None}), 400

        return jsonify({
            'code': 0,
            'message': '合并成功',
            'data': {
                'mergedPath': str(output_path).replace('\\', '/'),
                'elapsedMs': int(time.time() * 1000),
            },
        })
    except Exception as e:
        logger.error(f'合并行驶证图失败: {e}')
        return jsonify({'code': 500, 'message': f'合并失败: {e}', 'data': None}), 500


# ============ AI 裁剪 + 拼接（行驶证） ============

from config import AI_LICENSE_CROP_URL


def _extract_result_b64(payload: dict, key: str) -> bytes | None:
    """从 AI 返回中取 crop_result_imageN.base64 → 原始图片 bytes。"""
    node = payload.get(key)
    if node is None:
        return None
    if isinstance(node, dict):
        b64 = node.get('base64') or node.get('data') or node.get('image') or ''
    else:
        b64 = str(node)
    b64 = str(b64).strip()
    if not b64:
        return None
    if b64.startswith('data:') and ',' in b64:
        b64 = b64.split(',', 1)[1]
    try:
        import base64
        return base64.b64decode(b64)
    except Exception:
        return None


def _bytes_to_bgr(raw: bytes):
    """解码为 BGR；若带 alpha，叠到白底上（避免透明变黑、看起来像遮盖）。"""
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.shape[2] == 4:
        bgr = img[:, :, :3].astype(np.float32)
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        white = np.full_like(bgr, 255.0)
        return (bgr * alpha + white * (1.0 - alpha)).astype(np.uint8)
    return img


def _parse_box(box_info) -> tuple[int, int, int, int] | None:
    """解析 crop_boxN → (x1, y1, x2, y2)。"""
    if not isinstance(box_info, dict) or not box_info.get('found'):
        return None
    box = box_info.get('box')
    if box is None:
        return None
    if isinstance(box, dict):
        if all(k in box for k in ('x1', 'y1', 'x2', 'y2')):
            return int(box['x1']), int(box['y1']), int(box['x2']), int(box['y2'])
        if all(k in box for k in ('x', 'y', 'w', 'h')):
            x, y, w, h = int(box['x']), int(box['y']), int(box['w']), int(box['h'])
            return x, y, x + w, y + h
        return None
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        x1, y1, x2, y2 = [int(v) for v in box[:4]]
        # 兼容 [x,y,w,h]
        if x2 < x1 or y2 < y1:
            return x1, y1, x1 + x2, y1 + y2
        return x1, y1, x2, y2
    return None


def _crop_from_original(orig_bytes: bytes, box_info, pad_ratio: float = 0.12):
    """按检测框从原图裁切，四周留白，保证证件完整、不互相遮盖。"""
    coords = _parse_box(box_info)
    if not coords:
        return None
    img = _bytes_to_bgr(orig_bytes)
    if img is None:
        return None
    h, w = img.shape[:2]
    x1, y1, x2, y2 = coords
    bw = max(1, x2 - x1)
    bh = max(1, y2 - y1)
    pad_x = int(round(bw * pad_ratio))
    pad_y = int(round(bh * pad_ratio))
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(w, x2 + pad_x)
    y2 = min(h, y2 + pad_y)
    if x2 <= x1 or y2 <= y1:
        return None
    return img[y1:y2, x1:x2].copy()


def _trim_near_empty(img, thresh: int = 10):
    """去掉近黑/近空边，避免透明底解码后的大黑框。"""
    if img is None or img.size == 0:
        return img
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = gray > thresh
    if not np.any(mask):
        return img
    ys, xs = np.where(mask)
    return img[int(ys.min()): int(ys.max()) + 1, int(xs.min()): int(xs.max()) + 1]


def _pad_to_width(img, width: int, fill: int = 255):
    """宽度对齐：只缩小不放大；不够宽则右侧白边补齐（绝不叠图）。"""
    h, w = img.shape[:2]
    if w == width:
        return img
    if w > width:
        nh = max(1, int(round(h * (width / float(w)))))
        return cv2.resize(img, (width, nh), interpolation=cv2.INTER_AREA)
    canvas = np.full((h, width, 3), fill, dtype=np.uint8)
    canvas[:, 0:w] = img
    return canvas


def _stitch_vertical(img1, img2):
    """图1在上、图2在下：底边贴顶边硬拼。同宽直接拼；不同宽才右侧补白对齐。"""
    a = img1
    b = img2
    h1, w1 = a.shape[:2]
    h2, w2 = b.shape[:2]
    if w1 == w2:
        return np.vstack([a, b])
    width = max(w1, w2)
    return np.vstack([_pad_to_width(a, width), _pad_to_width(b, width)])


def _img_to_jpeg_data_url(img) -> str:
    import base64
    ok, buf = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:
        raise RuntimeError('编码 JPEG 失败')
    b64 = base64.b64encode(buf.tobytes()).decode('ascii')
    return f'data:image/jpeg;base64,{b64}'


def _pick_crop_image(payload: dict, orig_bytes: bytes, index: int):
    """优先用 AI 返回的 crop_result（同尺寸裁剪图），没有再按 box 从原图裁。"""
    raw = _extract_result_b64(payload, f'crop_result_image{index}')
    if raw:
        img = _bytes_to_bgr(raw)
        if img is not None:
            return img, 'result'
    boxed = _crop_from_original(orig_bytes, payload.get(f'crop_box{index}'))
    if boxed is not None and boxed.size > 0:
        return boxed, 'box'
    return None, ''


@file_api.route('/license/ai-crop', methods=['POST'])
def ai_crop_license():
    """POST /api/license/ai-crop — 调用外部 AI 裁剪行驶证，双图则上下拼接成一张。

    FormData:
      crop_image1 — 主行驶证（必填）
      crop_image2 — 挂车证（可选）

    返回:
      data.imageDataUrl — 最终一张图（data:image/jpeg;base64,...）
      data.mode — single | stitch
    """
    from io import BytesIO

    import requests as http_requests

    f1 = request.files.get('crop_image1')
    f2 = request.files.get('crop_image2')
    if not f1 or not f1.filename:
        return jsonify({'code': 400, 'message': '请提供主行驶证 crop_image1', 'data': None}), 400

    # 读入内存再转发，避免 FileStorage.stream 被消费/不可 seek
    orig1 = f1.read()
    files = {
        'crop_image1': (
            f1.filename or 'crop_image1.jpg',
            BytesIO(orig1),
            f1.mimetype or 'application/octet-stream',
        ),
    }
    has_second = bool(f2 and f2.filename)
    orig2 = b''
    if has_second:
        orig2 = f2.read()
        files['crop_image2'] = (
            f2.filename or 'crop_image2.jpg',
            BytesIO(orig2),
            f2.mimetype or 'application/octet-stream',
        )

    t0 = time.time()
    try:
        resp = http_requests.post(AI_LICENSE_CROP_URL, files=files, timeout=90)
    except Exception as e:
        logger.error(f'AI 裁剪请求失败: {e}')
        return jsonify({'code': 502, 'message': f'AI 服务请求失败: {e}', 'data': None}), 502

    if resp.status_code != 200:
        return jsonify({
            'code': 502,
            'message': f'AI 服务 HTTP {resp.status_code}',
            'data': {'body': (resp.text or '')[:500]},
        }), 502

    try:
        data = resp.json()
    except ValueError:
        return jsonify({'code': 502, 'message': 'AI 服务返回非 JSON', 'data': None}), 502

    # AI 的 data 字段可能是 list，真正字段在根上
    payload = data.get('data') if isinstance(data.get('data'), dict) else data
    if not isinstance(payload, dict):
        return jsonify({'code': 502, 'message': 'AI 返回格式异常', 'data': None}), 502

    img1, src1 = _pick_crop_image(payload, orig1, 1)
    if img1 is None:
        return jsonify({
            'code': 502,
            'message': 'AI 未返回可用的主行驶证裁剪结果（crop_box1 / crop_result_image1）',
            'data': {'keys': list(payload.keys())},
        }), 502

    mode = 'single'
    final = img1
    src2 = ''

    if has_second:
        img2, src2 = _pick_crop_image(payload, orig2, 2)
        if img2 is None:
            return jsonify({
                'code': 502,
                'message': '已传挂车证，但 AI 未返回可用的挂车证裁剪结果',
                'data': None,
            }), 502
        # 严格上下拼接，互不遮盖
        final = _stitch_vertical(img1, img2)
        mode = 'stitch'

    try:
        data_url = _img_to_jpeg_data_url(final)
    except Exception as e:
        return jsonify({'code': 500, 'message': f'输出编码失败: {e}', 'data': None}), 500

    elapsed_ms = int((time.time() - t0) * 1000)
    logger.info(
        'AI license crop ok mode=%s src1=%s src2=%s shape=%s elapsed=%sms',
        mode, src1, src2, getattr(final, 'shape', None), elapsed_ms,
    )
    return jsonify({
        'code': 0,
        'message': 'AI 检测拼接成功',
        'data': {
            'imageDataUrl': data_url,
            'mode': mode,
            'elapsedMs': elapsed_ms,
            'cropSource': {'image1': src1, 'image2': src2},
        },
    })
