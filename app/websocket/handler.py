"""WebSocket 处理器

使用 flask-socketio 提供 WebSocket 服务，路径为 /ws。

实时推送消息类型：
- radar_distance    雷达距离更新
- device_status     设备状态变化
- plc_status        PLC 状态变化
- xray_status       X光机状态
- booking           预约/来车通知
- detection_step    检测流程步骤变化
- image_ready       新图像可用通知
- lane_occupied     车道占用告警
"""
import threading
import time
import math
from flask_socketio import SocketIO, emit, join_room

socketio = SocketIO()

# 自维护连接数：flask-socketio 无稳定公开 API，挖内部 rooms 常失败会打出「在线 -1」
_ws_clients = 0
_ws_clients_lock = threading.Lock()


def init_socketio(app):
    """初始化 SocketIO，绑定 Flask 应用"""
    socketio.init_app(
        app,
        cors_allowed_origins='*',
        async_mode='gevent',
        ping_timeout=60,
        ping_interval=25,
    )
    register_handlers()
    from app.services.browser_talk import register_talk_handlers
    app.extensions['browser_talk'] = register_talk_handlers(socketio)
    return socketio


def get_socketio_clients_count() -> int:
    """返回当前已连接客户端数量（调试日志用）"""
    with _ws_clients_lock:
        return _ws_clients


def register_handlers():
    """注册 WebSocket 事件处理器"""

    @socketio.on('connect')
    def handle_connect(auth=None):
        global _ws_clients
        from app.extensions.auth import decode_token
        if isinstance(auth, dict) and decode_token(str(auth.get('token') or '')):
            join_room('inspection_sessions')
        with _ws_clients_lock:
            _ws_clients += 1
            n = _ws_clients
        print(f'[WS] 客户端连接 (在线 {n})')
        emit('message', {
            'type': 'connect_ack',
            'timestamp': int(time.time() * 1000),
            'data': {'message': '连接成功'},
        })
        # DEVICE 状态常只推一次：新连接补发最近一次，避免面板全灰
        plc = get_last_plc_status()
        if plc:
            emit('message', {
                'type': 'plc_status',
                'timestamp': int(time.time() * 1000),
                'data': plc,
            })
        radiometer = get_last_radiometer_status()
        if radiometer:
            emit('message', {'type': 'radiometer_status', 'timestamp': int(time.time() * 1000), 'data': radiometer})
        radar = get_last_radar_distance()
        if radar:
            emit('message', radar)

    @socketio.on('disconnect')
    def handle_disconnect():
        global _ws_clients
        with _ws_clients_lock:
            _ws_clients = max(0, _ws_clients - 1)
            n = _ws_clients
        print(f'[WS] 客户端断开 (在线 {n})')

    @socketio.on('message')
    def handle_message(payload):
        """处理通用消息"""
        msg_type = payload.get('type', '') if isinstance(payload, dict) else ''

        if msg_type == 'ping':
            emit('message', {
                'type': 'pong',
                'timestamp': int(time.time() * 1000),
                'data': {'server_time': int(time.time() * 1000)},
            })
        elif msg_type == 'subscribe':
            topics = payload.get('data', {}).get('topics', [])
            print(f'[WS] 客户端订阅: {topics}')
            emit('message', {
                'type': 'subscribe_ack',
                'timestamp': int(time.time() * 1000),
                'data': {'topics': topics},
            })
            if not topics or 'radar_distance' in topics or '*' in topics:
                radar = get_last_radar_distance()
                if radar:
                    emit('message', radar)
            if _last_plc_data and (
                not topics or 'plc_status' in topics or '*' in topics
            ):
                emit('message', {
                    'type': 'plc_status',
                    'timestamp': int(time.time() * 1000),
                    'data': dict(_last_plc_data),
                })
        else:
            print(f'[WS] 收到未知消息类型: {msg_type}')

    @socketio.on('ping')
    def handle_ping(data=None):
        """独立 ping 事件处理"""
        emit('pong', {
            'type': 'pong',
            'timestamp': int(time.time() * 1000),
            'data': {'server_time': int(time.time() * 1000)},
        })


