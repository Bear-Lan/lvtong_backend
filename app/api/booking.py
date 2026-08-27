"""预约业务 API 蓝图

处理车辆预约、雷达影像拉取、受理/驳回、急停等。
参考 Qt OrderDialog / LvTongPro::onOrderAccept() / onBookingDebounceTimeout()
"""
import threading
import time
from flask import Blueprint, request, jsonify, current_app

from config import RADAR_HEAD_URL
from app.extensions.auth import login_required
from app.devices.manager import DeviceManager
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
    'car_length': 1.5,           # 默认车头长度（光机 triggeroffset）
    'vehicle_type': 'small',     # 光机 type：small | large（与预约弹窗车型一致）
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
    """驳回时清空待受理预约（提交/重置不调用，保留下一辆已按键）。"""
    _booking_state['btn_prebook_state'] = False
    _booking_state['last_btn_click_time'] = ''


@booking_api.route('/open', methods=['POST'])
@login_required
def open_booking_session():
    """初始化预约会话（仅空闲弹窗时调用）

    POST /api/booking/open

    红灯/落杆/step2 已在预约按键时下发；此处只：
    1. 设备健康检查
    2. 标记弹窗已开
    3. 等待同一轮 step2 播完并释放 TwoWayAudio，供前端对讲
    """
    mgr = DeviceManager()
    devices_ready = mgr.health_check_all()

    _booking_state['booking_dialog_shown'] = True
    ensure_step2_announce_once()

    return ok({
        'devicesReady': devices_ready,
        'videoStreamUrl': None,
        'state': _booking_state,
        'announceStep': 2,
        'announceDone': True,
    }, '会话初始化成功')


