"""图像处理 API 蓝图

X光图像处理（色阶校正、满载率计算）、图像代理、行驶证OCR。
参考 Qt ImgProcess / OcrFetcher 实现。
"""
from flask import Blueprint, request, jsonify

from util.auth import login_required

imaging_api = Blueprint('imaging', __name__, url_prefix='/api/imaging')


def ok(data=None, message='success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    return jsonify({'code': code, 'message': message, 'data': data}), code


@imaging_api.route('/xray/proxy', methods=['GET'])
@login_required
def xray_proxy():
    """X光图像代理（带色阶/灰度处理）

    GET /api/imaging/xray/proxy?url=<xray_image_url>&gamma=2.0&white=128&type=200

    前端通过此接口获取处理后的X光图像
    参考 Qt ImgProcess::LevelImg()
    """
    image_url = request.args.get('url', '')
    gamma = float(request.args.get('gamma', 2.0))
    white = int(request.args.get('white', 128))
    img_type = request.args.get('type', '200')  # 200 或 160

    if not image_url:
        return fail(400, '缺少图像URL参数')

    # TODO: 实际实现：
    # 1. 从 image_url 拉取图像
    # 2. 应用 OpenCV 色阶校正 (参数: gamma, white)
    # 3. 返回处理后的图像（base64 或代理 URL）
    print(f'[IMAGING] X光代理: url={image_url}, gamma={gamma}, white={white}, type={img_type}')
    return ok({'processed': False, 'message': '图像代理功能开发中'})


@imaging_api.route('/load-rate', methods=['POST'])
@login_required
def compute_load_rate():
    """计算透视图像满载率

    POST /api/imaging/load-rate
    Body: { image_path, container_type_index }

    参考 Qt ImgProcess::ComputeXRayImgLoadRateType045/Type123()
    """
    body = request.get_json(silent=True) or {}
    image_path = body.get('image_path', '')
    container_type_index = body.get('container_type_index', 0)

    if not image_path:
        return fail(400, '缺少图像路径')

    # TODO: 实际实现：
    # 1. 加载图像
    # 2. 根据 container_type_index 选择算法
    #    - index 0/4/5 → ComputeXRayImgLoadRateType045
    #    - index 1/2/3 → ComputeXRayImgLoadRateType123
    # 3. 返回满载率 (0.0-1.0)
    load_rate = 0.0
    print(f'[IMAGING] 满载率计算: path={image_path}, container_type={container_type_index}')
    return ok({'load_rate': load_rate})


@imaging_api.route('/stitch', methods=['POST'])
@login_required
def stitch_images():
    """拼接200kV和160kV透视图像

    POST /api/imaging/stitch
    Body: { image200_path, image160_path }

    参考 Qt 中 startCaptureStitchImg() 的逻辑
    """
    body = request.get_json(silent=True) or {}
    image200 = body.get('image200_path', '')
    image160 = body.get('image160_path', '')

    if not image200 or not image160:
        return fail(400, '缺少图像路径')

    # TODO: 实际实现：
    # 1. 加载两张图像
    # 2. 不应用色阶直接拼接
    # 3. 返回拼接后的图像 URL
    print(f'[IMAGING] 拼接图像: 200={image200}, 160={image160}')
    return ok({'stitched': False, 'message': '图像拼接功能开发中'})


@imaging_api.route('/ocr/driving-license', methods=['POST'])
@login_required
def ocr_driving_license():
    """行驶证OCR识别

    POST /api/imaging/ocr/driving-license
    Body: { image_path (base64 or URL), side: "front"|"back" }

    参考 Qt OcrFetcher 调用 http://{ocrUrl}/dl_ocr/front_and_back
    """
    body = request.get_json(silent=True) or {}
    image_path = body.get('image_path', '')
    side = body.get('side', 'front')

    if not image_path:
        return fail(400, '缺少图像路径')

    # TODO: 实际实现：
    # 1. 读取图像并转 base64
    # 2. POST 到 OCR 服务 /dl_ocr/front_and_back
    # 3. 解析返回的行驶证信息（号牌号码、核定载质量、整备质量、总质量、外廓尺寸等）
    print(f'[IMAGING] OCR识别: path={image_path}, side={side}')
    return ok({
        'plate_number': '',
        'vehicle_type': '',
        'hdzzl': '',   # 核定载质量
        'zbzl': '',    # 整备质量
        'zzl': '',     # 总质量
        'wkcc': '',    # 外廓尺寸
        'message': 'OCR功能开发中',
    })
