"""PLC 控制器

对齐 Qt PLCModbus (device/plcmodbus.h/.cpp)
"""
import requests
from app.devices.base import DeviceBase, DeviceStatus


class PLCController(DeviceBase):

    def initialize(self) -> bool:
        ok = self.health_check()
        self.status = DeviceStatus.Online if ok else DeviceStatus.Offline
        if not ok:
            self._last_error = 'PLC 无法连接'
        return ok

    def health_check(self) -> bool:
        url = self.config.get('status_url', '')
        if not url:
            return True
        try:
            resp = requests.get(url, timeout=5)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def reconnect(self) -> bool:
        return self.initialize()

    def execute_action(self, action: str, params: dict | None = None) -> bool:
        if action == 'setPLC':
            return self._set_plc(**(params or {}))
        return False

    def _set_plc(self, **kwargs) -> bool:
        """下发 PLC 控制指令

        支持所有前端开关参数，直接转发到硬件中间层：
        - 红/黄/绿灯: redlight, yellowlight, greenlight
        - 补光灯: createlight / greatlight
        - 光闸: lightgate160, lightgate200
        - 急停: urgentstop
        - InterLock / 伺服复位 / 声音报警 等
        """
        params = {k: v for k, v in kwargs.items() if v is not None}
        if not params:
            return True
        url = self.config.get('control_url', '')
        if not url:
            return True
        try:
            resp = requests.get(url, params=params, timeout=5)
            return resp.status_code == 200
        except requests.RequestException as e:
            self._last_error = str(e)
            return False