# ========== 服务端推送工具函数 ==========

# 最近一次 PLC/DEVICE 状态（供晚连客户端 / 面板打开时回放）
_last_plc_data: dict | None = None
_last_plc_lock = threading.Lock()
_last_plc_time = 0.0
_last_radiometer_data: dict | None = None
_last_radiometer_time = 0.0
_radiometer_lock = threading.Lock()


def get_last_radiometer_status() -> dict | None:
    with _radiometer_lock:
        if _last_radiometer_data is None:
            return None
        age = max(0.0, time.monotonic() - _last_radiometer_time)
        # fresh只表示数值报文新鲜程度，不替代DEVICE设备在线位。
        return {**_last_radiometer_data, 'ageSeconds': round(age, 2), 'fresh': age <= 10.0}


def push_radiometer_status(value: float):
    global _last_radiometer_data, _last_radiometer_time
    data = {'value': value, 'unit': None, 'receivedAt': int(time.time() * 1000)}
    with _radiometer_lock:
        _last_radiometer_data = data
        _last_radiometer_time = time.monotonic()
    socketio.emit('message', {'type': 'radiometer_status', 'timestamp': data['receivedAt'],
                            'data': {**data, 'ageSeconds': 0, 'fresh': True}})

# 雷达距离推送节流：硬件常 ~10Hz；过密会压 gevent + 前端重绘
_RADAR_MIN_INTERVAL_SEC = 0.08  # ≈12.5 Hz 上限
_RADAR_MIN_DELTA_M = 0.05
_radar_push_state = {'t': 0.0, 'd': None, 'mode': None, 'count': None}
_radar_push_lock = threading.Lock()
_last_radar_message: dict | None = None
_last_radar_time = 0.0


def get_last_radar_distance() -> dict | None:
    """新连接补发5秒内最近的真实雷达位置，不回放断流前的旧位置。"""
    with _radar_push_lock:
        if _last_radar_message is None or time.monotonic() - _last_radar_time > 5.0:
            return None
        return {**_last_radar_message, 'data': dict(_last_radar_message['data'])}


def get_last_plc_status() -> dict | None:
    """返回缓存的最近一次 plc_status data（无则 None）"""
    with _last_plc_lock:
        if not _last_plc_data:
            return None
        age = max(0.0, time.monotonic() - _last_plc_time)
        return {**_last_plc_data, 'ageSeconds': round(age, 2), 'fresh': age <= 5.0}


def push_radar_distance(
    distance: float,
    mode: int = 1,
    *,
    object_count: int | None = None,
):
    """推送雷达距离更新（节流后约 ≤12Hz，且距离变化过小则跳过）

    Qt 参考: DistanceBasedScheduler::distanceChanged
    """
    global _last_radar_message, _last_radar_time
    if not math.isfinite(distance):
        return
    now = time.monotonic()
    rounded = round(distance, 2)
    count = int(object_count) if object_count is not None else None
    payload: dict = {'distance': rounded, 'mode': mode}
    if count is not None:
        payload['objectCount'] = count
    message = {'type': 'radar_distance', 'timestamp': int(time.time() * 1000), 'data': payload}
    with _radar_push_lock:
        # 缓存每个有效原始位置，实时推送仍遵守现有节流。
        _last_radar_message = message
        _last_radar_time = now
        last_t = _radar_push_state['t']
        last_d = _radar_push_state['d']
        last_mode = _radar_push_state['mode']
        last_count = _radar_push_state.get('count')
        if (
            last_d is not None
            and last_mode == mode
            and last_count == count
            and (now - last_t) < _RADAR_MIN_INTERVAL_SEC
            and abs(rounded - float(last_d)) < _RADAR_MIN_DELTA_M
        ):
            return
        _radar_push_state['t'] = now
        _radar_push_state['d'] = rounded
        _radar_push_state['mode'] = mode
        if count is not None:
            _radar_push_state['count'] = count

    socketio.emit('message', message)


