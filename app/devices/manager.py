"""设备管理器

对齐 Qt DeviceManager (device/devicemanager.h/.cpp)

- 摄像头：只从 DB devices 表读（IP/账号/RTSP）
- PLC / 道闸 / LED / 雷达 / 光机：代码内固定创建实例，**不读库、也不在 devices.py 列清单**
  - 控制：组播 $MDRMC / HTTP 启扫（URL 见 config.devices）
  - 状态：组播 $NTRMC（DEVICE / XRAY / …），不靠逐台探活当权威
"""
import threading

from app.db.device import DBDevice
from app.devices.base import DeviceBase, DeviceStatus
from app.devices.plc import PLCController
from app.devices.gate import GateController
from app.devices.led import LedController
from app.devices.radar import RadarReader
from app.devices.xray import XRayController

# DB 中视为摄像头的类型
_CAMERA_TYPES = frozenset({'camera', 'hikvision', 'hik'})


def _wire_control_controllers() -> dict[str, DeviceBase]:
    """固定接线：业务 execute_device_action / 组播监听需要的实例。

    不做成可配置设备表；状态以组播为准。
    """
    return {
        'plc_001': PLCController(
            device_id='plc_001',
            device_name='PLC',
            device_type='controller',
        ),
        'gate_001': GateController(
            device_id='gate_001',
            device_name='道闸',
            device_type='gate',
        ),
        'led_001': LedController(
            device_id='led_001',
            device_name='LED',
            device_type='led',
        ),
        # 组播收距离 + DEVICE/XRAY 状态
        'radar_001': RadarReader(
            device_id='radar_001',
            device_name='车道雷达',
            device_type='udpradar',
        ),
        # 启扫 HTTP（URL 在 config.devices；scheduler 用 xray_200）
        'xray_200': XRayController(
            device_id='xray_200',
            device_name='X光启扫',
            device_type='xray',
        ),
    }


