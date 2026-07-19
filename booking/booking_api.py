"""预约业务 API 蓝图"""
from flask import Blueprint, request, jsonify

from util.auth import login_required

booking_api = Blueprint('booking', __name__, url_prefix='/api/booking')


def ok(data=None, message='success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    return jsonify({'code': code, 'message': message, 'data': data}), code


@booking_api.route('/radar-image', methods=['GET'])
@login_required
def fetch_radar_image():
    """拉取雷达来车图
    GET /api/booking/radar-image
    返回雷达扫描图像 URL 和相关数据
    """
    # TODO: 对接实际的雷达图像采集系统
    return ok({
        'imageUrl': '',
        'imageEnvelope': '',
        'imageResolution': '',
        'vehicleHeaderEnvelope': '',
        'vehicleHeight': 0,
        'originalImageWidth': 0,
        'originalImageHeight': 0,
    }, '雷达图像获取成功（暂无数据）')


@booking_api.route('/accept', methods=['POST'])
@login_required
def accept_booking():
    """受理预约
    POST /api/booking/accept
    Body: { vehicleHeight, carHeadLength, xrayEnabled, linePosition }
    """
    body = request.get_json(silent=True) or {}
    user = request.gc_user

    print(f'[INFO] 用户 {user["username"]} 受理预约: {body}')
    # TODO: 对接实际的调度系统
    return ok(message='受理成功')


@booking_api.route('/reject', methods=['POST'])
@login_required
def reject_booking():
    """驳回预约
    POST /api/booking/reject
    """
    user = request.gc_user
    print(f'[INFO] 用户 {user["username"]} 驳回预约')
    # TODO: 对接实际的调度系统
    return ok(message='已驳回')
