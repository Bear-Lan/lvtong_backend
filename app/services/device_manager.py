"""设备管理器

统一管理所有硬件设备的状态与操作，与 Qt DeviceManager 架构保持一致。

设备类型对应关系：
- Camera     → 海康威视摄像头
- GPCamera   → 高拍仪
- Gate       → 串口栏杆机
- Controller → PLC 控制器 (Modbus over HTTP)
- UDPRadar   → 雷达测距 (UDP)
- Led        → LED 显示屏 (串口)
- TTSVoice   → 语音合成 (TCP)
- CodeReader → 扫码枪

当前架构下设备通过 HTTP 中间层通信，URL 从 config/app.json 读取。
"""
import threading
from app.db.device import DBDevice
from ws.handler import push_device_status


class DeviceManager:
    """设备管理器单例"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._devices: dict[str, dict] = {}

    # ---- 设备注册 ----

    def load_devices_from_db(self):
        """从数据库加载设备列表"""
        db = DBDevice()
        devices = db.getAllDevices()
        for d in devices:
            self._devices[d['device_id']] = d

    def get_device(self, device_id: str) -> dict | None:
        return self._devices.get(device_id)

    def get_devices_by_type(self, device_type: str) -> list[dict]:
        return [d for d in self._devices.values()
                if d.get('device_type') == device_type]

    def get_all_devices(self) -> list[dict]:
        return list(self._devices.values())

    # ---- 状态管理 ----

    def get_all_device_status(self) -> list[dict]:
        result = []
        for d in self._devices.values():
            status_text = {0: '离线', 1: '在线', 2: '忙碌', 3: '错误'}.get(
                d.get('status', 0), '未知')
            result.append({
                'deviceId': d['device_id'],
                'deviceName': d['device_name'],
                'deviceType': d['device_type'],
                'status': status_text,
                'connected': d.get('status') == 1,
            })
        return result

    def update_device_status(self, device_id: str, status_code: int):
        """更新设备状态并推送通知"""
        db = DBDevice()
        db.updateDeviceStatus(device_id, status_code)
        if device_id in self._devices:
            self._devices[device_id]['status'] = status_code
        push_device_status(device_id, status_code)

    def online_device_count(self) -> int:
        return sum(1 for d in self._devices.values() if d.get('status') == 1)

    def offline_device_count(self) -> int:
        return sum(1 for d in self._devices.values() if d.get('status') == 0)

    # ---- 健康检查 ----

    def health_check(self) -> bool:
        """健康检查：所有关键设备在线"""
        for d in self._devices.values():
            if d.get('status') != 1:
                return False
        return True

    def is_gate_device_offline(self) -> bool:
        """检查主栏杆机是否离线"""
        gates = self.get_devices_by_type('Gate')
        return any(g.get('status') != 1 for g in gates if '001' in g.get('device_id', ''))

    def attempt_reconnect(self):
        """尝试重连所有离线设备"""
        for device_id, d in self._devices.items():
            if d.get('status') != 1:
                # TODO: 通过 HTTP 中间层触发设备重连
                print(f'[DEVICE] 尝试重连设备: {device_id}')
