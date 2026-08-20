"""预约业务 API 蓝图

处理车辆预约、雷达影像拉取、受理/驳回、急停等。
参考 Qt OrderDialog / LvTongPro::onOrderAccept() / onBookingDebounceTimeout()
"""
import threading
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


# 并发 /open 只播一次 step2，其余请求等待同一轮结束
_open_announce_lock = threading.Lock()
_open_announce_inflight = False
_open_announce_waiters: list[threading.Event] = []


# ---- 全局状态（后续迁移到 Redis 或 scheduler 模块） ----
# 车道同时最多一辆待检车：预约只用「有无标志 + 最后按键时间」，不再用栈。
_booking_state = {
    'is_detection': False,       # 是否正在检测中
    'last_booking_state': False,
    'booking_dialog_shown': False,
    'btn_prebook_state': False,  # 是否有待受理预约（司机已按键）
    'car_height': 3.0,
    'car_length': 2.5,           # 默认车头宽度
    'is_check_xray': True,
    'check_step': 0,
    'radar_count': 0,

    # ---- 时间字段（对齐 Qt m_VehicleInspection.* 字段命名） ----
    'last_btn_click_time': '',       # 司机最后一次按预约键（待受理期间可被覆盖）
    'btn_prebook_time': '',          # 本轮受理时固化的按键时间（来自 last_btn_click_time）
    'acceptance_time': '',           # 用户点受理的时刻
    'opengate_time': '',             # 开闸时间（PLC green 或 arrived 触发）
    'openlightscreen_time': '',      # 光幕开启
    'closelightscreen_time': '',     # 光幕关闭
    'cd_photo_time': '',             # 车顶拍照时间
}


def get_booking_state():
    return _booking_state


def set_booking_state(**kwargs):
    _booking_state.update(kwargs)


def _now_iso() -> str:
    """返回 ISO 格式（秒级）的本地时间戳 — 数据库列 timestamp without time zone 本地时间。"""
    from datetime import datetime
    return datetime.now().isoformat(timespec='seconds')


def mark_book_button() -> str:
    """司机按键：置「有预约」并记录最后点击时间（覆盖写，车道只等一辆车）。"""
    now = _now_iso()
    _booking_state['btn_prebook_state'] = True
    _booking_state['last_btn_click_time'] = now
    return now


def take_book_button_for_accept() -> str:
    """受理：读出最后按键时间，并清空预约标志与待受理时间。"""
    clicked = bool(_booking_state.get('btn_prebook_state'))
    btn_time = (_booking_state.get('last_btn_click_time') or '') if clicked else ''
    _booking_state['btn_prebook_state'] = False
    _booking_state['last_btn_click_time'] = ''
    return btn_time


def clear_book_button() -> None:
    """驳回/复位：清空待受理预约。"""
    _booking_state['btn_prebook_state'] = False
    _booking_state['last_btn_click_time'] = ''


@booking_api.route('/open', methods=['POST'])
@login_required
def open_booking_session():
    """初始化预约会话

    POST /api/booking/open

    对齐 Qt OrderDialog 打开时的初始化：
    1. 设备健康检查
    2. PLC 红灯 + 关光闸（防人闯入）— 对齐 Qt onBookingDebounceTimeout
    3. 关闸
    4. LED step2
    5. 独占播报 step2，释放 TwoWayAudio 后再返回（前端才可开对讲）
    """
    mgr = DeviceManager()
    devices_ready = mgr.health_check_all()

    from app.services.device_action import execute_device_action

    execute_device_action('plc_001', 'setPLC', {
        'redlight': True, 'yellowlight': False, 'greenlight': False,
        'lightgate200': False, 'lightgate160': False,
    })
    execute_device_action('gate_001', 'close')
    execute_device_action('led_001', 'set_step2')

    _booking_state['booking_dialog_shown'] = True
    _ensure_step2_announce_once()

    return ok({
        'devicesReady': devices_ready,
        'videoStreamUrl': None,
        'state': _booking_state,
        'announceStep': 2,
        'announceDone': True,
    }, '会话初始化成功')


