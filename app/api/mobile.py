"""移动 app 上传接口 — 移植自 Java MobileUploadController.java
(E:\\code_product\\maochen_LvTong\\移动app后端服务\\backend\\...\\controller\\MobileUploadController.java)

【接口列表】
POST /api/mobile/upload/image - 单张图片上传
POST /api/mobile/upload/json  - JSON文件上传
GET  /api/mobile/health       - 服务健康检查

【存储结构】
{basePath}/                      ← MOCK_PHOTO_DIR（默认 D:/soft/FtpLvTong）
  ├─ {dirName}/
  │    ├─ 车头照.jpg
  │    └─ 行驶证.png
  └─ {jsonFileName}   ← JSON 与文件夹同级

收到文件后 JsonFolderWatcher（app/services/mobile_watcher.py）会监听此目录，
读到新 JSON 后解析图片 + 解析 passcode 14 字段 + 推 WS 给前端。
"""
import logging
import os
import re
import time
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

mobile_api = Blueprint('mobile_api', __name__, url_prefix='/api/mobile')
logger = logging.getLogger('mobile_api')

# 存储根目录：与 MOCK_PHOTO_DIR 保持一致（JsonFolderWatcher 监听同一个目录）
UPLOAD_BASE_PATH = Path(os.environ.get('MOCK_PHOTO_DIR', 'D:/soft/FtpLvTong'))
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_IMAGE_EXTS = {'.jpg', '.jpeg', '.png'}
ALLOWED_JSON_EXTS = {'.json'}


# ============ 响应辅助（对齐 Java ApiResponse） ============

def _ok(data=None, message: str = 'success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def _error(code: int, message: str, http_status: int = 400):
    return jsonify({'code': code, 'message': message, 'data': None}), http_status


# ============ 工具方法（移植 Java sanitizeFileName/getExtension） ============

def _get_extension(filename: str) -> str:
    if not filename:
        return ''
    last_dot = filename.rfind('.')
    if last_dot < 0:
        return ''
    return filename[last_dot:].lower()


def _sanitize_filename(filename: str) -> str:
    """清理文件名：去除路径部分、危险字符、首尾空格和点"""
    if not filename or not filename.strip():
        return f'{uuid.uuid4().hex}.jpg'
    # 去掉路径部分
    name = Path(filename).name
    # 去除路径穿越字符
    name = re.sub(r'[/\\]', '_', name)
    # 去除危险字符（Windows 文件名禁用字符）
    name = re.sub(r'[<>:"\|\?\*]', '_', name)
    # 去掉首尾空格和点
    name = name.strip().strip('.')
    if not name:
        name = f'{uuid.uuid4().hex}.jpg'
    return name


# ============ 端点 ============

@mobile_api.route('/health')
def health():
    """GET /api/mobile/health — 服务健康检查"""
    logger.debug('健康检查请求')
    return _ok({
        'status': 'UP',
        'timestamp': int(time.time() * 1000),
        'serverTime': time.strftime('%Y-%m-%d'),
    }, '服务正常')


@mobile_api.route('/upload/image', methods=['POST'])
def upload_image():
    """POST /api/mobile/upload/image — 单张图片上传

    FormData 参数：
      dirName — 文件夹名称（如 photo_20260327）
      file    — 图片文件（使用原始文件名存储）
    """
    start_time = time.time()
    request_id = uuid.uuid4().hex[:8]

    dir_name = request.form.get('dirName', '').strip()
    file = request.files.get('file')

    logger.info(f'[{request_id}] 图片上传开始 - dirName={dir_name}, file={file.filename if file else None}')

    # 1. 校验文件
    if not file or not file.filename:
        return _error(400, '请选择要上传的图片文件')
    if not dir_name:
        return _error(400, 'dirName 不能为空')
    if not re.match(r'^[a-zA-Z0-9_\-]+$', dir_name):
        return _error(400, 'dirName 只允许字母、数字、下划线、连字符')

    # 2. 校验格式
    ext = _get_extension(file.filename)
    if ext not in ALLOWED_IMAGE_EXTS:
        return _error(400, f'不支持的图片格式，仅支持 jpg/jpeg/png（收到 {ext}）')

    # 3. 校验大小（从 stream 读取）
    file.stream.seek(0, os.SEEK_END)
    file_size = file.stream.tell()
    file.stream.seek(0)
    if file_size > MAX_IMAGE_SIZE:
        return _error(400, f'图片大小不能超过 {MAX_IMAGE_SIZE // 1024 // 1024}MB（收到 {file_size}B）')

    # 4. 创建目录
    dir_path = UPLOAD_BASE_PATH / dir_name
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f'[{request_id}] 创建目录失败: {e}')
        return _error(500, f'无法创建目录: {e}', http_status=500)

    # 5. 保存文件
    saved_name = _sanitize_filename(file.filename)
    target_path = dir_path / saved_name
    try:
        file.save(str(target_path))
    except OSError as e:
        logger.error(f'[{request_id}] 文件保存失败: {e}')
        return _error(500, f'文件保存失败: {e}', http_status=500)

    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info(f'[{request_id}] 上传完成: {target_path}, 耗时={elapsed_ms}ms')

    return _ok({
        'savedName': saved_name,
        'savedPath': str(target_path).replace('\\', '/'),
        'elapsedMs': elapsed_ms,
    }, '上传成功')


@mobile_api.route('/upload/json', methods=['POST'])
def upload_json():
    """POST /api/mobile/upload/json — JSON文件上传

    FormData 参数：
      file — JSON 文件

    存到 {basePath}/{原始文件名}（与图片文件夹同级）
    """
    start_time = time.time()
    request_id = uuid.uuid4().hex[:8]

    file = request.files.get('file')
    logger.info(f'[{request_id}] JSON 上传开始 - file={file.filename if file else None}')

    # 1. 校验文件
    if not file or not file.filename:
        return _error(400, '请选择要上传的JSON文件')

    # 2. 校验扩展名
    ext = _get_extension(file.filename)
    if ext not in ALLOWED_JSON_EXTS:
        return _error(400, f'仅支持上传 .json 格式文件（收到 {ext}）')

    # 3. 保存文件（与图片文件夹同级）
    saved_name = _sanitize_filename(file.filename)
    target_path = UPLOAD_BASE_PATH / saved_name
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f'[{request_id}] 创建目录失败: {e}')
        return _error(500, f'无法创建目录: {e}', http_status=500)

    try:
        file.save(str(target_path))
    except OSError as e:
        logger.error(f'[{request_id}] 文件保存失败: {e}')
        return _error(500, f'文件保存失败: {e}', http_status=500)

    elapsed_ms = int((time.time() - start_time) * 1000)
    logger.info(f'[{request_id}] JSON 上传完成: {target_path}, 耗时={elapsed_ms}ms')

    return _ok({
        'savedName': saved_name,
        'savedPath': str(target_path).replace('\\', '/'),
        'elapsedMs': elapsed_ms,
    }, '上传成功')
