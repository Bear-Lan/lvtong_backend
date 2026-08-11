"""雷达数据读取器

现场组播（默认）:
  - $VMRMC,时间,物体个数,前端位置,前端速度  — 车辆距离
  - $NTRMC,PLC,<hex>                       — PLC 状态
  - $NTRMC,XRAY160/XRAY200/DM200,...       — 预留

兼容旧版单播 JSON:
  - { "distance": float, "mode": int }
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading

from app.devices.base import DeviceBase, DeviceStatus


def _guess_mcast_iface(preferred: str = '') -> str:
    """选择组播入组网卡。优先显式配置，否则选通往播发源的路由网卡。

    注意：若本机开了 VPN，默认路由可能把探测指到 10.x 等虚拟网卡，
    组播会加错口导致收不到 $VMRMC（小车不跑）。故优先与播发源同网段的地址。
    """
    if preferred:
        return preferred

    # (优先级, ip)：同网段优先，再按探测顺序
    ranked: list[tuple[int, str]] = []
    # 播发源在 192.168.0.188（现场实测）
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
        # 惩罚常见 VPN/隧道网段，避免抢走现场局域网
        vpn_like = 1 if ip.startswith(('10.', '172.16.', '172.17.', '172.18.', '172.19.',
                                       '172.2', '172.3', '100.')) else 0
        ranked.append((same_subnet + vpn_like, ip))

    if not ranked:
        return '0.0.0.0'
    ranked.sort(key=lambda x: (x[0], x[1]))
    return ranked[0][1]


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
        """注册 PLC 状态回调 cb(parsed: dict)"""
        self._plc_status_callback = cb

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
            # 组播端口：只用 mcast_port / 环境默认 9000，不用 DB 残留的旧 unicast port
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
    def parse_plc_status(text: str) -> dict | None:
        """解析 $NTRMC,PLC,<hex> 协议包"""
        try:
            parts = text.strip().split(',')
            if len(parts) < 3:
                return None

            hex_str = parts[2].split('\r')[0].split('\n')[0].strip()
            status_value = int(hex_str, 16)
            s16status_value = status_value >> 16

            return {
                'redLightCmd': (s16status_value & 0x0200) != 0,
                'yellowLightCmd': (s16status_value & 0x0400) != 0,
                'greenLightCmd': (s16status_value & 0x0800) != 0,
                'createLightCmd': (s16status_value & 0x1000) != 0,
                'soundalarmCmd': (s16status_value & 0x8000) != 0,
                'interlock160Cmd': (s16status_value & 0x4000) != 0,
                'interlock200Cmd': (s16status_value & 0x2000) != 0,
                'urgentStopStatus': (status_value & 0x0100) != 0,
                'bookingStatus': (status_value & 0x0200) != 0,
                'groundSensorStatus': (status_value & 0x0800) != 0,
                'lightScreenStatus': (status_value & 0x1000) != 0,
                'lightGate200Status': (status_value & 0x2000) != 0,
                'lightGate160Status': (status_value & 0x4000) != 0,
                'lightSource200Status': (status_value & 0x0002) != 0,
                'lightSource160Status': (status_value & 0x0004) != 0,
            }
        except (ValueError, IndexError):
            return None

    def _dispatch(self, text: str) -> None:
        if text.startswith('$VMRMC'):
            parsed = self.parse_vmrmc(text)
            if not parsed:
                return
            self._distance = parsed['distance']
            self._speed = parsed['speed']
            self._object_count = parsed['object_count']
            # mode: 有目标=1，无目标=0（兼容旧 JSON mode 语义）
            self._mode = 1 if self._object_count > 0 else 0
            if self._distance_callback:
                self._distance_callback(self._distance, self._mode)
            return

        if text.startswith('$NTRMC,PLC,'):
            if self._plc_status_callback:
                parsed = self.parse_plc_status(text)
                if parsed is not None:
                    self._plc_status_callback(parsed)
            return

        if text.startswith('{'):
            try:
                payload = json.loads(text)
                self._distance = float(payload.get('distance', 0))
                self._mode = int(payload.get('mode', 1))
                if self._distance_callback:
                    self._distance_callback(self._distance, self._mode)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            return

        # $NTRMC,XRAY200 / XRAY160 / DM200 预留

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
