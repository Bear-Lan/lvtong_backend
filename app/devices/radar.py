"""雷达数据读取器

现场组播（默认）:
  - $VMRMC,时间,物体个数,前端位置,前端速度  — 车辆距离
  - $NTRMC,DEVICE,<cmdHex>,<statusHex>     — 开关状态（ST_* bit0 起；两段 cmd,status）
  - $NTRMC,XRAY160/XRAY200,<电流>,<电压>,<温度>
  - $NTRMC,XRSUP/XRSDOWN,<电流>,<电压>,<温度>  — 现场新代号（上/下光机，等同 200/160）
  - $NTRMC,DM200,<个数>,<温度…>

兼容旧版单播 JSON:
  - { "distance": float, "mode": int }
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
from typing import Any, Callable

from app.devices.base import DeviceBase, DeviceStatus


def _guess_mcast_iface(preferred: str = '') -> str:
    """选择组播入组网卡。优先显式配置，否则选通往播发源的路由网卡。

    注意：若本机开了 VPN，默认路由可能把探测指到 10.x 等虚拟网卡，
    组播会加错口导致收不到 $VMRMC（小车不跑）。故优先与播发源同网段的地址。
    """
    if preferred:
        return preferred

    ranked: list[tuple[int, str]] = []
    for probe in ('192.168.0.188', '192.168.88.201'):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 1))
            ip = s.getsockname()[0]
            s.close()
        except OSError:
            continue
        if not ip or ip.startswith('127.'):
            continue
        probe_prefix = probe.rsplit('.', 1)[0]
        ip_prefix = ip.rsplit('.', 1)[0]
        same_subnet = 0 if ip_prefix == probe_prefix else 1
        vpn_like = 1 if ip.startswith(('10.', '172.16.', '172.17.', '172.18.', '172.19.',
                                       '172.2', '172.3', '100.')) else 0
        ranked.append((same_subnet + vpn_like, ip))

    if not ranked:
        return '0.0.0.0'
    ranked.sort(key=lambda x: (x[0], x[1]))
    return ranked[0][1]


def _bit(value: int, n: int) -> bool:
    return (value & (1 << n)) != 0


class RadarReader(DeviceBase):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._distance = 0.0
        self._mode = 1
        self._speed = 0.0
        self._object_count = 0
        self._running = False
        self._thread = None
        self._sock: socket.socket | None = None
        self._distance_callback = None
        self._plc_status_callback = None
        self._xray_callback: Callable[[str, float, float, float], Any] | None = None
        self._dm_callback: Callable[[list[float]], Any] | None = None
        self._last_plc_sig: tuple | None = None
        self._transport = 'multicast'
        self._iface_ip = '0.0.0.0'

    @property
    def distance(self) -> float:
        return self._distance

    @property
    def speed(self) -> float:
        return self._speed

    def set_distance_callback(self, cb):
        self._distance_callback = cb

    def set_plc_status_callback(self, cb):
        """注册 PLC/DEV 状态回调 cb(parsed: dict)"""
        self._plc_status_callback = cb

    def set_xray_status_callback(self, cb):
        """cb(xray_type: '160'|'200', current_ma, voltage_kv, temperature)"""
        self._xray_callback = cb

    def set_dm_temp_callback(self, cb):
        """cb(temps: list[float])"""
        self._dm_callback = cb

    def initialize(self) -> bool:
        cfg = self.config or {}
        try:
            from config import (
                RADAR_TRANSPORT,
                MCAST_GROUP,
                MCAST_PORT,
                MCAST_IFACE_IP,
            )
        except ImportError:
            RADAR_TRANSPORT, MCAST_GROUP, MCAST_PORT, MCAST_IFACE_IP = (
                'multicast', '224.0.0.100', 9000, ''
            )

        self._transport = (
            cfg.get('transport')
            or os.environ.get('RADAR_TRANSPORT')
            or RADAR_TRANSPORT
            or 'multicast'
        ).lower()

        if self._transport == 'unicast':
            udp_port = int(cfg.get('port') or self.port or 0)
            if not udp_port:
                self.status = DeviceStatus.Online
                return True
            ok = self._start_unicast_listen(udp_port)
        else:
            group = cfg.get('mcast_group') or MCAST_GROUP
            port = int(
                cfg.get('mcast_port')
                or os.environ.get('MCAST_PORT')
                or MCAST_PORT
                or 9000
            )
            iface = cfg.get('iface_ip') or MCAST_IFACE_IP or ''
            self._iface_ip = _guess_mcast_iface(iface)
            ok = self._start_multicast_listen(group, port, self._iface_ip)
            if ok:
                print(
                    f'[雷达] {self.device_id}: 组播监听 {group}:{port} '
                    f'iface={self._iface_ip}',
                    flush=True,
                )

        self.status = DeviceStatus.Online if ok else DeviceStatus.Offline
        if not ok:
            self._last_error = '雷达监听启动失败'
        return ok

    def health_check(self) -> bool:
        return self._running

    def reconnect(self) -> bool:
        self.shutdown()
        return self.initialize()

    def stop(self):
        self._running = False
        sock = self._sock
        self._sock = None
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def shutdown(self):
        self.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    # ---- 解析 ----

    @staticmethod
    def parse_vmrmc(text: str) -> dict | None:
        """解析 $VMRMC,时间,物体个数,前端位置,前端速度"""
        try:
            parts = text.strip().split(',')
            if len(parts) < 5 or not parts[0].startswith('$VMRMC'):
                return None
            return {
                'timestamp': float(parts[1]),
                'object_count': int(float(parts[2])),
                'distance': float(parts[3]),
                'speed': float(parts[4]),
            }
        except (ValueError, IndexError):
            return None

    @staticmethod
    def parse_dev_bits(value: int) -> dict:
        """新协议 DEVICE 位表（bit0 起，与中间件 ST_* 一致）→ plc_status。

        ST_RED_LIGHT=0 … ST_SOUND_ALARM=17；不做写死占位。
        """
        return {
            'protocol': 'dev',
            'raw': int(value) & 0xFFFFFFFF,
            # 0–3 灯
            'redLightCmd': _bit(value, 0),
            'yellowLightCmd': _bit(value, 1),
            'greenLightCmd': _bit(value, 2),
            'createLightCmd': _bit(value, 3),  # ST_FILL_LIGHT
            # 4–8
            'urgentStopCmd': _bit(value, 4),
            'urgentStopStatus': _bit(value, 4),  # ST_URGENT_STOP
            'bookingStatus': _bit(value, 5),  # ST_APPOINTMENT
            'stickDownStatus': _bit(value, 6),  # ST_BOOM_BARRIER
            'groundSensorStatus': _bit(value, 7),  # ST_LOOP_DETECTOR
            'lightScreenStatus': _bit(value, 8),  # ST_LIGHT_CURTAIN
            # 9–12 光源 / 光闸
            'lightSource200Status': _bit(value, 9),
            'lightSource160Status': _bit(value, 10),
            'lightGate200Status': _bit(value, 11),
            'lightGate160Status': _bit(value, 12),
            'xrayGate200Cmd': _bit(value, 11),  # ST_XRAY_GATE_200
            'xrayGate160Cmd': _bit(value, 12),  # ST_XRAY_GATE_160
            # 13–14 伺服（面板「伺服复位」与只读「伺服状态」同源）
            'gateMotor200Status': _bit(value, 13),  # ST_XRAY_MOTOR_200
            'gateMotor160Status': _bit(value, 14),  # ST_XRAY_MOTOR_160
            'xrayMotorRest200Cmd': _bit(value, 13),
            'xrayMotorRest160Cmd': _bit(value, 14),
            # 15–17
            'interlock160Cmd': _bit(value, 15),
            'interlock200Cmd': _bit(value, 16),
            'soundalarmCmd': _bit(value, 17),
        }

    @classmethod
    def parse_ntrmc_dev(cls, text: str) -> dict | None:
        """解析 $NTRMC,DEVICE,<cmdHex>,<statusHex>

        例：`$NTRMC,DEVICE,00000110,0000ffff`
        （前段 cmd，后段 status；status 低 16 位为 0xFFFF 时只信 cmd）。
        """
        try:
            parts = [p.strip() for p in text.strip().split(',')]
            if len(parts) < 3:
                return None
            kind = parts[1].upper()
            if kind != 'DEVICE':
                return None
            hexes: list[int] = []
            for raw in parts[2:]:
                h = raw.split('\r')[0].split('\n')[0].strip()
                if not h:
                    continue
                hexes.append(int(h, 16))
            if not hexes:
                return None
            status_bits = hexes[-1]
            parsed = cls.parse_dev_bits(status_bits)
            parsed['ntrmcKind'] = kind
            # 两段时：前段 cmd、后段 status
            if len(hexes) >= 2:
                cmd_val = hexes[0]
                parsed['cmdRaw'] = cmd_val
                cmd_parsed = cls.parse_dev_bits(cmd_val)
                # 现场中间件常把 status 打成 0xFFFF（无效），若仍按 status 解：
                # bit4/bit5 全 1 → 误触发急停弹窗、预约播报「申请已接收…」
                if (status_bits & 0xFFFF) == 0xFFFF:
                    parsed = cls.parse_dev_bits(cmd_val)
                    parsed['ntrmcKind'] = kind
                    parsed['cmdRaw'] = cmd_val
                    parsed['raw'] = status_bits
                    parsed['statusUnreliable'] = True
                else:
                    for k in (
                        'redLightCmd', 'yellowLightCmd', 'greenLightCmd',
                        'createLightCmd', 'urgentStopCmd', 'urgentStopStatus',
                        'bookingStatus', 'soundalarmCmd',
                        'interlock160Cmd', 'interlock200Cmd',
                        'xrayGate200Cmd', 'xrayGate160Cmd',
                        'xrayMotorRest200Cmd', 'xrayMotorRest160Cmd',
                        'gateMotor200Status', 'gateMotor160Status',
                        # 栏杆在 cmd bit6（如 6040↔6000）
                        'stickDownStatus',
                    ):
                        parsed[k] = cmd_parsed[k]
            return parsed
        except (ValueError, IndexError):
            return None

    @staticmethod
    def parse_xray_status(text: str) -> dict | None:
        """解析 X 光遥测。

        兼容：
          $NTRMC,XRAY160|XRAY200,<电流>,<电压>,<温度>
          $NTRMC,XRSUP|XRSDOWN,<电流>,<电压>,<温度>   # 现场中间件新代号
        映射：XRSUP→200（上光机），XRSDOWN→160（下光机）
        """
        try:
            parts = [p.strip() for p in text.strip().split(',')]
            if len(parts) < 5:
                return None
            kind = parts[1].upper()
            if kind in ('XRAY160', 'XRSDOWN'):
                xray_type = '160'
            elif kind in ('XRAY200', 'XRSUP'):
                xray_type = '200'
            else:
                return None
            return {
                'type': xray_type,
                'kind': kind,
                'current': float(parts[2]),
                'voltage': float(parts[3]),
                'temperature': float(parts[4]),
            }
        except (ValueError, IndexError):
            return None

    @staticmethod
    def parse_dm200(text: str) -> dict | None:
        """解析 $NTRMC,DM200,<个数>,<温度…>"""
        try:
            parts = [p.strip() for p in text.strip().split(',')]
            if len(parts) < 3 or parts[1].upper() != 'DM200':
                return None
            count = int(float(parts[2]))
            temps = [float(x) for x in parts[3:3 + max(0, count)]]
            if not temps and len(parts) > 3:
                temps = [float(x) for x in parts[3:]]
            return {'count': count, 'temperatures': temps}
        except (ValueError, IndexError):
            return None

    def _emit_plc(self, parsed: dict) -> None:
        if not self._plc_status_callback or parsed is None:
            return
        # DEVICE 心跳很密：相同寄存器不重复推 WS
        sig = (parsed.get('protocol'), parsed.get('raw'), parsed.get('cmdRaw'))
        if sig == self._last_plc_sig:
            return
        first = self._last_plc_sig is None
        self._last_plc_sig = sig
        if first:
            print(
                f"[RADAR] PLC/DEVICE 状态首包 protocol={parsed.get('protocol')} "
                f"raw=0x{int(parsed.get('raw') or 0):08x} "
                f"cmdRaw={parsed.get('cmdRaw')}"
            )
        self._plc_status_callback(parsed)

    def _dispatch(self, text: str) -> None:
        if text.startswith('$VMRMC'):
            parsed = self.parse_vmrmc(text)
            if not parsed:
                return
            self._distance = parsed['distance']
            self._speed = parsed['speed']
            self._object_count = parsed['object_count']
            # 物体个数>0 即有车 → mode=1（兼容旧 JSON mode）
            self._mode = 1 if self._object_count > 0 else 0
            if self._distance_callback:
                self._distance_callback(self._distance, self._mode, self._object_count)
            return

        upper = text.upper()
        if upper.startswith('$NTRMC,DEVICE,'):
            parsed = self.parse_ntrmc_dev(text)
            if parsed is not None:
                self._emit_plc(parsed)
            return

        if (
            upper.startswith('$NTRMC,XRAY160,')
            or upper.startswith('$NTRMC,XRAY200,')
            or upper.startswith('$NTRMC,XRSUP,')
            or upper.startswith('$NTRMC,XRSDOWN,')
        ):
            parsed = self.parse_xray_status(text)
            if parsed and self._xray_callback:
                self._xray_callback(
                    parsed['type'],
                    parsed['current'],
                    parsed['voltage'],
                    parsed['temperature'],
                )
            return

        if upper.startswith('$NTRMC,DM200,'):
            parsed = self.parse_dm200(text)
            if parsed and self._dm_callback:
                self._dm_callback(parsed['temperatures'])
            return

        if text.startswith('{'):
            try:
                payload = json.loads(text)
                self._distance = float(payload.get('distance', 0))
                self._mode = int(payload.get('mode', 1))
                intrusion = None
                if 'monitorFlag' in payload:
                    mf = int(payload.get('monitorFlag', 0))
                    intrusion = mf >= 1
                    self._object_count = 2 if intrusion else (1 if self._mode > 0 else 0)
                elif 'object_count' in payload or 'objectCount' in payload:
                    self._object_count = int(
                        payload.get('object_count', payload.get('objectCount', 0))
                    )
                else:
                    self._object_count = 1 if self._mode > 0 else 0
                if self._distance_callback:
                    # 兼容 (d, mode) / (d, mode, count) / 关键字 intrusion
                    try:
                        self._distance_callback(
                            self._distance,
                            self._mode,
                            self._object_count,
                            intrusion=intrusion,
                        )
                    except TypeError:
                        self._distance_callback(
                            self._distance, self._mode, self._object_count,
                        )
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            return

    # ---- 监听 ----

    def _start_multicast_listen(self, group: str, port: int, iface_ip: str) -> bool:
        if self._running:
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(iface_ip),
                )
            except OSError:
                pass
            sock.bind(('', port))
            mreq = struct.pack(
                '4s4s',
                socket.inet_aton(group),
                socket.inet_aton(iface_ip),
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(1.0)
        except OSError as e:
            self._last_error = str(e)
            print(f'[雷达] {self.device_id}: 组播启动失败: {e}', flush=True)
            return False

        self._sock = sock
        self._running = True
        self._thread = threading.Thread(
            target=self._listen_loop, args=(sock,), daemon=True, name=f'radar-mcast-{self.device_id}'
        )
        self._thread.start()
        return True

    def _start_unicast_listen(self, port: int) -> bool:
        if self._running:
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            sock.bind(('0.0.0.0', port))
        except OSError as e:
            self._last_error = str(e)
            print(f'[雷达] {self.device_id}: 单播启动失败: {e}', flush=True)
            return False

        self._sock = sock
        self._running = True
        self._thread = threading.Thread(
            target=self._listen_loop, args=(sock,), daemon=True, name=f'radar-udp-{self.device_id}'
        )
        self._thread.start()
        print(f'[雷达] {self.device_id}: 单播监听 0.0.0.0:{port}', flush=True)
        return True

    def _listen_loop(self, sock: socket.socket) -> None:
        while self._running:
            try:
                data, _ = sock.recvfrom(4096)
                if not data:
                    continue
                text = data.decode('utf-8', errors='ignore').strip()
                if text:
                    self._dispatch(text)
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    break
            except Exception as e:
                print(f'[雷达] {self.device_id}: 收包异常: {e}', flush=True)
                break
        self._running = False
