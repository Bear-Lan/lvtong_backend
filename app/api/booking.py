"""预约业务 API 蓝图

处理车辆预约、雷达影像拉取、受理/驳回、急停等。
参考 Qt OrderDialog / LvTongPro::onOrderAccept() / onBookingDebounceTimeout()
"""
import time
import requests
from flask import Blueprint, request, jsonify, current_app

from config import RADAR_HEAD_URL
from app.extensions.auth import login_required
from app.devices.manager import DeviceManager
from app.services.image_store import ImageStore
from app.websocket.handler import socketio

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


@booking_api.route('/open', methods=['POST'])
@login_required
def open_booking_session():
    """初始化预约会话

    POST /api/booking/open

    对齐 Qt OrderDialog 打开时的初始化：
    1. 设备健康检查
    2. PLC 红灯 + 关光闸（防人闯入）— 对齐 Qt onBookingDebounceTimeout
    3. 关闸
    4. LED step2 "绿通车辆 按键检测"
    5. 返回视频流 URL 和当前状态
    """
    mgr = DeviceManager()
    devices_ready = mgr.health_check_all()

    from app.services.device_action import execute_device_action

    # 设备联动（对齐 Qt onBookingDebounceTimeout 防抖后动作）
    execute_device_action('plc_001', 'setPLC', {
        'redlight': True, 'yellowlight': False, 'greenlight': False,
        'lightgate200': False, 'lightgate160': False,
    })
    execute_device_action('gate_001', 'close')
    execute_device_action('led_001', 'set_step2')

    # 标记预约弹窗已显示（对齐 Qt m_bookingDialogShown）
    _booking_state['booking_dialog_shown'] = True

    return ok({
        'devicesReady': devices_ready,
        'videoStreamUrl': None,  # 后续对接视频流
        'state': _booking_state,
    }, '会话初始化成功')


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
    radar_head_url = current_app.config.get('RADAR_HEAD_URL', RADAR_HEAD_URL)
    print(f'[BOOKING] 拉取雷达图像: {radar_head_url}')

    image_url = ''
    image_envelope = ''
    image_resolution = ''
    vehicle_header_envelope = ''
    vehicle_height = _booking_state['car_height']
    orig_width = 0
    orig_height = 0

    try:
        resp = requests.get(radar_head_url, timeout=10)
        if resp.status_code == 200:
            # 保存雷达图像到本地存储
            store = ImageStore()
            image_url = store.save_image_bytes(resp.content, 'radar', 'head')

            # 解析响应头
            image_envelope = resp.headers.get('Image-Envelope', '')
            image_resolution = resp.headers.get('Image-Resolution', '')
            vehicle_header_envelope = resp.headers.get('VehicleHeader-Envelope', '')
            vh_str = resp.headers.get('Vehicle-Height', '')
            if vh_str:
                try:
                    vehicle_height = float(vh_str)
                    _booking_state['car_height'] = vehicle_height
                except ValueError:
                    pass
        else:
            print(f'[BOOKING] 雷达图像获取失败: HTTP {resp.status_code}')
    except requests.RequestException as e:
        print(f'[BOOKING] 雷达图像获取异常: {e}')

    return ok({
        'imageUrl': image_url,
        'imageEnvelope': image_envelope,
        'imageResolution': image_resolution,
        'vehicleHeaderEnvelope': vehicle_header_envelope,
        'vehicleHeight': vehicle_height,
        'originalImageWidth': orig_width,
        'originalImageHeight': orig_height,
    }, '雷达图像获取成功')


