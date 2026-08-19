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
from app.services import temp_image_cache
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

    # ---- 时间字段（对齐 Qt m_VehicleInspection.* 字段命名） ----
    'btn_prebook_time': '',          # 本轮：受理时从栈中取出的最早按键时间（ISO 秒）
    'acceptance_time': '',           # 用户点受理的时刻
    'opengate_time': '',             # 开闸时间（PLC green 或 arrived 触发）
    'openlightscreen_time': '',      # 光幕开启
    'closelightscreen_time': '',     # 光幕关闭
    'cd_photo_time': '',             # 车顶拍照时间

    # ---- 栈与上一轮锚点（Qt m_lstPreBookTime + 闭区间对照） ----
    'btn_prebook_stack': [],         # 司机按键时间戳栈（按到达顺序追加；受理时 pop）
    'last_acceptance_time': '',       # 上一辆车的 acceptance_time，用于"取 > last 的最早"判定
}


def get_booking_state():
    return _booking_state


def set_booking_state(**kwargs):
    _booking_state.update(kwargs)


def _now_iso() -> str:
    """返回 ISO 格式（秒级）的本地时间戳 — 数据库列 timestamp without time zone 本地时间。"""
    from datetime import datetime
    return datetime.now().isoformat(timespec='seconds')


def _book_button_stack_push() -> str:
    """司机按键入栈 — 升沿（Qt 注释：booking && !m_lastBookingState 才打）。

    调用方应保证调用频率受控（去重由调用方做，例如仅在 /api/internal/emit
    收到 msg_type=='booking' action=='book_button' 时调用）。
    """
    now = _now_iso()
    _booking_state['btn_prebook_stack'].append(now)
    return now


def _book_button_stack_pop_for_acceptance(acceptance_time: str) -> str:
    """Qt 风格：取最早一个 > last_acceptance_time 的；如果没有 > last 的，取栈底最老。

    取出后从栈里删除该元素，同时把 < last_acceptance_time 的老尾巴清理掉。
    """
    stack = list(_booking_state['btn_prebook_stack'])
    last = _booking_state.get('last_acceptance_time', '')

    # 1. 优先：栈里 > last_acceptance_time 的最早一个
    candidates = [t for t in stack if (not last or t > last)]
    if candidates:
        picked = min(candidates)  # ISO 时间字符串字典序与时间顺序一致
    elif stack:
        # 2. 退化：栈里全是老车时间，取最老的一个作为本轮（视为"上一轮的尾巴"）
        picked = min(stack)
    else:
        picked = ''

    # 3. 清理：只保留 picked 之后的元素
    if picked and picked in stack:
        remaining = [t for t in stack if t != picked]
    else:
        remaining = stack

    # 4. 再清：< last_acceptance_time 的老尾巴
    if last:
        remaining = [t for t in remaining if t > last]

    _booking_state['btn_prebook_stack'] = remaining
    return picked


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


def _parse_image_resolution(header: str) -> tuple[int, int]:
    """解析 Image-Resolution，如 1920x1080 / 1920,1080 / 1920*1080。"""
    s = (header or '').strip().lower().replace(' ', '')
    if not s:
        return 0, 0
    for sep in ('x', '*', ',', '×'):
        if sep in s:
            parts = s.split(sep, 1)
            try:
                w = int(float(parts[0]))
                h = int(float(parts[1])) if len(parts) > 1 else 0
                if w > 0:
                    return w, h
            except (TypeError, ValueError):
                return 0, 0
    return 0, 0