class DeviceManager:
    """设备管理器（单例）"""

    _instance = None
    _lock = threading.Lock()

    HEALTH_CHECK_INTERVAL = 20
    RECONNECT_INTERVAL = 5

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._initialized = False
                    cls._instance = obj
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._devices: dict[str, DeviceBase] = {}
        self._db_devices: list[dict] = []  # 仅摄像头
        self._db = DBDevice()

        self._health_timer: threading.Timer | None = None
        self._reconnect_timer: threading.Timer | None = None
        self._running = False

        self._ws_push_device_status = None

    # ========== 加载 & 初始化 ==========

    def load_devices(self) -> list[dict]:
        """代码固定控制实例 + DB 摄像头。"""
        result: list[dict] = []
        self._devices.clear()

        for device_id, ctrl in _wire_control_controllers().items():
            ctrl.set_status_changed_callback(self._on_device_status_changed)
            self._devices[device_id] = ctrl
            result.append({
                'device_id': device_id,
                'device_name': ctrl.device_name,
                'device_type': ctrl.device_type,
                'initialized': False,
                'source': 'code',
            })

        cameras: list[dict] = []
        try:
            for d in self._db.getAllDevices():
                dt = (d.get('device_type') or '').lower()
                did = str(d.get('device_id') or '')
                if dt in _CAMERA_TYPES or dt.startswith('cam') or did.startswith('cam_'):
                    cameras.append(d)
        except Exception as e:
            print(f'[设备] 读摄像头 DB 失败: {e}', flush=True)

        self._db_devices = cameras
        for d in cameras:
            result.append({
                'device_id': d['device_id'],
                'device_name': d.get('device_name', ''),
                'device_type': d.get('device_type', 'camera'),
                'initialized': False,
                'source': 'db',
                'reason': '摄像头无 DeviceManager 驱动（抓图/MediaMTX 直读 DB）',
            })

        return result

    def load_from_db(self) -> list[dict]:
        """兼容旧启动入口 → load_devices()"""
        return self.load_devices()

    def initialize_all(self) -> dict[str, bool]:
        """启动监听/发送通道；业务状态仍以组播推送为准。"""
        result = {}
        for device_id, ctrl in self._devices.items():
            try:
                ok = ctrl.initialize()
                result[device_id] = ok
            except Exception as e:
                result[device_id] = False
                ctrl._last_error = str(e)
                ctrl.status = DeviceStatus.Error
        return result

    # ========== 定时健康检查（对齐 Qt 20s） ==========

    def start_health_check(self):
        if self._running:
            return
        self._running = True
        self._schedule_health_check()

    def stop_health_check(self):
        self._running = False
        if self._health_timer:
            self._health_timer.cancel()
        if self._reconnect_timer:
            self._reconnect_timer.cancel()

    def _schedule_health_check(self):
        if not self._running:
            return
        self._health_timer = threading.Timer(
            self.HEALTH_CHECK_INTERVAL, self._perform_health_check
        )
        self._health_timer.daemon = True
        self._health_timer.start()

    def _perform_health_check(self):
        has_offline = False
        for ctrl in list(self._devices.values()):
            try:
                ok = ctrl.health_check()
                if not ok and ctrl.status == DeviceStatus.Online:
                    ctrl.status = DeviceStatus.Offline
                elif ok and ctrl.status == DeviceStatus.Offline:
                    ctrl.status = DeviceStatus.Online
                if ctrl.status in (DeviceStatus.Offline, DeviceStatus.Error):
                    has_offline = True
            except Exception:
                ctrl.status = DeviceStatus.Error
                has_offline = True

        if has_offline:
            self._start_reconnect_timer()
        else:
            self._stop_reconnect_timer()

        self._schedule_health_check()

    # ========== 自动重连（对齐 Qt 5s） ==========

    def _start_reconnect_timer(self):
        if self._reconnect_timer is not None:
            return
        self._schedule_reconnect()

    def _stop_reconnect_timer(self):
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None

    def _schedule_reconnect(self):
        if not self._running:
            return
        self._reconnect_timer = threading.Timer(
            self.RECONNECT_INTERVAL, self._attempt_reconnect
        )
        self._reconnect_timer.daemon = True
        self._reconnect_timer.start()

    def _attempt_reconnect(self):
        self._reconnect_timer = None
        for ctrl in list(self._devices.values()):
            if ctrl.status in (DeviceStatus.Offline, DeviceStatus.Error):
                try:
                    if ctrl.reconnect():
                        ctrl.status = DeviceStatus.Online
                except Exception:
                    pass

        still_offline = any(
            c.status in (DeviceStatus.Offline, DeviceStatus.Error)
            for c in self._devices.values()
        )
        if still_offline:
            self._schedule_reconnect()

    # ========== 手动重连 ==========

    def manual_reconnect(self) -> list[str]:
        reconnected = []
        for device_id, ctrl in self._devices.items():
            if ctrl.status in (DeviceStatus.Offline, DeviceStatus.Error):
                try:
                    if ctrl.reconnect():
                        ctrl.status = DeviceStatus.Online
                        reconnected.append(device_id)
                except Exception:
                    pass
        return reconnected

    # ========== 状态回调 ==========

    def _on_device_status_changed(self, device_id: str, _old: int, new: int):
        if self._ws_push_device_status:
            status_text = {0: '离线', 1: '在线', 2: '忙碌', 3: '错误'}.get(new, '未知')
            self._ws_push_device_status(device_id, new, status_text)

    def set_ws_push(self, push_func):
        self._ws_push_device_status = push_func

    # ========== 状态查询 ==========

    def get_device(self, device_id: str) -> DeviceBase | None:
        return self._devices.get(device_id)

    def get_devices_by_type(self, device_type: str) -> list[DeviceBase]:
        return [c for c in self._devices.values()
                if c.device_type == device_type.lower()]

    def get_all_status(self) -> list[dict]:
        """控制实例（接线用）+ DB 摄像头。开关量权威在组播 plc_status。"""
        result = []
        for device_id, ctrl in self._devices.items():
            item = {
                'deviceId': device_id,
                'deviceName': ctrl.device_name,
                'deviceType': ctrl.device_type,
                'status': {0: '离线', 1: '在线', 2: '忙碌', 3: '错误'}.get(int(ctrl.status), '未知'),
                'statusCode': int(ctrl.status),
                'connected': ctrl.is_online,
                'lastError': ctrl.last_error,
                'source': 'code',
            }
            if device_id == 'gate_001' or ctrl.device_type == 'gate':
                item['gateOpen'] = bool(getattr(ctrl, 'is_opened', False))
            result.append(item)

        for d in self._db_devices:
            result.append({
                'deviceId': d['device_id'],
                'deviceName': d.get('device_name', ''),
                'deviceType': d.get('device_type', 'camera'),
                'status': '摄像头',
                'statusCode': -1,
                'connected': False,
                'lastError': '',
                'source': 'db',
            })
        return result

    def online_count(self) -> int:
        return sum(1 for c in self._devices.values() if c.is_online)

    def offline_count(self) -> int:
        return len(self._devices) - self.online_count()

    def health_check_all(self) -> bool:
        for ctrl in self._devices.values():
            if not ctrl.is_online:
                return False
        return True

    def is_gate_offline(self) -> bool:
        for device_id, ctrl in self._devices.items():
            if device_id == 'gate_001' and not ctrl.is_online:
                return True
        return False

    def shutdown(self):
        self.stop_health_check()
        for ctrl in self._devices.values():
            try:
                ctrl.shutdown()
            except Exception:
                pass
