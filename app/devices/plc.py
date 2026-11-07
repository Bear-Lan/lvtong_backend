"""PLC 控制器 — 组播 $MDRMC

协议例：
  $MDRMC,1,redlight=true
  $MDRMC,3,redlight=true,greenlight=false,leddisplay=LTCL_YYJC
"""
from __future__ import annotations

from app.devices.base import DeviceBase, DeviceStatus
from app.devices.mcast_cmd import send_mdrmc

# 协议布尔开关
_BOOL_KEYS = frozenset({
    'urgentstop',
    'redlight', 'yellowlight', 'greenlight',
    'filllight',
    'interlock200', 'interlock160',
    'soundalarm',
    'xraygate200', 'xraygate160',
    'xraymotorreset200', 'xraymotorreset160',
    'xraysource160', 'xraysource200',
    'boombarrier',
})


def _bool_token(v) -> str | None:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)) and v in (0, 1):
        return 'true' if int(v) else 'false'
    s = str(v).strip().lower()
    if s in ('true', '1', 'on', 'yes'):
        return 'true'
    if s in ('false', '0', 'off', 'no'):
        return 'false'
    return None


def params_to_mdrmc_cmds(params: dict) -> list[str]:
    """把 setPLC 参数字典转成 MDRMC 命令片段列表（键名须与协议一致）。"""
    cmds: list[str] = []
    for raw_key, raw_val in (params or {}).items():
        if raw_val is None:
            continue
        key = str(raw_key).strip().lower()
        if not key:
            continue
        if key == 'leddisplay':
            code = str(raw_val).strip()
            if code:
                cmds.append(f'leddisplay={code}')
            continue
        if key not in _BOOL_KEYS:
            continue
        tok = _bool_token(raw_val)
        if tok is None:
            continue
        if key == 'urgentstop' and tok == 'true':
            # 急停只停X光采集，不通过旧整机急停键联动其他设备。
            cmds.append('xraycap=false')
            continue
        cmds.append(f'{key}={tok}')
    return cmds


class PLCController(DeviceBase):

    def initialize(self) -> bool:
        self.status = DeviceStatus.Online
        self._last_error = ''
        return True

    def health_check(self) -> bool:
        return True

    def reconnect(self) -> bool:
        return self.initialize()

    def execute_action(self, action: str, params: dict | None = None) -> bool:
        if action == 'setPLC':
            return self._set_plc(**(params or {}))
        return False

    def _set_plc(self, **kwargs) -> bool:
        kwargs = {str(key).strip().lower(): value for key, value in kwargs.items()}
        cmds = params_to_mdrmc_cmds(kwargs)
        if not cmds:
            return True
        ok = send_mdrmc(*cmds)
        if not ok:
            self._last_error = 'MDRMC PLC 发送失败'
            self.status = DeviceStatus.Offline
            return False
        self.status = DeviceStatus.Online
        self._last_error = ''
        return True
