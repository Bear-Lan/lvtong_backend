"""栏杆机控制器 — 组播 $MDRMC

  $MDRMC,1,boombarrier=true   # 抬杆
  $MDRMC,1,boombarrier=false  # 落杆

状态确认：短等 $NTRMC,DEVICE cmd bit6（1=抬起，0=落下）。
"""
from __future__ import annotations

import socket
import time

from app.devices.base import DeviceBase, DeviceStatus


class GateController(DeviceBase):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._is_open_state = False
        self._last_boom_result: dict = {}

    @property
    def last_boom_result(self) -> dict:
        return dict(self._last_boom_result)

    @property
    def is_opened(self) -> bool:
        return bool(self._is_open_state)

    def initialize(self) -> bool:
        if self._mdrmc_iface_ok():
            self.status = DeviceStatus.Online
            self._last_error = ''
            print(f'[Gate:{self.device_id}] MDRMC 栏杆 → 224.0.0.100:9000', flush=True)
            return True
        self._last_error = '组播网卡不可用，无法发 MDRMC'
        self.status = DeviceStatus.Offline
        print(f'[Gate:{self.device_id}] {self._last_error}', flush=True)
        return False

    def health_check(self) -> bool:
        return self._mdrmc_iface_ok()

    def reconnect(self) -> bool:
        self.shutdown()
        return self.initialize()

    def shutdown(self):
        pass

    def execute_action(self, action: str, params: dict | None = None) -> bool:
        if action == 'toggle':
            return self.execute_action('close' if self._is_open_state else 'open', params)
        if action == 'open':
            ok = self._mdrmc_set_boom(True)
            if ok:
                self._is_open_state = True
            return ok
        if action == 'close':
            ok = self._mdrmc_set_boom(False)
            if ok:
                self._is_open_state = False
            return ok
        return False

    def sync_open_state(self, opened: bool) -> None:
        """用组播 DEVICE bit6 同步本机记忆（供主页图标 / toggle）。"""
        self._is_open_state = bool(opened)

    def _mdrmc_iface_ok(self) -> bool:
        from config import MCAST_IFACE_IP
        from app.devices.radar import _guess_mcast_iface

        iface = _guess_mcast_iface(MCAST_IFACE_IP or '')
        if not iface or iface == '0.0.0.0':
            return True
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((iface, 0))
            s.close()
            return True
        except OSError as e:
            self._last_error = str(e)
            return False

    def _mdrmc_set_boom(self, raised: bool) -> bool:
        from app.devices.mcast_cmd import send_mdrmc
        from app.websocket.handler import get_last_plc_status

        cmd = f'boombarrier={"true" if raised else "false"}'
        packet = f'$MDRMC,1,{cmd}'
        ok = send_mdrmc(cmd)
        result: dict = {
            'transport': 'mdrmc',
            'sent': bool(ok),
            'packet': packet,
            'wantedOpen': bool(raised),
            'confirmed': False,
            'stickdown': None,
            'cmdRaw': None,
            'waitMs': 2500,
            'note': 'UDP 组播无 ACK；confirmed=2.5s 内 DEVICE cmd bit6 达目标',
        }
        if not ok:
            self._last_error = 'MDRMC boombarrier 发送失败'
            self.status = DeviceStatus.Offline
            self._last_boom_result = result
            return False

        self.status = DeviceStatus.Online
        self._last_error = ''
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            plc = get_last_plc_status() or {}
            if 'stickdown' in plc:
                sd = bool(plc.get('stickdown'))
                result['stickdown'] = sd
                result['cmdRaw'] = plc.get('cmdRaw')
                if sd == bool(raised):
                    result['confirmed'] = True
                    break
            time.sleep(0.1)

        self._last_boom_result = result
        if result['confirmed']:
            print(
                f'[Gate:{self.device_id}] MDRMC 已确认 bit6={int(bool(raised))} '
                f'({"抬起" if raised else "落下"})',
                flush=True,
            )
        else:
            print(
                f'[Gate:{self.device_id}] MDRMC 已发送但 2.5s 内未确认 DEVICE bit6 '
                f'(want={"1抬起" if raised else "0落下"} stickdown={result.get("stickdown")})',
                flush=True,
            )
        return True
