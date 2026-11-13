"""LED 前后屏经 MDRMC 新协议显示，编号依据用户提供的 LED 表。

DISPLAY1 按键检测、2 已接收等待、3 已受理等待、4 请通行、5 检测中勿跟车、
6 系统运维暂停使用。业务 step6 原为检测中，仍用 DISPLAY5；完成用4，驳回回到1。
"""
from __future__ import annotations

import re
import threading

from app.devices.base import DeviceBase, DeviceStatus
from app.devices.mcast_cmd import send_mdrmc

STEP_TO_LEDDISPLAY = {
    1: 'DISPLAY1', 2: 'DISPLAY2', 3: 'DISPLAY3', 4: 'DISPLAY4',
    5: 'DISPLAY5', 6: 'DISPLAY5', 7: 'DISPLAY4',
}
STEP_MSG = {
    1: '绿通车辆 按键检测', 2: '申请已接收 请原地等待',
    3: '申请已受理 请等待放行', 4: '待检车辆 请通行',
    5: '检测中 请勿跟车', 6: '检测中 请勿跟车', 7: '待检车辆 请通行',
}
# 兼容业务调用点，但从此只发送新协议，不向中间件发送旧码。
_LEGACY_CODES = {
    'LTCL_YYJC': 'DISPLAY1', 'SQYJS_QYDDD': 'DISPLAY2',
    'JCYSL_QDDFX': 'DISPLAY3', 'DJCL_QTX': 'DISPLAY4',
    'JCZ_QWKJ': 'DISPLAY5', 'JCWC_QTX': 'DISPLAY4', 'SQYQX_QTX': 'DISPLAY4',
}
LEDDISPLAY_CODES = frozenset(f'DISPLAY{n}' for n in range(1, 10))
_send_lock = threading.Lock()


class LedController(DeviceBase):
    """分别发送 frontleddisplay 和 backleddisplay，发送成功不等于屏显回执。"""
    STEP_MSG = STEP_MSG

    def initialize(self) -> bool:
        self.status = DeviceStatus.Online
        self._last_error = ''
        return True

    def health_check(self) -> bool:
        return True

    def reconnect(self) -> bool:
        return self.initialize()

    def execute_action(self, action: str, params: dict | None = None) -> bool:
        params = params or {}
        if action.startswith('set_step'):
            try:
                step = int(action.replace('set_step', ''))
            except ValueError:
                return False
            return self.set_step(step)
        if action in ('set_display', 'leddisplay'):
            code = str(params.get('code') or params.get('leddisplay') or '').strip()
            return self.set_display(code)
        return False

    def set_step(self, step: int) -> bool:
        from app.services.system_config import value
        code = value('led.steps', {}).get(str(step), STEP_TO_LEDDISPLAY.get(step))
        if not code:
            self._last_error = f'未知 LED step={step}'
            return False
        return self.set_display(code)

    def set_display(self, code: str) -> bool:
        code = (code or '').strip()
        code = _LEGACY_CODES.get(code, code)
        if not re.fullmatch(r'DISPLAY[1-9]', code):
            self._last_error = 'LED 编号必须为 DISPLAY1～DISPLAY9'
            return False
        failures = []
        with _send_lock:
            for side in ('front', 'back'):
                command = f'{side}leddisplay={code}'
                if not send_mdrmc(command):
                    failures.append(command)
                print(f'[LED:{self.device_id}] MDRMC {command} udp_sent={command not in failures}', flush=True)
        if failures:
            self._last_error = 'MDRMC 发送失败: ' + ', '.join(failures)
            self.status = DeviceStatus.Offline
            return False
        self.status = DeviceStatus.Online
        self._last_error = ''
        return True

    def shutdown(self):
        pass