@booking_api.route('/accept', methods=['POST'])
@login_required
def accept_booking():
    """受理预约

    POST /api/booking/accept
    Body: { vehicleHeight, carHeadLength, xrayEnabled, linePosition }

    参考 Qt LvTongPro::onOrderAccept()
    1. PLC 红灯 / 关闭栏杆 / LED step4（走 device_action：mock agent 或真实硬件）
    2. 启动距离调度器
    3. 进入检测流程（acceptanceTime 后端打点）

    摄像头（camera1/2）由前端 useHikvisionPlayer 直连海康插件，不经此处。
    """
    body = request.get_json(silent=True) or {}
    user = request.gc_user

    vehicle_height = body.get('vehicleHeight', 3.0)
    car_head_length = body.get('carHeadLength', 2.5)
    xray_enabled = body.get('xrayEnabled', True)

    print(f'[BOOKING] 用户 {user["username"]} 受理预约: '
          f'车高={vehicle_height}, 车头长={car_head_length}, X光={xray_enabled}')

    from app.services.device_action import execute_device_action

    # 后端打点 acceptanceTime（对齐 Qt QDateTime::currentDateTime()）
    from datetime import datetime
    acceptance_time = datetime.now().isoformat(timespec='seconds')

    # 更新全局状态
    _booking_state.update({
        'car_height': vehicle_height,
        'car_length': car_head_length,
        'is_check_xray': xray_enabled,
        'is_detection': True,
        'check_step': 1,
        'radar_count': 0,
        'btn_prebook_state': False,
        'acceptance_time': acceptance_time,
    })

    # 1. PLC 红灯
    execute_device_action('plc_001', 'setPLC', {
        'redlight': True, 'yellowlight': False, 'greenlight': False
    })

    # 2. LED 步骤4: "待检车辆 请通行 勿停车倒车"
    execute_device_action('led_001', 'set_step4')

    # 3. 关闭栏杆
    execute_device_action('gate_001', 'close')

    # 4. 启动距离调度器
    try:
        from app.services.scheduler import DistanceScheduler
        scheduler = DistanceScheduler()
        scheduler.set_detection_state(True)
        scheduler.start()
        print('[BOOKING] 距离调度器已启动')
    except Exception as e:
        print(f'[BOOKING] 调度器启动失败: {e}')

    # 通过 WebSocket 推送受理状态
    try:
        from app.websocket.handler import socketio
        socketio.emit('message', {
            'type': 'booking_accepted',
            'timestamp': int(time.time() * 1000),
            'data': {
                'vehicleHeight': vehicle_height,
                'carHeadLength': car_head_length,
                'xrayEnabled': xray_enabled,
                'acceptanceTime': acceptance_time,
                'operator': user['real_name'],
            },
        })
    except Exception as e:
        print(f'[WS] 推送失败: {e}')

    return ok({
        'acceptanceTime': acceptance_time,
    }, '受理成功')


