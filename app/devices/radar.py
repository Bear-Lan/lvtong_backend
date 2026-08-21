"""雷达数据读取器

现场组播（默认）:
  - $VMRMC,时间,物体个数,前端位置,前端速度  — 车辆距离
  - $NTRMC,DEV,<hex>                       — 新协议开关状态（bit0 起）
  - $NTRMC,DEVICE,<hex>[,<hex>]            — 现场中间件（可两段：cmd,status）
  - $NTRMC,DEVICE_STATUS,<hex>             — 状态心跳（同 DEV 位表）
  - $NTRMC,PLC,<hex>                       — 旧版 PLC（高16 CMD / 低16 STATUS）
  - $NTRMC,XRAY160/XRAY200,<电流>,<电压>,<温度>
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
        """新协议 DEV 位表（bit0 起）→ 与前端 plc_status 兼容的字段。"""
        return {
            'protocol': 'dev',
            'raw': int(value) & 0xFFFFFFFF,
            'redLightCmd': _bit(value, 0),
            'yellowLightCmd': _bit(value, 1),
            'greenLightCmd': _bit(value, 2),
            'createLightCmd': _bit(value, 3),
            'urgentStopCmd': _bit(value, 4),
            'urgentStopStatus': _bit(value, 4),
            'bookingStatus': _bit(value, 5),
            'stickDownStatus': _bit(value, 6),
            'groundSensorStatus': _bit(value, 7),
            'lightScreenStatus': _bit(value, 8),
            'lightSource200Status': _bit(value, 9),
            'lightSource160Status': _bit(value, 10),
            'lightGate200Status': _bit(value, 11),
            'lightGate160Status': _bit(value, 12),
            'gateMotor200Status': _bit(value, 13),
            'gateMotor160Status': _bit(value, 14),
            'xrayGate200Cmd': _bit(value, 11),
            'xrayGate160Cmd': _bit(value, 12),
            'interlock160Cmd': _bit(value, 15),
            'interlock200Cmd': _bit(value, 16),
            'soundalarmCmd': _bit(value, 17),
            'xrayMotorRest200Cmd': False,
            'xrayMotorRest160Cmd': False,
        }

    @classmethod
    def parse_ntrmc_dev(cls, text: str) -> dict | None:
        """解析 $NTRMC,DEV|DEVICE|DEVICE_STATUS,<hex>[,<hex>]

        现场中间件常见：`$NTRMC,DEVICE,00000000,0003f9bf`
        （前段多为 cmd，后段为 status；UI 默认以最后一段为准）。
        """
        try:
            parts = [p.strip() for p in text.strip().split(',')]
            if len(parts) < 3:
                return None
            kind = parts[1].upper()
            if kind not in ('DEV', 'DEVICE', 'DEVICE_STATUS'):
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
            # 两段时：前段 cmd、后段 status；cmd=0 也要覆盖，避免把 status 误当成灯控
            if len(hexes) >= 2:
                parsed['cmdRaw'] = hexes[0]
                cmd_parsed = cls.parse_dev_bits(hexes[0])
                for k in (
                    'redLightCmd', 'yellowLightCmd', 'greenLightCmd',
                    'createLightCmd', 'urgentStopCmd', 'soundalarmCmd',
                    'interlock160Cmd', 'interlock200Cmd',
                    'xrayGate200Cmd', 'xrayGate160Cmd',
                ):
                    parsed[k] = cmd_parsed[k]
            return parsed
        except (ValueError, IndexError):
            return None

    @staticmethod
    def parse_plc_status(text: str) -> dict | None:
        """解析 $NTRMC,PLC,<hex> 旧协议包

        32 位寄存器：高 16 = CMD_*，低 16 = STATUS_*。
        与新协议 DEV 位布局不同，不可混用。
        """
        try:
            parts = text.strip().split(',')
            if len(parts) < 3:
                return None

            hex_str = parts[2].split('\r')[0].split('\n')[0].strip()
            status_value = int(hex_str, 16)
            cmd = status_value >> 16
            st = status_value & 0xFFFF

            return {
                'protocol': 'plc',
                'raw': status_value,
                'urgentStopCmd': (cmd & 0x0100) != 0,
                'redLightCmd': (cmd & 0x0200) != 0,
                'yellowLightCmd': (cmd & 0x0400) != 0,
                'greenLightCmd': (cmd & 0x0800) != 0,
                'createLightCmd': (cmd & 0x1000) != 0,
                'interlock200Cmd': (cmd & 0x2000) != 0,
                'interlock160Cmd': (cmd & 0x4000) != 0,
                'soundalarmCmd': (cmd & 0x8000) != 0,
                'xrayGate200Cmd': (cmd & 0x0001) != 0,
                'xrayGate160Cmd': (cmd & 0x0002) != 0,
                'xrayMotorRest200Cmd': (cmd & 0x0004) != 0,
                'xrayMotorRest160Cmd': (cmd & 0x0008) != 0,
                'urgentStopStatus': (st & 0x0100) != 0,
                'bookingStatus': (st & 0x0200) != 0,
                'stickDownStatus': (st & 0x0400) != 0,
                'groundSensorStatus': (st & 0x0800) != 0,
                'lightScreenStatus': (st & 0x1000) != 0,
                'lightGate200Status': (st & 0x2000) != 0,
                'lightGate160Status': (st & 0x4000) != 0,
                'gateMotor200Status': (st & 0x8000) != 0,
                'gateMotor160Status': (st & 0x0001) != 0,
                'lightSource200Status': (st & 0x0002) != 0,
                'lightSource160Status': (st & 0x0004) != 0,
            }
        except (ValueError, IndexError):
            return None

    @staticmethod
    def parse_xray_status(text: str) -> dict | None:
        """解析 $NTRMC,XRAY160|XRAY200,<电流>,<电压>,<温度>"""
        try:
            parts = [p.strip() for p in text.strip().split(',')]
            if len(parts) < 5:
                return None
            kind = parts[1].upper()
            if kind == 'XRAY160':
                xray_type = '160'
            elif kind == 'XRAY200':
                xray_type = '200'
            else:
                return None
            return {
                'type': xray_type,
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

        if text.startswith('$NTRMC,PLC,'):
            parsed = self.parse_plc_status(text)
            if parsed is not None:
                self._emit_plc(parsed)
            return

        upper = text.upper()
        if (
            upper.startswith('$NTRMC,DEV,')
            or upper.startswith('$NTRMC,DEVICE,')
            or upper.startswith('$NTRMC,DEVICE_STATUS,')
        ):
            # DEVICE 会匹配 DEVICE_STATUS 前缀？DEVICE_STATUS 以 DEVICE_ 开头
            # 上面三条分别判断；DEVICE, 与 DEVICE_STATUS, 不冲突
            parsed = self.parse_ntrmc_dev(text)
            if parsed is not None:
                self._emit_plc(parsed)
            return

        if upper.startswith('$NTRMC,XRAY160,') or upper.startswith('$NTRMC,XRAY200,'):
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
