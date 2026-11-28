"""图像处理 API 蓝图

X光图像处理（色阶校正、满载率计算、拼接）、行驶证OCR。
参考 Qt ImgProcess / OcrFetcher 实现。
"""
import base64
from decimal import Decimal
import math
import re
import requests
import cv2
from flask import Blueprint, request, jsonify, Response

from app.services.xray_processing import XRayProcessor
from app.extensions.auth import login_required

imaging_api = Blueprint('imaging', __name__, url_prefix='/api/imaging')


def ok(data=None, message='success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    return jsonify({'code': code, 'message': message, 'data': data}), code


@imaging_api.get('/ai-source/<temp_id>')
@login_required
def ai_source(temp_id):
    """AI读取同帧原图，避免把面板缩略图误作模型输入。"""
    from app.services.temp_image_cache import get_for_persist
    entry = get_for_persist(temp_id)
    if entry is None:
        return fail(404, 'AI原图已过期，请重新采集')
    return Response(entry.data, mimetype=entry.mime, headers={'Cache-Control': 'no-store'})


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


@imaging_api.route('/ai-load-rate', methods=['POST'])
@login_required
def ai_load_rate():
    """AI 掩码装载率（对齐绿通 AI 智能体 truck_xray_box）

    前提：透视拼接图 + 已选货箱类型。
    - 厢式货车 → 方案1（中80% + 底20%）
    - 其他 → 方案2（中20% + 底80%）

    支持：
      multipart: image + container_type_name | container_type_code
      JSON: image_base64 | image_url | image_path + container_type_name | container_type_code
    """
    from app.services.mask_loading_rate import compute_ai_load_rate

    container_name = ''
    container_code = ''
    image_bytes = b''
    filename = 'xray.jpg'

    if request.content_type and 'multipart/form-data' in request.content_type:
        f = request.files.get('image')
        container_name = (request.form.get('container_type_name') or '').strip()
        container_code = (request.form.get('container_type_code') or '').strip()
        if f and f.filename:
            image_bytes = f.read()
            filename = f.filename
    else:
        body = request.get_json(silent=True) or {}
        container_name = (body.get('container_type_name') or '').strip()
        container_code = (body.get('container_type_code') or '').strip()
        # 复用现有读图：转成 jpeg bytes 再送给 AI
        img = _load_image_from_body(body)
        if img is not None:
            ok_enc, buf = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            if ok_enc:
                image_bytes = buf.tobytes()

    if container_code and not container_name:
        try:
            from app.db.dictionary import DBDictionary
            for row in DBDictionary().getAllContainerTypes():
                if str(row.get('type_code')) == str(container_code):
                    container_name = str(row.get('type_name') or '')
                    break
        except Exception:
            pass

    if not container_name and not container_code:
        return fail(400, '请先选择货箱类型')
    if not container_name:
        container_name = str(container_code)
    if not image_bytes:
        return fail(400, '请先提供透视拼接图')

    try:
        result = compute_ai_load_rate(image_bytes, container_name, filename)
        return ok(result, 'AI 装载率计算成功')
    except ValueError as e:
        return fail(400, str(e))
    except Exception as e:
        return fail(502, f'AI 装载率失败: {e}')


@imaging_api.route('/ai-truck-real', methods=['POST'])
@login_required
def ai_truck_real():
    """AI 货车/货箱类型识别（对齐绿通 AI truck_real）

    输入：车身影像「车身图」；可选 plate_color / plate_number（两轮车一/二型判定用）。
    返回：
      vehicle_type / vehicle_type_name       — 由 wheel_count(+车牌) 映射一~六型
      vehicle_container_type / name         — 由 cratetype 映射货箱字典
    """
    from app.services.truck_real_detect import detect_truck_and_container

    image_bytes = b''
    filename = 'body.jpg'
    plate_color = ''
    plate_number = ''

    if request.content_type and 'multipart/form-data' in request.content_type:
        f = request.files.get('image')
        if f and f.filename:
            image_bytes = f.read()
            filename = f.filename
        plate_color = (request.form.get('plate_color') or '').strip()
        plate_number = (request.form.get('plate_number') or '').strip()
    else:
        body = request.get_json(silent=True) or {}
        plate_color = str(body.get('plate_color') or '').strip()
        plate_number = str(body.get('plate_number') or '').strip()
        img = _load_image_from_body(body)
        if img is not None:
            ok_enc, buf = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            if ok_enc:
                image_bytes = buf.tobytes()

    if not image_bytes:
        return fail(400, '请先提供车身图')

    try:
        result = detect_truck_and_container(
            image_bytes, filename,
            plate_color=plate_color,
            plate_number=plate_number,
        )
        return ok(result, 'AI 货车/货箱识别成功')
    except ValueError as e:
        return fail(400, str(e))
    except Exception as e:
        return fail(502, f'AI 货车/货箱识别失败: {e}')


def _read_image_upload(default_name: str) -> tuple[bytes, str]:
    """multipart image 或 JSON image_* → (bytes, filename)。"""
    image_bytes = b''
    filename = default_name
    if request.content_type and 'multipart/form-data' in request.content_type:
        f = request.files.get('image')
        if f and f.filename:
            image_bytes = f.read()
            filename = f.filename
    else:
        body = request.get_json(silent=True) or {}
        img = _load_image_from_body(body)
        if img is not None:
            ok_enc, buf = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            if ok_enc:
                image_bytes = buf.tobytes()
    return image_bytes, filename


@imaging_api.route('/ai-goods-real', methods=['POST'])
@login_required
def ai_goods_real():
    """实拍货物推荐（新版 /v1/goods/real）

    输入：货物照片；返回 goods[{code,name,pinyin,score}] 及 goods_type/goods_name（| 拼接）。
    """
    from app.services.goods_detect import detect_product_real

    image_bytes, filename = _read_image_upload('goods.jpg')
    if not image_bytes:
        return fail(400, '请先提供货物实物图')
    try:
        result = detect_product_real(image_bytes, filename)
        return ok(result, 'AI 货物实物识别成功')
    except ValueError as e:
        return fail(400, str(e))
    except Exception as e:
        return fail(502, f'AI 货物实物识别失败: {e}')


@imaging_api.route('/ai-goods-xray', methods=['POST'])
@login_required
def ai_goods_xray():
    """透视整图定位+区域分类，仅提供待核辅助推荐。"""
    from app.services.goods_detect import detect_product_xray

    image_bytes, filename = _read_image_upload('xray.jpg')
    if not image_bytes:
        return fail(400, '请先提供透视图')
    try:
        return ok(detect_product_xray(image_bytes, filename), 'AI透视辅助识别成功')
    except ValueError as e:
        return fail(400, str(e))
    except Exception as e:
        return fail(502, f'AI透视辅助识别失败: {e}')


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
    - multipart: image=<主车证>，可选 hang_image=<挂车证>，两张证照分别识别
    - JSON: { image_base64 | image_url | image_path }，由后端读取后转base64str

    返回主车证照字段和按 Qt 规则计算的出口称重参考区间，单位 kg。
    """
    from app.services.ai_client import request_json, AIError
    try:
        hang_file = request.files.get('hang_image')
        hang_bytes = hang_file.read() if hang_file is not None else None
        if hang_bytes == b'':
            return fail(400, '挂车行驶证图片为空')
        if request.files.get('image'):
            f = request.files['image']
            image_bytes = f.read()
            if not image_bytes:
                return fail(400, '行驶证图片为空')
            ocr_result = request_json('/dl_ocr/front_and_back',
                files={'image': (f.filename or 'license.jpg', image_bytes, f.mimetype or 'application/octet-stream')})
        else:
            body = request.get_json(silent=True) or {}
            img = _load_image_from_body(body)
            if img is None:
                return fail(400, '缺少可读取的行驶证图像')
            encoded, buffer = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
            if not encoded:
                return fail(400, '行驶证图像编码失败')
            ocr_result = request_json('/dl_ocr/front_and_back',
                json_body={'base64str': base64.b64encode(buffer.tobytes()).decode('ascii')})
        parsed = _parse_license_ocr(ocr_result)
        hang = None
        if hang_file is not None:
            parsed['weight_range_scope'] = 'combination'
            hang = {}
            try:
                hang_result = request_json('/dl_ocr/front_and_back', files={
                    'image': (hang_file.filename or 'hang.jpg', hang_bytes, hang_file.mimetype or 'application/octet-stream'),
                })
                hang = _parse_license_ocr(hang_result)
            except AIError:
                parsed['weight_range_error'] = '挂车行驶证OCR未成功，请检查正、副页后重试'
        parsed.update(_calc_weight_range(parsed, hang))
        return ok(parsed)

    except AIError as e:
        return fail(502, str(e))
    except Exception as e:
        return fail(500, f'OCR处理失败: {str(e)}')


def _parse_license_ocr(ocr_result: dict) -> dict:
    from app.services.ai_client import AIError

    data_list = ocr_result.get('data')
    if not isinstance(data_list, list):
        raise AIError('OCR返回字段格式异常')

    parsed = dict.fromkeys(('plate_number', 'vehicle_type', 'hdzzl', 'zbzl', 'zzl', 'wkcc'), '')
    parsed['all_fields'] = data_list
    key_map = {
        '号牌号码': 'plate_number', '车辆类型': 'vehicle_type',
        '核定载质量': 'hdzzl', '整备质量': 'zbzl',
        '总质量': 'zzl', '外廓尺寸': 'wkcc', '检验有效期': 'inspection_valid_until',
    }
    for item in data_list:
        if not isinstance(item, dict):
            continue
        key = str(item.get('key') or '').strip()
        for label, field in key_map.items():
            # 重量字段必须匹配完整名称，避免把准牵引总质量当作总质量。
            if key == label or (field not in ('hdzzl', 'zbzl', 'zzl') and label in key):
                parsed[field] = str(item.get('value') or '').strip()
                break
    return parsed


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


def _license_mass(raw: object) -> Decimal:
    value = str(raw or '').strip()
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?|[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?)\s*(?:kg|千克|公斤)?', value, re.IGNORECASE)
    return Decimal(match[1].replace(',', '')) if match else Decimal(0)


def _calc_weight_range(main: dict, hang: dict | None = None) -> dict:
    """按 Qt 单车或主挂车公式计算，保留原始边界用于称重比较。"""
    load_license = main if hang is None else hang
    payload = _license_mass(load_license.get('hdzzl'))
    curb = _license_mass(load_license.get('zbzl'))
    gross = _license_mass(load_license.get('zzl'))
    tractor_curb = Decimal(0) if hang is None else _license_mass(main.get('zbzl'))
    empty = {'weight_min': 0, 'weight_max': 0, 'weight_range_text': ''}
    if min(payload, curb, gross) <= 0 or (hang is not None and tractor_curb <= 0):
        return empty

    lower = float(payload * Decimal('0.8') + curb + tractor_curb)
    upper = float(gross * Decimal('1.05') + tractor_curb)
    if not (math.isfinite(lower) and math.isfinite(upper) and lower < upper):
        return empty
    return {'weight_min': lower, 'weight_max': upper, 'weight_range_text': f'({lower:.1f}-{upper:.1f})'}


@imaging_api.route('/ai-status', methods=['GET'])
@login_required
def ai_status():
    """只读AI模型状态，不触发设备健康检查。"""
    from app.services.ai_client import request_json, AIError
    try:
        return ok(request_json('/status', read_timeout=5))
    except AIError as e:
        return fail(502, str(e))