@booking_api.route('/reject', methods=['POST'])
@login_required
def reject_booking():
    """驳回预约

    POST /api/booking/reject

    参考 Qt LvTongPro::onOrderReject()
    1. PLC 黄灯 / 开闸 / LED step1（走 device_action）
    2. 停止调度器
    """
    user = request.gc_user
    print(f'[BOOKING] 用户 {user["username"]} 驳回预约')

    from app.services.device_action import execute_device_action

    # 恢复状态
    _booking_state.update({
        'is_detection': False,
        'btn_prebook_state': False,
        'check_step': 0,
    })

    # 1. PLC 黄灯
    execute_device_action('plc_001', 'setPLC', {
        'redlight': False, 'yellowlight': True, 'greenlight': False
    })

    # 2. 开闸
    execute_device_action('gate_001', 'open')

    # 3. LED step1: "绿通车辆 按键检测"
    execute_device_action('led_001', 'set_step1')

    # 4. 停止调度器
    try:
        from app.services.scheduler import DistanceScheduler
        scheduler = DistanceScheduler()
        scheduler.set_detection_state(False)
        scheduler.stop()
        print('[BOOKING] 距离调度器已停止')
    except Exception as e:
        print(f'[BOOKING] 调度器停止失败: {e}')

    try:
        from app.websocket.handler import socketio
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

    对齐 Qt LvTongPro::onStopClicked()
    """
    user = request.gc_user
    print(f'[BOOKING] 用户 {user["username"]} 执行急停')

    from app.services.device_action import execute_device_action

    # 对齐 Qt: m_plc->executeAction("setPLC", {urgentstop: true})
    success = execute_device_action('plc_001', 'setPLC', {'urgentstop': True})
    if not success:
        return fail(503, 'PLC 急停控制失败，请检查设备连接')

    socketio.emit('message', {
        'type': 'plc_status',
        'timestamp': int(time.time() * 1000),
        'data': {
            'urgentstop': True,
            'operator': user['real_name'],
        },
    })

    return ok(message='急停指令已发送')


@booking_api.route('/stop-reset', methods=['POST'])
@login_required
def stop_reset():
    """急停复位

    POST /api/booking/stop-reset

    对齐 Qt LvTongPro::onPLCStopChanged()
    """
    user = request.gc_user
    print(f'[BOOKING] 用户 {user["username"]} 急停复位')

    from app.services.device_action import execute_device_action

    # 对齐 Qt: m_plc->executeAction("setPLC", {urgentstop: false})
    success = execute_device_action('plc_001', 'setPLC', {'urgentstop': False})
    if not success:
        return fail(503, 'PLC 复位失败，请检查设备连接')

    socketio.emit('message', {
        'type': 'plc_status',
        'timestamp': int(time.time() * 1000),
        'data': {
            'urgentstop': False,
            'operator': user['real_name'],
        },
    })

    return ok(message='急停复位已执行')


@booking_api.route('/stop-video', methods=['POST'])
@login_required
def stop_video_session():
    """停止预约弹窗的视频对讲

    对齐 Qt LvTongPro::stopSpCamera()
    """
    user = request.gc_user
    print(f'[BOOKING] 用户 {user["username"]} 停止视频对讲')
    return ok(message='视频会话已停止')


@booking_api.route('/state', methods=['GET'])
@login_required
def booking_state():
    """获取当前预约/检测状态"""
    return ok(_booking_state)


@booking_api.route('/reset', methods=['POST'])
@login_required
def reset_booking():
    """重置预约状态机 — 对齐 LvTongPro::onReset

    POST /api/booking/reset

    1. 重置 _booking_state（is_detection=false, check_step=0, ...）
    2. 停止 DistanceScheduler
    3. 设备复位（PLC 黄灯 + 开闸 + LED step1）
    4. WS 推 booking_reset 事件同步前端
    """
    user = request.gc_user
    print(f'[BOOKING] 用户 {user["username"]} 重置预约状态')

    from app.services.device_action import execute_device_action

    # 1. 重置 _booking_state 到初始值
    _booking_state.update({
        'is_detection': False,
        'last_booking_state': False,
        'booking_dialog_shown': False,
        'btn_prebook_state': False,
        'car_height': 3.0,
        'car_length': 2.5,
        'is_check_xray': True,
        'check_step': 0,
        'radar_count': 0,
    })

    # 2. 停止距离调度器
    try:
        from app.services.scheduler import DistanceScheduler
        DistanceScheduler().set_detection_state(False)
        DistanceScheduler().stop()
        print('[BOOKING] 距离调度器已停止')
    except Exception as e:
        print(f'[BOOKING] 调度器停止失败: {e}')

    # 3. 设备复位（PLC 黄灯 + 开闸 + LED step1）
    execute_device_action('plc_001', 'setPLC', {
        'redlight': False, 'yellowlight': True, 'greenlight': False
    })
    execute_device_action('gate_001', 'open')
    execute_device_action('led_001', 'set_step1')

    # 4. WS 推 booking_reset 给前端
    try:
        from app.websocket.handler import socketio
        socketio.emit('message', {
            'type': 'booking_reset',
            'timestamp': int(time.time() * 1000),
            'data': {
                'state': dict(_booking_state),
                'operator': user['real_name'],
            },
        })
    except Exception as e:
        print(f'[WS] 推送 booking_reset 失败: {e}')

    return ok(_booking_state, '重置成功')