def ensure_step2_announce_once() -> None:
    """预约键与 /open 共用：只播一轮 step2（LED+语音），其它调用方等待同轮结束。"""
    global _open_announce_inflight

    waiter: threading.Event | None = None
    with _open_announce_lock:
        if _open_announce_inflight:
            waiter = threading.Event()
            _open_announce_waiters.append(waiter)
        else:
            _open_announce_inflight = True

    if waiter is not None:
        print('[BOOKING] step2：已有播报进行中，等待同轮结束', flush=True)
        waiter.wait(timeout=60)
        return

    try:
        from app.services.device_action import execute_device_action
        from app.services.lane_announce import get_lane_announce
        from app.services.booking_camera_tts import (
            release_voice_channel,
            speak_step,
        )

        get_lane_announce().stop_idle_loop()
        try:
            execute_device_action('led_001', 'set_step2')
        except Exception as e:
            print(f'[BOOKING] LED step2 失败: {e}', flush=True)
        print('[BOOKING] step2：长连接接播（对讲仍未开）', flush=True)
        speak_step(2, interrupt=True, blocking=True)
        release_voice_channel()
        print('[BOOKING] step2：结束且 TwoWayAudio 已释放，前端可开对讲', flush=True)
    except Exception as e:
        print(f'[BOOKING] step2 播报失败: {e}', flush=True)
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

    中间件 :5001/geom/image 响应头（现场）：
    - Vehicle-Height: 车高（米）
    - Vehicle-SX: 车头距图片起始位置（米）
    - Image-Resolution: 米/像素（如 0.01 → 1px=0.01m）
    - Image-Envelope: 可选包络

    红线默认放在车头后 1.5m：像素 = (Vehicle-SX + 1.5) / Image-Resolution
    """
    radar_head_url = current_app.config.get('RADAR_HEAD_URL', RADAR_HEAD_URL)
    print(f'[BOOKING] 拉取雷达图像: {radar_head_url}')

    image_url = ''
    image_envelope = ''
    image_resolution = ''
    vehicle_header_envelope = ''
    vehicle_height = _booking_state['car_height']
    vehicle_sx = None  # 车头距图起点（米）；None=未返回 Vehicle-SX
    meters_per_pixel = 0.01
    orig_width = 0
    orig_height = 0
    content_origin_x = 0
    last_error = ''

    # gevent 下 greenlet 直连 requests 偶发卡读超时；改真实线程池拉取
    max_attempts = 3
    per_timeout = (3.0, 10.0)

    for attempt in range(1, max_attempts + 1):
        try:
            from app.services.middleware_http import middleware_get

            t0 = time.monotonic()
            resp = middleware_get(radar_head_url, timeout=per_timeout)
            elapsed = time.monotonic() - t0
            if resp.status_code == 200 and resp.content:
                orig_width, orig_height = _image_size_from_bytes(resp.content)

                from app.services.image_preview import put_temp_preview_url
                image_url = put_temp_preview_url(resp.content, radar_head_url)
                # preview 字节数仅用于日志
                from app.services.temp_image_cache import get as temp_get, parse_temp_id
                prev_entry = temp_get(parse_temp_id(image_url))
                preview_len = len(prev_entry.data) if prev_entry else 0

                image_envelope = resp.headers.get('Image-Envelope', '') or ''
                image_resolution = resp.headers.get('Image-Resolution', '') or ''
                vehicle_header_envelope = resp.headers.get('VehicleHeader-Envelope', '') or ''

                try:
                    meters_per_pixel = float(image_resolution) if image_resolution else 0.01
                    if meters_per_pixel <= 0:
                        meters_per_pixel = 0.01
                except ValueError:
                    meters_per_pixel = 0.01

                sx_str = resp.headers.get('Vehicle-SX', '') or ''
                if sx_str:
                    try:
                        vehicle_sx = float(sx_str)
                    except ValueError:
                        vehicle_sx = None

                # 车头在图中的像素起点（供兼容旧前端字段）
                if vehicle_sx is not None and meters_per_pixel > 0:
                    content_origin_x = int(round(max(0.0, vehicle_sx) / meters_per_pixel))
                    if not vehicle_header_envelope:
                        vehicle_header_envelope = f'{content_origin_x},0'

                if orig_width <= 0:
                    rw, rh = _parse_image_resolution(image_resolution)
                    if rw > 0:
                        orig_width, orig_height = rw, rh

                if (
                    content_origin_x <= 0
                    and not image_envelope
                    and not vehicle_header_envelope
                    and resp.content
                ):
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
                    f'elapsed={elapsed:.2f}s '
                    f'url={image_url} bytes={preview_len} (raw={len(resp.content)}) '
                    f'size={orig_width}x{orig_height} '
                    f'height={vehicle_height} '
                    f'sx={vehicle_sx if vehicle_sx is not None else "-"}m '
                    f'mpp={meters_per_pixel} originX={content_origin_x} '
                    f'env={image_envelope!r}',
                    flush=True,
                )
                last_error = ''
                break

            last_error = f'HTTP {resp.status_code}'
            print(
                f'[BOOKING] 雷达图像获取失败 attempt={attempt}/{max_attempts} '
                f'elapsed={elapsed:.2f}s: {last_error}',
                flush=True,
            )
        except Exception as e:
            last_error = str(e)
            print(
                f'[BOOKING] 雷达图像获取异常 attempt={attempt}/{max_attempts}: {e}',
                flush=True,
            )

        if attempt < max_attempts:
            time.sleep(0.2)

    if not image_url:
        return fail(
            502,
            f'雷达图像获取失败（已重试{max_attempts}次）: {last_error or "无图像数据"}',
            {
                'imageUrl': '',
                'imageEnvelope': '',
                'imageResolution': '',
                'metersPerPixel': 0.01,
                'vehicleHeaderEnvelope': '',
                'vehicleHeight': vehicle_height,
                'vehicleSx': None,
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
        'metersPerPixel': meters_per_pixel,
        'vehicleHeaderEnvelope': vehicle_header_envelope,
        'vehicleHeight': vehicle_height,
        'vehicleSx': vehicle_sx,
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

    1. 记状态 / acceptanceTime
    2. 异步播 step3（对讲已由前端 stop-video 关掉）
    3. 启动距离调度器；车道正常累计后抬杆（step4 可打断 step3）
    """
    body = request.get_json(silent=True) or {}
    user = request.gc_user

    vehicle_height = body.get('vehicleHeight', 3.0)
    car_head_length = body.get('carHeadLength', 2.5)
    xray_enabled = body.get('xrayEnabled', True)
    # 光机 type 与预约弹窗车型一致：前端 vehicleType(small|big) 优先；未传再按车高
    raw_vtype = str(body.get('vehicleType') or body.get('vehicle_type') or '').strip().lower()
    if raw_vtype in ('large', 'big'):
        vehicle_type = 'large'
    elif raw_vtype == 'small':
        vehicle_type = 'small'
    else:
        try:
            _h = float(vehicle_height)
        except (TypeError, ValueError):
            _h = 3.0
        vehicle_type = 'large' if _h > 2.8 else 'small'
    try:
        car_head_length = float(car_head_length)
    except (TypeError, ValueError):
        car_head_length = 2.5

    print(f'[BOOKING] 用户 {user["username"]} 受理预约: '
          f'车高={vehicle_height}, 车头长={car_head_length}, '
          f'车型={vehicle_type}, X光={xray_enabled}')

    from app.services.device_action import execute_device_action

    # 后端打点 acceptanceTime（对齐 Qt QDateTime::currentDateTime()）
    acceptance_time = _now_iso()

    # 读最后按键时间并清空「有预约」标志（车道同时只等一辆车）
    btn_prebook_time = take_book_button_for_accept()
    if btn_prebook_time:
        print(f'[BOOKING] 受理读取 last_btn_click_time={btn_prebook_time}，预约已清空')
    else:
        print('[BOOKING] 无待受理预约时间，btn_prebook_time 留空（未按键直接受理）')

    _booking_state.update({
        'car_height': vehicle_height,
        'car_length': car_head_length,
        'vehicle_type': vehicle_type,
        'is_check_xray': xray_enabled,
        'is_detection': True,
        'check_step': 1,
        'radar_count': 0,
        'acceptance_time': acceptance_time,
        'btn_prebook_time': btn_prebook_time,
    })

    # 1. 异步 step3（抬杆 step4 可打断）
    try:
        from app.services.lane_announce import get_lane_announce

        get_lane_announce().on_accepted()
    except Exception as e:
        print(f'[BOOKING] 受理播报失败: {e}')
        execute_device_action('led_001', 'set_step3')

    # 2. 启动距离调度器（含放行判定：累计达标后绿灯+抬杆）
    try:
        from app.services.scheduler import DistanceScheduler
        scheduler = DistanceScheduler()
        scheduler.set_detection_state(True)
        scheduler.start()
        print('[BOOKING] 距离调度器已启动（受理后待雷达确认再抬杆）')
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
                'vehicleType': vehicle_type,
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
    """预约按键（现场司机键 / 操作台底部「预约」图标，同一套记时）

    POST /api/booking/btn-press
    Body 可选: { "source": "ui"|"plc", "pushWs": true|false }
      - source=ui：操作台点击，默认不推 WS（避免弹窗二次打开）
      - 其它：现场按键，默认推 WS book_button

    任意时刻：记时 + 红灯 + 落杆 + step2。
    空闲时 data.openDialog=true，供前端弹窗拉图/对讲。
    """
    body = request.get_json(silent=True) or {}
    source = str(body.get('source') or '').lower()
    if 'pushWs' in body:
        push_ws = bool(body.get('pushWs'))
    else:
        push_ws = source != 'ui'
    detecting = bool(_booking_state.get('is_detection')) or int(
        _booking_state.get('check_step') or 0
    ) >= 1
    try:
        from app.services.lane_announce import get_lane_announce

        get_lane_announce().on_booking_button(push_ws=push_ws)
    except Exception as e:
        print(f'[BOOKING] btn-press 失败，仅记时间: {e}')
        mark_book_button()
    btn_time = _booking_state.get('last_btn_click_time', '')
    open_dialog = not detecting
    print(
        f'[BOOKING] btn-press source={source or "plc"} pushWs={push_ws} '
        f'time={btn_time} pending={_booking_state.get("btn_prebook_state")} '
        f'openDialog={open_dialog}'
    )
    return ok({
        'btnPressTime': btn_time,
        'btnPrebookState': bool(_booking_state.get('btn_prebook_state')),
        'lastBtnClickTime': btn_time,
        'openDialog': open_dialog,
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
        release_voice_channel()
        get_lane_announce().on_reset_to_idle()
    except Exception as e:
        print(f'[BOOKING] 复位播报状态失败: {e}')

    # 1. PLC 黄灯
    execute_device_action('plc_001', 'setPLC', {
        'redlight': False, 'yellowlight': True, 'greenlight': False
    })

    # 2. 开闸
    execute_device_action('gate_001', 'open')

    # 3. LED：申请已取消，请通行
    execute_device_action('led_001', 'set_display', {'code': 'SQYQX_QTX'})

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
        from app.services.body_camera_fetch import stop_body_camera_fetch
        stop_body_camera_fetch()
    except Exception as e:
        print(f'[BOOKING] 车身相机拉图停止失败: {e}')

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


def _perform_booking_reset(user: dict) -> dict:
    """重置预约状态机 — 底部「重置」专用。"""
    from app.services.device_action import execute_device_action

    pending = bool(_booking_state.get('btn_prebook_state'))
    pending_time = _booking_state.get('last_btn_click_time', '')
    _booking_state.update({
        'is_detection': False,
        'last_booking_state': False,
        'booking_dialog_shown': False,
        'car_height': 3.0,
        'car_length': 2.5,
        'is_check_xray': True,
        'vehicle_type': 'small',
        'check_step': 0,
        'radar_count': 0,
        'btn_prebook_time': '',
        'acceptance_time': '',
        'opengate_time': '',
        'openlightscreen_time': '',
        'closelightscreen_time': '',
        'cd_photo_time': '',
    })
    if pending:
        print(f'[BOOKING] 重置保留待受理预约 last_btn_click_time={pending_time}')

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
    try:
        from app.services.body_camera_fetch import stop_body_camera_fetch
        stop_body_camera_fetch()
    except Exception as e:
        print(f'[BOOKING] 车身相机拉图停止失败: {e}')

    try:
        from app.devices.xray import close_xray_capture
        close_xray_capture()
    except Exception as e:
        print(f'[BOOKING] 重置 xraycap=false 失败: {e}')

    try:
        from app.services.booking_camera_tts import release_voice_channel, stop_speak
        from app.services.lane_announce import get_lane_announce

        stop_speak()
        release_voice_channel()
        get_lane_announce().on_reset_to_idle()
    except Exception as e:
        print(f'[BOOKING] 复位播报状态失败: {e}')

    execute_device_action('plc_001', 'setPLC', {
        'redlight': False, 'yellowlight': True, 'greenlight': False
    })
    execute_device_action('gate_001', 'open')
    execute_device_action('led_001', 'set_step1')

    try:
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

    return dict(_booking_state)


@booking_api.route('/urgent-stop', methods=['POST'])
@login_required
def urgent_stop():
    """顶部 STOP — 仅停光机 + 停止透视轮询拉图。

    POST /api/booking/urgent-stop
    """
    user = request.gc_user
    print(f'[BOOKING] 用户 {user["username"]} 执行 STOP')

    try:
        from app.services.progressive_image_fetch import stop_progressive_panel_fetch
        stop_progressive_panel_fetch()
    except Exception as e:
        print(f'[BOOKING] STOP 停止透视轮询失败: {e}')

    try:
        from app.devices.xray import close_xray_capture
        close_xray_capture()
    except Exception as e:
        print(f'[BOOKING] STOP xraycap=false 失败: {e}')

    return ok(message='已停止光机及透视拉图')


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

    try:
        from app.devices.xray import close_xray_capture
        close_xray_capture()
    except Exception as e:
        print(f'[BOOKING] 急停复位 xraycap=false 失败: {e}')

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
    """
    user = request.gc_user
    print(f'[BOOKING] 用户 {user["username"]} 重置预约状态')
    state = _perform_booking_reset(user)
    return ok(state, '重置成功')
