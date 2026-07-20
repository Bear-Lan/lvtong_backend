"""PLC 控制器

对齐 Qt PLCModbus (device/plcmodbus.h/.cpp)
"""
import requests
from app.services.device_base import DeviceBase, DeviceStatus


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

    def _set_plc(self, redlight: bool | None = None, yellowlight: bool | None = None,
                  greenlight: bool | None = None, greatlight: bool | None = None,
                  lightgate160: bool | None = None, lightgate200: bool | None = None,
                  urgentstop: bool | None = None) -> bool:
        params = {k: v for k, v in locals().items() if v is not None and k not in ('self', 'params')}
        url = self.config.get('control_url', '')
        if not url:
            return True
        try:
            resp = requests.get(url, params=params, timeout=5)
            return resp.status_code == 200
        except requests.RequestException as e:
            self._last_error = str(e)
            return False