def push_device_status(device_id: str, status_code: int, status_text: str = ''):
    """推送设备状态变化

    Qt 参考: DeviceManager::deviceStatusChanged
    """
    socketio.emit('message', {
        'type': 'device_status',
        'timestamp': int(time.time() * 1000),
        'data': {
            'deviceId': device_id,
            'statusCode': status_code,
            'status': status_text or {0: '离线', 1: '在线', 2: '忙碌', 3: '错误'}.get(status_code, '未知'),
        },
    })


def push_plc_status(red: bool = False, yellow: bool = False, green: bool = False,
                    greatlight: bool = False, lightgate200: bool = False,
                    lightgate160: bool = False,
                    urgentstop: bool = False,
                    booking: bool = False,
                    groundsensor: bool = False,
                    lightscreen: bool = False,
                    lightsource200: bool = False,
                    lightsource160: bool = False,
                    # CMD 补全
                    urgentstop_cmd: bool = False,
                    soundalarm: bool = False,
                    interlock160: bool = False,
                    interlock200: bool = False,
                    xraygate200_cmd: bool = False,
                    xraygate160_cmd: bool = False,
                    xraymotorreset200: bool = False,
                    xraymotorreset160: bool = False,
                    # STATUS 补全
                    stickdown: bool = False,
                    gatemotor200: bool = False,
                    gatemotor160: bool = False,
                    # 完整 camelCase（与 parse_dev_bits / ST_* 一致，供前端优先读取）
                    parsed: dict | None = None):
    """推送 PLC 状态变化

    位定义对齐硬件 CMD_*/STATUS_*；parsed 有值时以其为准并带上短别名。
    """
    data = {
        'red': red, 'yellow': yellow, 'green': green,
        'greatlight': greatlight,
        'lightgate200': lightgate200,
        'lightgate160': lightgate160,
        'urgentstop': urgentstop,
        'booking': booking,
        'groundsensor': groundsensor,
        'lightscreen': lightscreen,
        'lightsource200': lightsource200,
        'lightsource160': lightsource160,
        'urgentstop_cmd': urgentstop_cmd,
        'soundalarm': soundalarm,
        'interlock160': interlock160,
        'interlock200': interlock200,
        'xraygate200_cmd': xraygate200_cmd,
        'xraygate160_cmd': xraygate160_cmd,
        'xraymotorreset200': xraymotorreset200,
        'xraymotorreset160': xraymotorreset160,
        'stickdown': stickdown,
        'gatemotor200': gatemotor200,
        'gatemotor160': gatemotor160,
    }
    if parsed:
        data.update(parsed)
        # 短别名覆盖，保证 Dashboard / 旧逻辑仍可读
        data.update({
            'red': parsed.get('redLightCmd', red),
            'yellow': parsed.get('yellowLightCmd', yellow),
            'green': parsed.get('greenLightCmd', green),
            'greatlight': parsed.get('createLightCmd', greatlight),
            'lightgate200': parsed.get('lightGate200Status', lightgate200),
            'lightgate160': parsed.get('lightGate160Status', lightgate160),
            'urgentstop': parsed.get('urgentStopStatus', urgentstop),
            'booking': parsed.get('bookingStatus', booking),
            'groundsensor': parsed.get('groundSensorStatus', groundsensor),
            'lightscreen': parsed.get('lightScreenStatus', lightscreen),
            'lightsource200': parsed.get('lightSource200Status', lightsource200),
            'lightsource160': parsed.get('lightSource160Status', lightsource160),
            'urgentstop_cmd': parsed.get('urgentStopCmd', urgentstop_cmd),
            'soundalarm': parsed.get('soundalarmCmd', soundalarm),
            'interlock160': parsed.get('interlock160Cmd', interlock160),
            'interlock200': parsed.get('interlock200Cmd', interlock200),
            'xraygate200_cmd': parsed.get('xrayGate200Cmd', xraygate200_cmd),
            'xraygate160_cmd': parsed.get('xrayGate160Cmd', xraygate160_cmd),
            'xraymotorreset200': parsed.get('xrayMotorRest200Cmd', xraymotorreset200),
            'xraymotorreset160': parsed.get('xrayMotorRest160Cmd', xraymotorreset160),
            'stickdown': parsed.get('stickDownStatus', stickdown),
            'gatemotor200': parsed.get('gateMotor200Status', gatemotor200),
            'gatemotor160': parsed.get('gateMotor160Status', gatemotor160),
        })
    data.update(receivedAt=int(time.time() * 1000), fresh=True, ageSeconds=0)
    global _last_plc_data, _last_plc_time
    with _last_plc_lock:
        _last_plc_data = dict(data)
        _last_plc_time = time.monotonic()
    socketio.emit('message', {
        'type': 'plc_status',
        'timestamp': int(time.time() * 1000),
        'data': data,
    })


