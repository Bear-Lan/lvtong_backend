"""雷达数据读取器

对齐 Qt UDPRadar (device/udpradar.h/.cpp)

UDP 数据包支持两种协议（与 Qt 一致，同一端口复用）：
  - JSON: { distance: float, mode: int }          — 雷达距离
  - 文本: $NTRMC,PLC,<hex>\r\n                     — PLC 状态（含急停标志）
  - 文本: $NTRMC,XRAY200,<hex>\r\n 等              — X光状态（预留）
"""
import socket
import json
import threading
from app.services.device_base import DeviceBase, DeviceStatus


class RadarReader(DeviceBase):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._distance = 0.0
        self._mode = 1
        self._running = False
        self._thread = None
        self._distance_callback = None
        self._plc_status_callback = None

    @property
    def distance(self) -> float:
        return self._distance

    def set_distance_callback(self, cb):
        self._distance_callback = cb

    def set_plc_status_callback(self, cb):
        """注册 PLC 状态回调 cb(text: str)

        对齐 Qt UDPRadar::actionCompleted("plc_status", ...)
        当收到 $NTRMC,PLC,<hex> 包时触发。
        """
        self._plc_status_callback = cb

    def initialize(self) -> bool:
        udp_port = self.config.get('port', self.port)
        if not udp_port:
            self.status = DeviceStatus.Online
            return True
        self._start_listen(udp_port)
        self.status = DeviceStatus.Online
        return True

    def health_check(self) -> bool:
        return self._running

    def reconnect(self) -> bool:
        if not self._running:
            return self.initialize()
        return True

    def _start_listen(self, port: int):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._listen, args=(port,), daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    @staticmethod
    def parse_plc_status(text: str) -> dict | None:
        """解析 $NTRMC,PLC,<hex> 协议包

        对齐 Qt UDPRadar::processPLCData() (udpradar.cpp:256-348)

        包格式: $NTRMC,PLC,<32-bit hex>\r\n

        返回 dict 包含所有 PLC 状态字段，解析失败返回 None。
        """
        try:
            # 提取 hex 值: $NTRMC,PLC,<hex>\r\n
            parts = text.strip().split(',')
            if len(parts) < 3:
                return None

            hex_str = parts[2].split('\r')[0].split('\n')[0].strip()
            status_value = int(hex_str, 16)
            s16status_value = status_value >> 16  # 高 16 位 = 命令位

            return {
                # 命令位（高 16 位移位后）— 对齐 Qt s16statusValue 位掩码
                'redLightCmd': (s16status_value & 0x0200) != 0,
                'yellowLightCmd': (s16status_value & 0x0400) != 0,
                'greenLightCmd': (s16status_value & 0x0800) != 0,
                'createLightCmd': (s16status_value & 0x1000) != 0,
                'soundalarmCmd': (s16status_value & 0x8000) != 0,
                'interlock160Cmd': (s16status_value & 0x4000) != 0,
                'interlock200Cmd': (s16status_value & 0x2000) != 0,
                # 状态位（完整 32 位）— 对齐 Qt statusValue 位掩码
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

    def _listen(self, port: int):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        try:
            sock.bind(('0.0.0.0', port))
        except OSError:
            self._running = False
            sock.close()
            return
        while self._running:
            try:
                data, _ = sock.recvfrom(4096)
                if not data:
                    continue
                text = data.decode('utf-8', errors='ignore').strip()
                if not text:
                    continue

                # 协议分发（对齐 Qt UDPRadar::processData()）
                if text.startswith('{'):
                    # JSON: 雷达距离
                    payload = json.loads(text)
                    self._distance = float(payload.get('distance', 0))
                    self._mode = int(payload.get('mode', 1))
                    if self._distance_callback:
                        self._distance_callback(self._distance, self._mode)

                elif text.startswith('$NTRMC,PLC,'):
                    # 文本协议: PLC 状态（对齐 Qt processPLCData）
                    if self._plc_status_callback:
                        parsed = self.parse_plc_status(text)
                        if parsed is not None:
                            self._plc_status_callback(parsed)

                # $NTRMC,XRAY200 / $NTRMC,XRAY160 / $NTRMC,DM200 预留
                # 对齐 Qt processXray200Data / processXray160Data / processXrayTemperature

            except socket.timeout:
                continue
            except Exception:
                break
        sock.close()

    def shutdown(self):
        self.stop()
