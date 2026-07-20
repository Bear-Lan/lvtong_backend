"""雷达数据读取器

对齐 Qt UDPRadar (device/udpradar.h/.cpp)
UDP 数据包: JSON { distance: float, mode: int }
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

    @property
    def distance(self) -> float:
        return self._distance

    def set_distance_callback(self, cb):
        self._distance_callback = cb

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
                if data and self._distance_callback:
                    payload = json.loads(data.decode('utf-8'))
                    self._distance = float(payload.get('distance', 0))
                    self._mode = int(payload.get('mode', 1))
                    self._distance_callback(self._distance, self._mode)
            except socket.timeout:
                continue
            except Exception:
                break
        sock.close()

    def shutdown(self):
        self.stop()