def _decode_bgr(data: bytes):
    if not data:
        return None
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(data, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _image_size_from_bytes(data: bytes) -> tuple[int, int]:
    """从原图字节读宽高（车头长度按原图像素算，不能用预览缩放后的宽）。"""
    img = _decode_bgr(data)
    if img is None:
        return 0, 0
    h, w = img.shape[:2]
    return int(w), int(h)


def _leftmost_content_x(data: bytes, white_threshold: int = 245) -> int:
    """无包络头时：找点云/车辆内容最左列，作为车头起算原点（像素）。"""
    img = _decode_bgr(data)
    if img is None:
        return 0
    try:
        import numpy as np
        # 非近白像素视为内容（点云底白）
        mask = np.any(img < white_threshold, axis=2)
        cols = np.where(mask.any(axis=0))[0]
        if cols.size == 0:
            return 0
        return int(cols[0])
    except Exception:
        return 0


@booking_api.route('/radar-image', methods=['GET'])
@login_required
def fetch_radar_image():
    """拉取雷达来车图

    GET /api/booking/radar-image

    返回雷达扫描图像 URL 和相关数据。
    参考 Qt OrderDialog::onRefreshClicked() —
    从雷达设备 HTTP 接口获取图像及响应头信息。

    响应头关键字段：
    - Image-Envelope: 图像包络（车辆在图中的范围，用于初始红线位置）
    - Image-Resolution: 图像分辨率
    - VehicleHeader-Envelope: 车头包络
    - Vehicle-Height: 车辆高度

    注意：当前 :5006/image?type=appt 往往不带头；无包络时用内容最左列作原点。
    中间件偶发超时，本接口自动重试若干次。
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
    content_origin_x = 0
    last_error = ''

    # 单次 12s × 最多 3 次，避免中间件偶发超时把预约界面卡死
    max_attempts = 3
    per_timeout = 12

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(radar_head_url, timeout=per_timeout)
            if resp.status_code == 200 and resp.content:
                orig_width, orig_height = _image_size_from_bytes(resp.content)

                from app.services.image_preview import preview_from_raw
                preview = preview_from_raw(resp.content, source_url=radar_head_url)
                image_url = temp_image_cache.put_and_url(preview, 'image/jpeg')

                image_envelope = resp.headers.get('Image-Envelope', '') or ''
                image_resolution = resp.headers.get('Image-Resolution', '') or ''
                vehicle_header_envelope = resp.headers.get('VehicleHeader-Envelope', '') or ''
                if orig_width <= 0:
                    rw, rh = _parse_image_resolution(image_resolution)
                    if rw > 0:
                        orig_width, orig_height = rw, rh

                if not image_envelope and not vehicle_header_envelope and resp.content:
                    content_origin_x = _leftmost_content_x(resp.content)
                    vehicle_header_envelope = f'{content_origin_x},0'

                vh_str = resp.headers.get('Vehicle-Height', '')
                if vh_str:
                    try:
                        vehicle_height = float(vh_str)
                        _booking_state['car_height'] = vehicle_height
                    except ValueError:
                        pass

                print(
                    f'[BOOKING] 雷达图像预览(未落盘) attempt={attempt}/{max_attempts} '
                    f'url={image_url} bytes={len(preview)} (raw={len(resp.content)}) '
                    f'size={orig_width}x{orig_height} '
                    f'originX={content_origin_x} '
                    f'env={image_envelope!r} hdr={vehicle_header_envelope!r}',
                    flush=True,
                )
                last_error = ''
                break

            last_error = f'HTTP {resp.status_code}'
            print(
                f'[BOOKING] 雷达图像获取失败 attempt={attempt}/{max_attempts}: {last_error}',
                flush=True,
            )
        except requests.RequestException as e:
            last_error = str(e)
            print(
                f'[BOOKING] 雷达图像获取异常 attempt={attempt}/{max_attempts}: {e}',
                flush=True,
            )

        if attempt < max_attempts:
            time.sleep(1.0)

    if not image_url:
        return fail(
            502,
            f'雷达图像获取失败（已重试{max_attempts}次）: {last_error or "无图像数据"}',
            {
                'imageUrl': '',
                'imageEnvelope': '',
                'imageResolution': '',
                'vehicleHeaderEnvelope': '',
                'vehicleHeight': vehicle_height,
                'originalImageWidth': 0,
                'originalImageHeight': 0,
                'contentOriginX': 0,
                'attempts': max_attempts,
            },
        )

    return ok({
        'imageUrl': image_url,
        'imageEnvelope': image_envelope,
        'imageResolution': image_resolution,
        'vehicleHeaderEnvelope': vehicle_header_envelope,
        'vehicleHeight': vehicle_height,
        'originalImageWidth': orig_width,
        'originalImageHeight': orig_height,
        'contentOriginX': content_origin_x,
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

    摄像头（cam_lane / cam_booking / cam_ptz）由前端 useHikvisionPlayer 直连海康插件，不经此处。
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
    acceptance_time = _now_iso()

    # 从 btn_prebook_stack 取出本轮司机的最早按键时间（Qt m_lstPreBookTime 语义）
    btn_prebook_time = _book_button_stack_pop_for_acceptance(acceptance_time)
    if btn_prebook_time:
        print(f'[BOOKING] 从栈中取出 btn_prebook_time={btn_prebook_time} '
              f'(栈剩余 {len(_booking_state["btn_prebook_stack"])} 项)')
    else:
        print(f'[BOOKING] 栈为空，btn_prebook_time 留空（司机未按键直接受理）')

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
        'btn_prebook_time': btn_prebook_time,
        # opengate/openlight/closelight/cd_photo 在 scheduler 触发时按需打
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

    # 5. 车身/透视渐进出图：不在受理时一次拉完。
    #    对齐 Qt startXrayDetect — 雷达 33~35m（xray_capture_trigger）再轮询。

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
                'btnPrebookTime': btn_prebook_time,
                'operator': user['real_name'],
            },
        })
    except Exception as e:
        print(f'[WS] 推送失败: {e}')

    return ok({
        'acceptanceTime': acceptance_time,
        'btnPrebookTime': btn_prebook_time,
    }, '受理成功')


@booking_api.route('/btn-press', methods=['POST'])
def btn_press():
    """司机按键预约（Qt LvTongPro::onPLCBookingChanged 上升沿）

    POST /api/booking/btn-press

    行为：把当前时间压入 _booking_state.btn_prebook_stack。
    注意：不要求登录（PLC 触发），但要求来源为 mock_back/同机
    （main.py 会通过 /api/internal/emit 调用，或者直接调此 endpoint）。

    返回：当前栈长度 + 栈内容。
    """
    btn_time = _book_button_stack_push()
    stack = _booking_state['btn_prebook_stack']
    print(f'[BOOKING] btn-press 入栈: {btn_time}, 栈长度={len(stack)}')
    return ok({
        'btnPressTime': btn_time,
        'stackLen': len(stack),
        'stack': stack,
    })


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

    # 4. 停止调度器 + 渐进拉图
    try:
        from app.services.scheduler import DistanceScheduler
        scheduler = DistanceScheduler()
        scheduler.set_detection_state(False)
        scheduler.stop()
        print('[BOOKING] 距离调度器已停止')
    except Exception as e:
        print(f'[BOOKING] 调度器停止失败: {e}')
    try:
        from app.services.progressive_image_fetch import stop_progressive_panel_fetch
        stop_progressive_panel_fetch()
    except Exception as e:
        print(f'[BOOKING] 渐进拉图停止失败: {e}')

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
        # 时间字段 + 栈 + 锚点全清
        'btn_prebook_time': '',
        'acceptance_time': '',
        'opengate_time': '',
        'openlightscreen_time': '',
        'closelightscreen_time': '',
        'cd_photo_time': '',
        'btn_prebook_stack': [],
        'last_acceptance_time': '',
    })

    # 2. 停止距离调度器 + 渐进拉图
    try:
        from app.services.scheduler import DistanceScheduler
        DistanceScheduler().set_detection_state(False)
        DistanceScheduler().stop()
        print('[BOOKING] 距离调度器已停止')
    except Exception as e:
        print(f'[BOOKING] 调度器停止失败: {e}')
    try:
        from app.services.progressive_image_fetch import stop_progressive_panel_fetch
        stop_progressive_panel_fetch()
    except Exception as e:
        print(f'[BOOKING] 渐进拉图停止失败: {e}')

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
