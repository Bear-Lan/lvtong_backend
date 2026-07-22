"""X 光机控制器

对齐 Qt X 光机 HTTP 接口
"""
import requests
from app.devices.base import DeviceBase, DeviceStatus


class XRayController(DeviceBase):

    def initialize(self) -> bool:
        ok = self.health_check()
        self.status = DeviceStatus.Online if ok else DeviceStatus.Offline
        if not ok:
            self._last_error = 'X光机无法连接'
        return ok

    def health_check(self) -> bool:
        url = self.config.get('health_url', '')
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
        if action == 'start_scan':
            return self.start_scan(
                params.get('channel', '200'),
                params.get('car_length', 5.0),
                params.get('scan_id', '')
            )
        return False

    def start_scan(self, channel: str, car_length: float, scan_id: str = '') -> bool:
        key = f'start_{channel}_url'
        url = self.config.get(key, '')
        if not url:
            return True
        full_url = f'{url}{car_length}'
        if scan_id:
            full_url += f'&id={scan_id}'
        try:
            resp = requests.get(full_url, timeout=10)
            return resp.status_code == 200
        except requests.RequestException as e:
            self._last_error = str(e)
            return False

    def get_image_url(self, channel: str) -> str:
        return self.config.get(f'image_{channel}_url', '')
