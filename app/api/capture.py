"""图像采集 API 蓝图

提供检测流程中各类型图像的采集接口。
摄像头 URL 从设备配置读取，支持 HTTP 抓图。
"""
import requests
from flask import Blueprint, request, jsonify, current_app

from app.services.image_store import ImageStore
from app.devices.manager import DeviceManager
from app.extensions.auth import login_required
from config import BODY_IMAGE_URL

capture_api = Blueprint('capture', __name__, url_prefix='/api/capture')


def ok(data=None, message='success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    return jsonify({'code': code, 'message': message, 'data': data}), code


# 采集类别 → 硬件 URL 映射
# 车头/车尾/车顶 → BODY_IMAGE_URL（车身相机），货物/证据照/行驶证 → 暂无 URL 留空走本地
CAPTURE_URL_MAP = {
    'head': BODY_IMAGE_URL,
    'tail': BODY_IMAGE_URL,
    'top': BODY_IMAGE_URL,
    'goods': '',           # 高拍仪 URL 后续配置
    'license': '',         # 行驶证拍照 URL 后续配置
    'evidence': '',        # 证据照 URL 后续配置
}


def _fetch_and_save(category: str, url: str = '') -> dict:
    """从 URL 拉取图像并保存，返回 API 路径和状态"""
    store = ImageStore()

    if url:
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                api_path = store.save_image_bytes(resp.content, category)
                _push_image_ready(category, api_path)
                return {'apiPath': api_path, 'size': len(resp.content)}
            else:
                raise Exception(f'硬件返回状态码 {resp.status_code}')
        except requests.RequestException as e:
            raise Exception(f'硬件连接失败: {str(e)}')
    else:
        raise Exception(f'采集类别 "{category}" 未配置硬件 URL，请检查设备配置')


def _push_image_ready(image_type: str, url: str):
    """通过 WebSocket 推送新图像通知"""
    try:
        from app.websocket.handler import push_image_ready
        push_image_ready(image_type, url)
    except Exception:
        pass


def _get_device_capture_url(category: str) -> str:
    """从设备管理器查找对应类别的采集 URL"""
    # 优先使用 CAPTURE_URL_MAP 的直接映射
    if category in CAPTURE_URL_MAP and CAPTURE_URL_MAP[category]:
        return CAPTURE_URL_MAP[category]

    # 其次从设备管理器查找摄像头/相机设备
    mgr = DeviceManager()
    cameras = mgr.get_devices_by_type('camera')
    for cam in cameras:
        url = cam.config.get(f'{category}_capture_url', '')
        if url:
            return url

    # 最后回退到通用配置
    return current_app.config.get(f'{category.upper()}_CAPTURE_URL', '')


# ==================== 单图采集 ====================

@capture_api.route('/head', methods=['POST'])
@login_required
def capture_head():
    """采集车头照片"""
    try:
        url = _get_device_capture_url('head')
        result = _fetch_and_save('head', url)
        return ok(result, '车头照片采集成功')
    except Exception as e:
        return fail(500, f'车头照片采集失败: {str(e)}')


@capture_api.route('/tail', methods=['POST'])
@login_required
def capture_tail():
    """采集车尾照片"""
    try:
        url = _get_device_capture_url('tail')
        result = _fetch_and_save('tail', url)
        return ok(result, '车尾照片采集成功')
    except Exception as e:
        return fail(500, f'车尾照片采集失败: {str(e)}')


@capture_api.route('/top', methods=['POST'])
@login_required
def capture_top():
    """采集车顶照片"""
    try:
        url = _get_device_capture_url('top')
        result = _fetch_and_save('top', url)
        return ok(result, '车顶照片采集成功')
    except Exception as e:
        return fail(500, f'车顶照片采集失败: {str(e)}')


@capture_api.route('/goods', methods=['POST'])
@login_required
def capture_goods():
    """采集货物照片"""
    try:
        url = _get_device_capture_url('goods')
        result = _fetch_and_save('goods', url)
        return ok(result, '货物照片采集成功')
    except Exception as e:
        return fail(500, f'货物照片采集失败: {str(e)}')


@capture_api.route('/license', methods=['POST'])
@login_required
def capture_license():
    """采集行驶证照片"""
    try:
        url = _get_device_capture_url('license')
        result = _fetch_and_save('license', url)
        return ok(result, '行驶证照片采集成功')
    except Exception as e:
        return fail(500, f'行驶证照片采集失败: {str(e)}')


@capture_api.route('/evidence', methods=['POST'])
@login_required
def capture_evidence():
    """采集证据照"""
    try:
        url = _get_device_capture_url('evidence')
        result = _fetch_and_save('evidence', url)
        return ok(result, '证据照采集成功')
    except Exception as e:
        return fail(500, f'证据照采集失败: {str(e)}')


# ==================== 批量采集 ====================

@capture_api.route('/bulk', methods=['POST'])
@login_required
def capture_bulk():
    """批量采集（对齐 Qt auto-capture 阶段）"""
    body = request.get_json(silent=True) or {}
    categories = body.get('categories', ['head', 'tail', 'top'])
    results = {}
    errors = {}

    for cat in categories:
        try:
            url = _get_device_capture_url(cat)
            results[cat] = _fetch_and_save(cat, url)
        except Exception as e:
            errors[cat] = str(e)

    return ok({
        'captured': results,
        'failed': errors,
    }, f'批量采集完成: {len(results)} 成功, {len(errors)} 失败')
