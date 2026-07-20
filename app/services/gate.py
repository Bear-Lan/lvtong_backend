"""栏杆机控制器

通过 HTTP 控制串口栏杆机。
参考 Qt Gate (device/gate.h)。
"""
import requests


class GateController:
    """栏杆机 HTTP 接口封装"""

    def __init__(self, base_url: str = ''):
        self.base_url = base_url
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    def open_gate(self) -> bool:
        if not self.base_url:
            print('[GATE] 模拟 openGate')
            self._open = True
            return True
        try:
            resp = requests.post(f'{self.base_url}/open', timeout=5)
            if resp.status_code == 200:
                self._open = True
                return True
        except requests.RequestException as e:
            print(f'[GATE] openGate 失败: {e}')
        return False

    def close_gate(self) -> bool:
        if not self.base_url:
            print('[GATE] 模拟 closeGate')
            self._open = False
            return True
        try:
            resp = requests.post(f'{self.base_url}/close', timeout=5)
            if resp.status_code == 200:
                self._open = False
                return True
        except requests.RequestException as e:
            print(f'[GATE] closeGate 失败: {e}')
        return False
