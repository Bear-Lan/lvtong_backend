"""预约业务 API 蓝图

处理车辆预约、雷达影像拉取、受理/驳回、急停等。
参考 Qt OrderDialog / LvTongPro::onOrderAccept() / onBookingDebounceTimeout()
"""
import time
from flask import Blueprint, request, jsonify

from util.auth import login_required

booking_api = Blueprint('booking', __name__, url_prefix='/api/booking')


def ok(data=None, message='success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    return jsonify({'code': code, 'message': message, 'data': data}), code


# ---- 全局状态（后续迁移到 Redis 或 scheduler 模块） ----
_booking_state = {
    'is_detection': False,       # 是否正在检测中
    'last_booking_state': False,
    'booking_dialog_shown': False,
    'btn_prebook_state': False,
    'car_height': 3.0,
    'car_length': 2.5,           # 默认车头宽度
    'is_check_xray': True,
    'check_step': 0,
    'radar_count': 0,
}


def get_booking_state():
    return _booking_state


def set_booking_state(**kwargs):
    _booking_state.update(kwargs)


@booking_api.route('/radar-image', methods=['GET'])
@login_required
def fetch_radar_image():
    """拉取雷达来车图

    GET /api/booking/radar-image

    返回雷达扫描图像 URL 和相关数据。
    参考 Qt OrderDialog::onRefreshClicked() —
    从雷达设备 HTTP 接口获取图像及响应头信息。

    响应头关键字段：
    - Image-Envelope: 图像包络信息
    - Image-Resolution: 图像分辨率
    - VehicleHeader-Envelope: 车头包络
    - Vehicle-Height: 车辆高度
    """
    # TODO: 对接实际的雷达图像采集系统
    # 参考 Qt 实现：
    # 1. GET m_radarHeadUrl (从 config/app.json radarConfig.headurl 读取)
    # 2. 解析响应头: Image-Envelope, Image-Resolution,
    #    VehicleHeader-Envelope, Vehicle-Height
    # 3. 保存原始图片尺寸用于距离计算
    # 4. 返回图像数据 + 元信息
    from flask import current_app

    radar_head_url = current_app.config.get('RADAR_HEAD_URL', '')
    print(f'[BOOKING] 拉取雷达图像: {radar_head_url}')

    return ok({
        'imageUrl': '',
        'imageEnvelope': '',
        'imageResolution': '',
        'vehicleHeaderEnvelope': '',
        'vehicleHeight': _booking_state['car_height'],
        'originalImageWidth': 0,
        'originalImageHeight': 0,
    }, '雷达图像获取成功')


@booking_api.route('/accept', methods=['POST'])
@login_required
def accept_booking():
    """受理预约

    POST /api/booking/accept
    Body: { vehicleHeight, carHeadLength, xrayEnabled, linePosition }

    参考 Qt LvTongPro::onOrderAccept()
    1. 健康检查（设备是否在线）
    2. 关闭栏杆、设置红灯
    3. LED 显示 "待检车辆 请通行"
    4. 启动距离调度器
    5. 进入检测流程
    """
    body = request.get_json(silent=True) or {}
    user = request.gc_user

    vehicle_height = body.get('vehicleHeight', 3.0)
    car_head_length = body.get('carHeadLength', 2.5)
    xray_enabled = body.get('xrayEnabled', True)

    print(f'[BOOKING] 用户 {user["username"]} 受理预约: '
          f'车高={vehicle_height}, 车头长={car_head_length}, X光={xray_enabled}')

    # 更新全局状态
    _booking_state.update({
        'car_height': vehicle_height,
        'car_length': car_head_length,
        'is_check_xray': xray_enabled,
        'is_detection': True,
        'check_step': 1,
        'radar_count': 0,
        'btn_prebook_state': False,
    })

    # TODO: 对接实际的设备控制
    # 1. 健康检查 m_deviceManager->HealthCheck()
    # 2. PLC 红灯: m_plc->executeAction("setPLC", {red:true, yellow:false, green:false})
    # 3. LED: m_led->setStep4()
    # 4. 关闭栏杆: m_gate->closeGate()
    # 5. 启动调度器: m_distanceScheduler->startMonitoring()

    # 通过 WebSocket 推送受理状态
    try:
        from ws.ws_handler import socketio
        socketio.emit('message', {
            'type': 'booking_accepted',
            'timestamp': int(time.time() * 1000),
            'data': {
                'vehicleHeight': vehicle_height,
                'carHeadLength': car_head_length,
                'xrayEnabled': xray_enabled,
                'operator': user['real_name'],
            },
        })
    except Exception as e:
        print(f'[WS] 推送失败: {e}')

    return ok(message='受理成功')


@booking_api.route('/reject', methods=['POST'])
@login_required
def reject_booking():
    """驳回预约

    POST /api/booking/reject

    参考 Qt LvTongPro::onOrderReject()
    1. 复位 PLC（黄灯）
    2. 打开栏杆
    3. LED 恢复 "绿通车辆 按键检测"
    4. 停止调度器检测
    """
    user = request.gc_user
    print(f'[BOOKING] 用户 {user["username"]} 驳回预约')

    # 恢复状态
    _booking_state.update({
        'is_detection': False,
        'btn_prebook_state': False,
        'check_step': 0,
    })

    # TODO: 对接实际的设备控制
    # 1. PLC 黄灯: executePLCCtrl(false, true, false)
    # 2. 开门: m_gate->openGate()
    # 3. LED: m_led->setStep1()

    try:
        from ws.ws_handler import socketio
        socketio.emit('message', {
            'type': 'booking_rejected',
            'timestamp': int(time.time() * 1000),
            'data': {'operator': user['real_name']},
        })
    except Exception as e:
        print(f'[WS] 推送失败: {e}')

    return ok(message='已驳回')


@booking_api.route('/urgent-stop', methods=['POST'])
@login_required
def urgent_stop():
    """急停操作

    POST /api/booking/urgent-stop

    参考 Qt LvTongPro::onStopClicked()
    """
    user = request.gc_user
    print(f'[BOOKING] 用户 {user["username"]} 执行急停')

    # TODO: 对接实际的设备控制
    # m_plc->executeAction("setPLC", {urgentstop: true})

    try:
        from ws.ws_handler import socketio
        socketio.emit('message', {
            'type': 'urgent_stop',
            'timestamp': int(time.time() * 1000),
            'data': {'operator': user['real_name']},
        })
    except Exception as e:
        print(f'[WS] 推送失败: {e}')

    return ok(message='急停指令已发送')


@booking_api.route('/stop-reset', methods=['POST'])
@login_required
def stop_reset():
    """急停复位

    POST /api/booking/stop-reset

    参考 Qt LvTongPro::onPLCStopChanged()
    """
    user = request.gc_user
    print(f'[BOOKING] 用户 {user["username"]} 急停复位')

    # TODO: 对接实际的设备控制
    # m_plc->executeAction("setPLC", {urgentstop: false})

    return ok(message='急停复位已执行')


@booking_api.route('/state', methods=['GET'])
@login_required
def booking_state():
    """获取当前预约/检测状态"""
    return ok(_booking_state)
