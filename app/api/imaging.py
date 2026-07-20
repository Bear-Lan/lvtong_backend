"""图像处理 API 蓝图

X光图像处理（色阶校正、满载率计算、拼接）、行驶证OCR。
参考 Qt ImgProcess / OcrFetcher 实现。
"""
import base64
import requests
import cv2
from flask import Blueprint, request, jsonify, Response

from app.services.xray_processing import XRayProcessor
from util.auth import login_required

imaging_api = Blueprint('imaging', __name__, url_prefix='/api/imaging')


def ok(data=None, message='success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    return jsonify({'code': code, 'message': message, 'data': data}), code


# ==================== X光图像色阶处理 ====================

@imaging_api.route('/xray/proxy', methods=['GET'])
@login_required
def xray_proxy():
    """X光图像代理（带色阶处理）

    GET /api/imaging/xray/proxy?url=<图像URL>&gamma=2.0&white=128&black=0&return_image=false

    对齐 Qt ImgProcess::LevelImg() + Levels::adjust()
    """
    image_url = request.args.get('url', '')
    if not image_url:
        return fail(400, '缺少图像URL参数')

    gamma = float(request.args.get('gamma', 2.0))
    white = int(request.args.get('white', 128))
    black = int(request.args.get('black', 0))
    return_image = request.args.get('return_image', 'false').lower() == 'true'

    try:
        resp = requests.get(image_url, timeout=15)
        if resp.status_code != 200:
            return fail(502, f'图像源返回错误: {resp.status_code}')

        img = XRayProcessor.decode_image(resp.content)
        if img is None:
            return fail(502, '图像解码失败')

        processed = XRayProcessor.level_img(img, gamma, white, black)

        if return_image:
            output = XRayProcessor.encode_image(processed, '.png')
            return Response(output, mimetype='image/png')

        encoded = base64.b64encode(
            XRayProcessor.encode_image(processed, '.jpg')
        ).decode('ascii')
        return ok({'image_base64': f'data:image/jpeg;base64,{encoded}'})

    except requests.RequestException as e:
        return fail(502, f'图像拉取失败: {str(e)}')
    except Exception as e:
        return fail(500, f'图像处理失败: {str(e)}')


# ==================== 满载率计算 ====================

@imaging_api.route('/load-rate', methods=['POST'])
@login_required
def compute_load_rate():
    """计算透视图像满载率

    POST /api/imaging/load-rate
    Body: { image_base64 | image_url | image_path, container_type_index }

    对齐 Qt:
    - containerType 0,4,5 → ComputeXRayImgLoadRateType045
    - containerType 1,2,3 → ComputeXRayImgLoadRateType123
    """
    body = request.get_json(silent=True) or {}
    container_type_index = int(body.get('container_type_index', 0))

    img = _load_image_from_body(body)
    if img is None:
        return fail(400, '无法读取图像，请提供 image_base64 / image_url / image_path')

    try:
        if container_type_index in (0, 4, 5):
            load_rate = XRayProcessor.compute_load_rate_type045(img)
            algorithm = 'type045'
        else:
            load_rate = XRayProcessor.compute_load_rate_type123(img)
            algorithm = 'type123'

        return ok({
            'load_rate': round(load_rate, 4),
            'load_rate_pct': round(load_rate * 100, 2),
            'algorithm': algorithm,
            'container_type_index': container_type_index,
        })

    except Exception as e:
        return fail(500, f'满载率计算失败: {str(e)}')


# ==================== 200+160 拼接 ====================

@imaging_api.route('/stitch', methods=['POST'])
@login_required
def stitch_images():
    """拼接200kV和160kV透视图像

    POST /api/imaging/stitch
    Body: {
      image200_base64 | image200_url | image200_path,
      image160_base64 | image160_url | image160_path,
      gamma200, white200,   // 默认 2.0 / 128
      gamma160, white160    // 默认 1.0 / 255
    }

    对齐 Qt startCaptureStitchImg() 逻辑
    """
    body = request.get_json(silent=True) or {}

    gamma200 = float(body.get('gamma200', 2.0))
    white200 = int(body.get('white200', 128))
    gamma160 = float(body.get('gamma160', 1.0))
    white160 = int(body.get('white160', 255))
    stitch_gamma = float(body.get('stitch_gamma', 1.0))
    stitch_white = int(body.get('stitch_white', 255))

    img200 = _load_image_from_body(body, '200')
    img160 = _load_image_from_body(body, '160')

    if img200 is None:
        return fail(400, '无法读取200kV图像')
    if img160 is None:
        return fail(400, '无法读取160kV图像')

    try:
        stitched = XRayProcessor.stitch_xray_images(
            img200, img160,
            gamma200, white200,
            gamma160, white160,
            stitch_gamma, stitch_white
        )

        encoded = base64.b64encode(
            XRayProcessor.encode_image(stitched, '.png')
        ).decode('ascii')
        return ok({
            'image_base64': f'data:image/png;base64,{encoded}',
            'width': stitched.shape[1],
            'height': stitched.shape[0],
        })

    except Exception as e:
        return fail(500, f'图像拼接失败: {str(e)}')


# ==================== OCR 行驶证识别 ====================

@imaging_api.route('/ocr/driving-license', methods=['POST'])
@login_required
def ocr_driving_license():
    """行驶证OCR识别

    POST /api/imaging/ocr/driving-license
    Body: { image_base64 | image_url, side: "front"|"back" }

    参考 Qt OcrFetcher — POST {ocrUrl}/dl_ocr/front_and_back
    解析号牌号码、核定载质量、整备质量、总质量、外廓尺寸，
    并自动计算称重合格区间。
    """
    body = request.get_json(silent=True) or {}

    from config import OCR_SERVICE_URL
    ocr_url = f'{OCR_SERVICE_URL}/dl_ocr/front_and_back'

    ocr_body = {}
    if body.get('image_base64'):
        ocr_body['image'] = body['image_base64']
        ocr_body['side'] = body.get('side', 'front')
    elif body.get('image_url'):
        ocr_body['image_url'] = body['image_url']
        ocr_body['side'] = body.get('side', 'front')
    else:
        return fail(400, '缺少图像数据 (image_base64 或 image_url)')

    try:
        resp = requests.post(ocr_url, json=ocr_body, timeout=30)
        if resp.status_code != 200:
            return fail(502, f'OCR服务返回错误: {resp.status_code}')

        ocr_result = resp.json()
        data_list = ocr_result.get('data', [])

        # 按 key 匹配（对齐 Qt parseMainVehicleFromJson）
        parsed = {
            'plate_number': '', 'vehicle_type': '',
            'hdzzl': '', 'zbzl': '', 'zzl': '', 'wkcc': '',
            'all_fields': data_list,
        }

        key_map = {
            '号牌号码': 'plate_number', '车辆类型': 'vehicle_type',
            '核定载质量': 'hdzzl', '整备质量': 'zbzl',
            '总质量': 'zzl', '外廓尺寸': 'wkcc',
        }
        for item in data_list:
            key = item.get('key', '')
            value = item.get('value', '')
            for qt_key, our_key in key_map.items():
                if qt_key in key:
                    parsed[our_key] = value
                    break

        # 计算称重区间（对齐 Qt weight range 公式）
        parsed.update(_calc_weight_range(parsed))

        return ok(parsed)

    except requests.RequestException as e:
        return fail(502, f'OCR服务不可达: {str(e)}')
    except Exception as e:
        return fail(500, f'OCR处理失败: {str(e)}')


# ==================== 辅助函数 ====================

def _load_image_from_body(body: dict, prefix: str = '') -> 'np.ndarray | None':
    """从请求体中加载图像

    按优先级: base64 > url > path
    prefix 为空时使用 'image_base64' / 'image_url' / 'image_path'
    prefix 不为空时使用 'image{prefix}_base64' / 'image{prefix}_url' / 'image{prefix}_path'
    """
    import numpy as np

    if prefix:
        keys = [
            f'image{prefix}_base64', f'image{prefix}_url', f'image{prefix}_path'
        ]
    else:
        keys = ['image_base64', 'image_url', 'image_path']

    for key in keys:
        val = body.get(key, '')
        if not val:
            continue

        if 'base64' in key:
            if ',' in val:
                val = val.split(',', 1)[1]
            return XRayProcessor.decode_image(base64.b64decode(val))
        elif 'url' in key:
            try:
                resp = requests.get(val, timeout=15)
                if resp.status_code == 200:
                    return XRayProcessor.decode_image(resp.content)
            except requests.RequestException:
                continue
        elif 'path' in key:
            img = cv2.imread(val)
            if img is not None:
                return img

    return None


def _calc_weight_range(parsed: dict) -> dict:
    """计算称重合格区间

    对齐 Qt:
      min = hdzzl * 0.8 + zbzl
      max = zzl * 1.05
    """
    try:
        hdzzl = float(parsed.get('hdzzl', '0').replace('kg', '').strip())
        zbzl = float(parsed.get('zbzl', '0').replace('kg', '').strip())
        zzl = float(parsed.get('zzl', '0').replace('kg', '').strip())

        weight_min = hdzzl * 0.8 + zbzl
        weight_max = zzl * 1.05

        return {
            'weight_min': round(weight_min, 1),
            'weight_max': round(weight_max, 1),
            'weight_range_text': f'称重合格区间: [{weight_min:.1f} kg - {weight_max:.1f} kg]',
        }
    except (ValueError, AttributeError):
        return {'weight_min': 0, 'weight_max': 0, 'weight_range_text': ''}
