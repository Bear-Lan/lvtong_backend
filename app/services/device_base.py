"""设备基类

模仿 Qt DeviceBase 的接口模式。
每个设备必须实现: initialize(), health_check(), reconnect()
"""
from abc import ABC, abstractmethod
from enum import IntEnum


class DeviceStatus(IntEnum):
    Offline = 0
    Online = 1
    Busy = 2
    Error = 3


class DeviceBase(ABC):
    """设备抽象基类"""

    def __init__(self, device_id: str, device_name: str = '',
                 device_type: str = '', ip_address: str = '',
                 port: int = 0, username: str = '', password: str = '',
                 config: dict | None = None):
        self.device_id = device_id
        self.device_name = device_name
        self.device_type = device_type
        self.ip_address = ip_address
        self.port = port
        self.username = username
        self.password = password
        self.config = config or {}
        self._status = DeviceStatus.Offline
        self._last_error = ''
        self._status_changed_cb = None

    @property
    def status(self) -> DeviceStatus:
        return self._status

    @status.setter
    def status(self, value: DeviceStatus):
        old = self._status
        self._status = value
        if old != value and self._status_changed_cb:
            try:
                self._status_changed_cb(self.device_id, int(old), int(value))
            except Exception:
                pass

    @property
    def is_online(self) -> bool:
        return self._status == DeviceStatus.Online

    @property
    def last_error(self) -> str:
        return self._last_error

    def set_status_changed_callback(self, cb):
        """注入回调 cb(device_id, old_status, new_status)"""
        self._status_changed_cb = cb

    # ---- 子类必须实现 ----
    @abstractmethod
    def initialize(self) -> bool:
        ...
    @abstractmethod
    def health_check(self) -> bool:
        ...
    @abstractmethod
    def reconnect(self) -> bool:
        ...

    def execute_action(self, action: str, params: dict | None = None) -> bool:
        return False

    def shutdown(self):
        pass
