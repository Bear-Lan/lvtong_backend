"""LED 显示屏 — 组播 $MDRMC leddisplay=

协议码（与中间件一致）:
  LTCL_YYJC     绿通车辆，预约检测          ← step1
  SQYJS_QYDDD   申请已接收，请原地等待      ← step2
  SQYQX_QTX     申请已取消，请通行
  JCYSL_QDDFX   检测已受理，请等待放行      ← step3
  DJCL_QTX      待检车辆，请通行            ← step4
  JCZ_QWKJ      检测中…请勿靠近             ← step5/6
  JCWC_QTX      检测完成，请通行            ← step7

业务仍用 set_step1…7；内部转 leddisplay=。
"""
from __future__ import annotations

from app.devices.base import DeviceBase, DeviceStatus
from app.devices.mcast_cmd import send_mdrmc

# Qt/业务 step → 协议码
STEP_TO_LEDDISPLAY = {
    1: 'LTCL_YYJC',
    2: 'SQYJS_QYDDD',
    3: 'JCYSL_QDDFX',
    4: 'DJCL_QTX',
    5: 'JCZ_QWKJ',
    6: 'JCZ_QWKJ',
    7: 'JCWC_QTX',
}

STEP_MSG = {
    1: '绿通车辆 按键检测',
    2: '已接收 请原地等待',
    3: '已受理 请等待放行',
    4: '待检车辆 请通行',
    5: '检测中 请勿靠近',
    6: '检测中...请勿靠近',
    7: '检测完成 请通行',
}

# 具名码（驳回等可直接 set_display）
LEDDISPLAY_CODES = frozenset(STEP_TO_LEDDISPLAY.values()) | {
    'SQYQX_QTX',
}


class LedController(DeviceBase):
    """LED 显示屏 — $MDRMC,1,leddisplay=…"""

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
        code = STEP_TO_LEDDISPLAY.get(int(step))
        if not code:
            self._last_error = f'未知 LED step={step}'
            return False
        return self.set_display(code)

    def set_display(self, code: str) -> bool:
        code = (code or '').strip()
        if not code:
            return False
        ok = send_mdrmc(f'leddisplay={code}')
        if not ok:
            self._last_error = f'MDRMC leddisplay={code} 发送失败'
            self.status = DeviceStatus.Offline
            return False
        self.status = DeviceStatus.Online
        self._last_error = ''
        print(f'[LED:{self.device_id}] MDRMC leddisplay={code}', flush=True)
        return True

    def shutdown(self):
        pass
