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
from flask_socketio import SocketIO, emit

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
    return socketio


def get_socketio_clients_count() -> int:
    """返回当前已连接客户端数量（调试日志用）"""
    with _ws_clients_lock:
        return _ws_clients


def register_handlers():
    """注册 WebSocket 事件处理器"""

    @socketio.on('connect')
    def handle_connect():
        global _ws_clients
        with _ws_clients_lock:
            _ws_clients += 1
            n = _ws_clients
        print(f'[WS] 客户端连接 (在线 {n})')
        emit('message', {
            'type': 'connect_ack',
            'timestamp': int(time.time() * 1000),
            'data': {'message': '连接成功'},
        })

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

def push_radar_distance(distance: float, mode: int = 1):
    """推送雷达距离更新（高频：~100ms）

    Qt 参考: DistanceBasedScheduler::distanceChanged
    """
    socketio.emit('message', {
        'type': 'radar_distance',
        'timestamp': int(time.time() * 1000),
        'data': {
            'distance': round(distance, 2),
            'mode': mode,
        },
    })


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
                    # 对齐 Qt udpradar.cpp 位掩码解析 (0x0100~0x4000)
                    urgentstop: bool = False,
                    booking: bool = False,
                    groundsensor: bool = False,
                    lightscreen: bool = False,
                    lightsource200: bool = False,
                    lightsource160: bool = False):
    """推送 PLC 状态变化

    Qt 参考: UDPRadar::processPLCData() → LvTongPro::onPLCStatusUpdate()
    字段对齐 Qt udpradar.cpp 位掩码:
      0x0100 urgentStopStatus  0x0200 bookingStatus
      0x0800 groundSensorStatus  0x1000 lightScreenStatus
      0x2000 lightGate200Status  0x4000 lightGate160Status
      0x0002 lightSource200Status  0x0004 lightSource160Status
    """
    socketio.emit('message', {
        'type': 'plc_status',
        'timestamp': int(time.time() * 1000),
        'data': {
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
        },
    })


def push_xray_status(xray_type: str, kv: float, ma: float, temperature: float):
    """推送 X 光机状态

    xray_type: "200" 或 "160"
    Qt 参考: UDPRadar 中 X 光温度数据解析
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


def push_detection_step(step: int, message: str = ''):
    """推送检测流程步骤变化

    Qt 参考: m_checkstep 的各个阶段
    步骤: 0=空闲, 1=预约等待, 2=放行中, 3=检测中, 4=检测完成
    """
    socketio.emit('message', {
        'type': 'detection_step',
        'timestamp': int(time.time() * 1000),
        'data': {
            'step': step,
            'message': message,
        },
    })


def push_image_ready(image_type: str, url: str):
    """推送新图像可用通知（单张）

    image_type: body/transparent/head/tail/top/goods/evidence/license
    """
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
    socketio.emit('message', {
        'type': 'image_ready',
        'timestamp': int(time.time() * 1000),
        'data': {
            'imageType': image_type,
            'group': group,
            'urls': urls,
        },
    })


def push_lane_occupied(occupied: bool):
    """推送车道占用/异物告警

    Qt 参考: LvTongPro::onRadarMonitor() 中 monitorFlag 判定
    """
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
    socketio.emit('message', {
        'type': 'booking',
        'timestamp': int(time.time() * 1000),
        'data': {
            'action': action,
            **kwargs,
        },
    })