def _ensure_step2_announce_once() -> None:
    """多请求并发 open 时：只播一轮 step2，其它请求阻塞等待同一轮结束。"""
    global _open_announce_inflight

    waiter: threading.Event | None = None
    with _open_announce_lock:
        if _open_announce_inflight:
            waiter = threading.Event()
            _open_announce_waiters.append(waiter)
        else:
            _open_announce_inflight = True

    if waiter is not None:
        print('[BOOKING] open：已有 step2 播报进行中，等待同轮结束', flush=True)
        waiter.wait(timeout=60)
        return

    try:
        from app.services.lane_announce import get_lane_announce
        from app.services.booking_camera_tts import (
            release_voice_channel,
            speak_step,
            stop_speak,
        )

        get_lane_announce().stop_idle_loop()
        stop_speak()
        print('[BOOKING] open：开始播报 step2（对讲须关闭，独占通道）', flush=True)
        speak_step(2, interrupt=True, blocking=True)
        release_voice_channel()
        print('[BOOKING] open：step2 结束且 TwoWayAudio 已释放，前端可开对讲', flush=True)
    except Exception as e:
        print(f'[BOOKING] open 播报失败: {e}', flush=True)
        try:
            from app.services.booking_camera_tts import release_voice_channel

            release_voice_channel()
        except Exception:
            pass
    finally:
        with _open_announce_lock:
            _open_announce_inflight = False
            waiters = list(_open_announce_waiters)
            _open_announce_waiters.clear()
        for ev in waiters:
            ev.set()


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

    # 读最后按键时间并清空「有预约」标志（车道同时只等一辆车）
    btn_prebook_time = take_book_button_for_accept()
    if btn_prebook_time:
        print(f'[BOOKING] 受理读取 last_btn_click_time={btn_prebook_time}，预约已清空')
    else:
        print('[BOOKING] 无待受理预约时间，btn_prebook_time 留空（未按键直接受理）')

    # 更新全局状态
    _booking_state.update({
        'car_height': vehicle_height,
        'car_length': car_head_length,
        'is_check_xray': xray_enabled,
        'is_detection': True,
        'check_step': 1,
        'radar_count': 0,
        'acceptance_time': acceptance_time,
        'btn_prebook_time': btn_prebook_time,
        # opengate/openlight/closelight/cd_photo 在 scheduler 触发时按需打
    })

    # 1. PLC 红灯
    execute_device_action('plc_001', 'setPLC', {
        'redlight': True, 'yellowlight': False, 'greenlight': False
    })

    # 2. LED 步骤3: 「申请已受理，请等待放行」
    try:
        from app.services.lane_announce import get_lane_announce

        get_lane_announce().on_accepted()
    except Exception as e:
        print(f'[BOOKING] 受理播报失败: {e}')
        execute_device_action('led_001', 'set_step3')

    # 3. 关闭栏杆（抬杆放行时再开闸并播 step4）
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

    行为：置有预约 + 记最后点击时间 + 播报「申请已接收…」+ WS book_button。
    """
    try:
        from app.services.lane_announce import get_lane_announce

        get_lane_announce().on_booking_button()
    except Exception as e:
        print(f'[BOOKING] btn-press 播报失败，仅记时间: {e}')
        mark_book_button()
    btn_time = _booking_state.get('last_btn_click_time', '')
    print(f'[BOOKING] btn-press: time={btn_time}, pending={_booking_state.get("btn_prebook_state")}')
    return ok({
        'btnPressTime': btn_time,
        'btnPrebookState': bool(_booking_state.get('btn_prebook_state')),
        'lastBtnClickTime': btn_time,
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

    # 恢复状态（先清播报放行标记，避免随后开闸误播 step4）
    clear_book_button()
    _booking_state.update({
        'is_detection': False,
        'check_step': 0,
        'booking_dialog_shown': False,
    })
    try:
        from app.services.booking_camera_tts import release_voice_channel, stop_speak
        from app.services.lane_announce import get_lane_announce

        stop_speak()
        get_lane_announce().on_reset_to_idle()
        release_voice_channel()
    except Exception as e:
        print(f'[BOOKING] 复位播报状态失败: {e}')

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
    clear_book_button()
    _booking_state.update({
        'is_detection': False,
        'last_booking_state': False,
        'booking_dialog_shown': False,
        'car_height': 3.0,
        'car_length': 2.5,
        'is_check_xray': True,
        'check_step': 0,
        'radar_count': 0,
        'btn_prebook_time': '',
        'acceptance_time': '',
        'opengate_time': '',
        'openlightscreen_time': '',
        'closelightscreen_time': '',
        'cd_photo_time': '',
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

    # 3. 设备复位（先停语音+清播报，再开闸，避免误播/抢通道）
    try:
        from app.services.booking_camera_tts import release_voice_channel, stop_speak
        from app.services.lane_announce import get_lane_announce

        stop_speak()
        get_lane_announce().on_reset_to_idle()
        release_voice_channel()
    except Exception as e:
        print(f'[BOOKING] 复位播报状态失败: {e}')

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
