"""设备管理器

对齐 Qt DeviceManager (device/devicemanager.h/.cpp)

核心原则：设备状态由实时连通性测试决定，不从数据库读取，也不写回。
数据库只存元数据（名称、IP、URL），状态纯内存维护。
"""
import threading
from app.db.device import DBDevice
from app.devices.base import DeviceBase, DeviceStatus
from app.devices.plc import PLCController
from app.devices.gate import GateController
from app.devices.led import LedController
from app.devices.radar import RadarReader
from app.devices.xray import XRayController

_DEVICE_CLASS_MAP = {
    'controller': PLCController,
    'gate': GateController,
    'led': LedController,
    'udpradar': RadarReader,
    'xray': XRayController,
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

        self._devices: dict[str, DeviceBase] = {}       # 有驱动的控制器实例
        self._db_devices: list[dict] = []                # 所有数据库设备记录（含无驱动的）
        self._db = DBDevice()

        self._health_timer: threading.Timer | None = None
        self._reconnect_timer: threading.Timer | None = None
        self._running = False

        self._ws_push_device_status = None

    # ========== 加载 & 初始化 ==========

    def load_from_db(self) -> list[dict]:
        """从数据库加载设备元数据（不读 status 列），创建控制器实例

        DB 职责：device_id, device_name, device_type, ip_address, port, config(URL等)
        状态职责：运行时通过 ping 确定
        """
        db_devices = self._db.getAllDevices()
        self._db_devices = db_devices
        result = []

        for d in db_devices:
            device_id = d['device_id']
            device_type = (d.get('device_type') or '').lower()

            cls = _DEVICE_CLASS_MAP.get(device_type)
            if cls is None:
                result.append({
                    'device_id': device_id,
                    'device_name': d['device_name'],
                    'device_type': device_type,
                    'initialized': False,
                    'reason': f'暂无 {device_type} 驱动',
                })
                continue

            ctrl = cls(
                device_id=device_id,
                device_name=d.get('device_name', ''),
                device_type=device_type,
                ip_address=d.get('ip_address', ''),
                port=d.get('port', 0),
                username=d.get('username', ''),
                password=d.get('password', ''),
                config=d.get('config', {}) if isinstance(d.get('config'), dict) else {},
            )
            ctrl.set_status_changed_callback(self._on_device_status_changed)

            self._devices[device_id] = ctrl
            result.append({
                'device_id': device_id,
                'device_name': d['device_name'],
                'device_type': device_type,
                'initialized': False,
            })

        return result

    def initialize_all(self) -> dict[str, bool]:
        """逐个初始化设备 — 真实 ping 硬件，结果即状态

        状态只在内存中，不写 DB。
        """
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

    # ========== 状态回调（纯内存 + WS 推送，不写 DB） ==========

    def _on_device_status_changed(self, device_id: str, _old: int, new: int):
        """设备状态变化时推送 WebSocket"""
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
        """获取所有设备状态

        有驱动的 → 从控制器实例获取实时状态
        无驱动的 → 从 DB 记录展示「暂无驱动」
        """
        result = []
        for d in self._db_devices:
            device_id = d['device_id']
            ctrl = self._devices.get(device_id)
            if ctrl:
                result.append({
                    'deviceId': device_id,
                    'deviceName': ctrl.device_name,
                    'deviceType': ctrl.device_type,
                    'status': {0: '离线', 1: '在线', 2: '忙碌', 3: '错误'}.get(int(ctrl.status), '未知'),
                    'statusCode': int(ctrl.status),
                    'connected': ctrl.is_online,
                    'lastError': ctrl.last_error,
                })
            else:
                dt = d['device_type']
                result.append({
                    'deviceId': device_id,
                    'deviceName': d['device_name'],
                    'deviceType': dt,
                    'status': '暂无驱动',
                    'statusCode': -1,
                    'connected': False,
                    'lastError': f'暂无 {dt} 类型驱动',
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
