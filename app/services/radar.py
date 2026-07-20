"""雷达数据读取器

UDP 雷达距离数据读取，同时解析 X 光温度数据。
参考 Qt UDPRadar (device/udpradar.h)。

UDP 数据包格式（来自 Qt 实现）：
- 距离数据：从 UDP 端口监听，JSON 格式 { distance: float, mode: int }
- X 光温度：通过 PLC 相关 UDP 端口获取
"""
import socket
import json
import threading


class RadarReader:
    """UDP 雷达数据读取器"""

    def __init__(self, host: str = '0.0.0.0', port: int = 6000):
        self.host = host
        self.port = port
        self._distance = 0.0
        self._mode = 1
        self._running = False
        self._thread: threading.Thread | None = None
        self._callback = None

    @property
    def distance(self) -> float:
        return self._distance

    def set_callback(self, callback):
        """设置距离更新回调 callback(distance, mode)"""
        self._callback = callback

    def start(self):
        """启动 UDP 监听"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        print(f'[RADAR] UDP 监听启动 {self.host}:{self.port}')

    def stop(self):
        """停止 UDP 监听"""
        self._running = False

    def _listen(self):
        """UDP 监听线程"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        try:
            sock.bind((self.host, self.port))
        except OSError as e:
            print(f'[RADAR] 绑定失败: {e}')
            sock.close()
            return

        while self._running:
            try:
                data, addr = sock.recvfrom(4096)
                if data:
                    self._parse_packet(data)
            except socket.timeout:
                continue
            except Exception as e:
                print(f'[RADAR] 接收异常: {e}')
                break

        sock.close()

    def _parse_packet(self, data: bytes):
        """解析 UDP 数据包"""
        try:
            payload = json.loads(data.decode('utf-8'))
            distance = float(payload.get('distance', 0))
            mode = int(payload.get('mode', 1))

            self._distance = distance
            self._mode = mode

            if self._callback:
                self._callback(distance, mode)
        except (json.JSONDecodeError, ValueError) as e:
            print(f'[RADAR] 数据解析失败: {e}')
