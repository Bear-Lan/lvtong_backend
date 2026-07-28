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