def push_xray_status(xray_type: str, kv: float, ma: float, temperature: float):
    """推送 X 光机状态

    xray_type: "200" 或 "160"
    """
    socketio.emit('message', {
        'type': 'xray_status',
        'timestamp': int(time.time() * 1000),
        'data': {
            'type': xray_type,
            'kv': kv,
            'ma': ma,
            'temperature': temperature,
        },
    })


def push_dm_temp(temperatures: list[float]):
    """推送探测器温度（$NTRMC,DM200）"""
    temps = [float(t) for t in (temperatures or [])]
    socketio.emit('message', {
        'type': 'dm_temp',
        'timestamp': int(time.time() * 1000),
        'data': {
            'temperatures': temps,
            'count': len(temps),
            'max': max(temps) if temps else None,
        },
    })


def push_detection_step(step: int, message: str = '', *, gate_released: bool = False):
    """推送检测流程步骤变化

    Qt 参考: m_checkstep 的各个阶段
    步骤: 0=空闲, 1=预约等待, 2=放行中, 3=检测中, 4=检测完成
    gate_released: True 表示抬杆放行（区别于距离阈值误推的 step=2）
    """
    data: dict = {
        'step': step,
        'message': message,
    }
    if gate_released:
        data['gateReleased'] = True
    from app.services.inspection_session import record_event
    record_event('detection_step', data)
    socketio.emit('message', {
        'type': 'detection_step',
        'timestamp': int(time.time() * 1000),
        'data': data,
    })


def push_image_ready(image_type: str, url: str):
    """推送新图像可用通知（单张）

    image_type: body/transparent/head/tail/top/goods/evidence/license
    """
    from app.services.inspection_session import record_event
    record_event('image_ready', {'imageType': image_type, 'url': url})
    socketio.emit('message', {
        'type': 'image_ready',
        'timestamp': int(time.time() * 1000),
        'data': {
            'imageType': image_type,
            'url': url,
        },
    })


def push_image_ready_group(image_type: str, urls: dict, group: int = 1):
    """推送分组图像（对齐前端 mock_back：urls['1'|'2'|'3']）

    image_type: body | transparent
    """
    # 键统一成字符串，避免部分客户端把 1 当成数字键读不到
    norm = {str(k): v for k, v in (urls or {}).items() if v}
    from app.services.inspection_session import record_event
    record_event('image_ready', {'imageType': image_type, 'urls': norm})
    n = get_socketio_clients_count()
    socketio.emit('message', {
        'type': 'image_ready',
        'timestamp': int(time.time() * 1000),
        'data': {
            'imageType': image_type,
            'group': group,
            'urls': norm,
        },
    })
    print(
        f'[WS] image_ready type={image_type} keys={list(norm.keys())} clients={n}',
        flush=True,
    )


def push_lane_occupied(occupied: bool):
    """推送车道占用/异物告警

    Qt 参考: LvTongPro::onRadarMonitor() 中 monitorFlag 判定
    """
    from app.services.inspection_session import record_event
    record_event('lane_occupied', {'occupied': occupied})
    socketio.emit('message', {
        'type': 'lane_occupied',
        'timestamp': int(time.time() * 1000),
        'data': {
            'occupied': occupied,
        },
    })


def push_booking_event(action: str, **kwargs):
    """推送预约事件

    action: coming/accepted/rejected/book_button
    """
    from app.services.inspection_session import record_event
    record_event('booking_state')
    socketio.emit('message', {
        'type': 'booking',
        'timestamp': int(time.time() * 1000),
        'data': {
            'action': action,
            **kwargs,
        },
    })
