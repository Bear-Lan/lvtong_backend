"""栏杆机控制器 — pyserial 直连串口

对齐 Qt Gate (device/gate.cpp)
串口协议: ASCII 文本指令
  开闸: "alb open\n"
  关闸: "alb close\n"
  心跳: " heart beat\n" (每 300ms)
  状态回报: "status 1 2 1" (关) / "status 1 4 1" (开)
"""
import threading
import serial
from app.devices.base import DeviceBase, DeviceStatus


class GateController(DeviceBase):

    HEARTBEAT_INTERVAL = 0.3   # 300ms 心跳

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._serial: serial.Serial | None = None
        self._is_open_state = False
        self._heartbeat_thread: threading.Thread | None = None
        self._running = False

    # ========== 初始化 ==========

    def initialize(self) -> bool:
        """打开串口连接（对齐 Qt Gate::connectDevice）"""
        try:
            port = self.ip_address or self.config.get('port_name', 'COM7')
            baudrate = self.config.get('baud_rate', 9600)

            self._serial = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
            )
            self._serial.reset_input_buffer()

            # 启动心跳 + 状态监听
            self._running = True
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True
            )
            self._heartbeat_thread.start()

            self.status = DeviceStatus.Online
            return True

        except serial.SerialException as e:
            self._last_error = f'串口打开失败: {e}'
            self.status = DeviceStatus.Offline
            return False

    def health_check(self) -> bool:
        """检查串口是否还开着"""
        return self._serial is not None and self._serial.is_open

    def reconnect(self) -> bool:
        self.shutdown()
        return self.initialize()

    def execute_action(self, action: str, params: dict | None = None) -> bool:
        if action == 'open':
            return self._send_command(b'alb open\n')
        elif action == 'close':
            return self._send_command(b'alb close\n')
        return False

    def shutdown(self):
        self._running = False
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None

    # ========== 内部 ==========

    def _send_command(self, data: bytes) -> bool:
        if not self._serial or not self._serial.is_open:
            return False
        try:
            self._serial.write(data)
            self._serial.flush()
            return True
        except serial.SerialException:
            self.status = DeviceStatus.Offline
            return False

    def _heartbeat_loop(self):
        """每 300ms 发心跳 + 检查串口状态回传"""
        while self._running:
            try:
                if self._serial and self._serial.is_open:
                    # 发心跳
                    self._serial.write(b' heart beat\n')
                    self._serial.flush()

                    # 检查是否有状态回传
                    if self._serial.in_waiting:
                        line = self._serial.readline().decode('utf-8', errors='ignore').strip()
                        if 'status 1 2 1' in line and self._is_open_state:
                            # 关状态但记录是开 → 发开闸
                            self._serial.write(b'alb open\n')
                        elif 'status 1 4 1' in line and not self._is_open_state:
                            # 开状态但记录是关 → 发关闸
                            self._serial.write(b'alb close\n')

            except (serial.SerialException, OSError):
                self.status = DeviceStatus.Offline
            except Exception:
                pass

            threading.Event().wait(self.HEARTBEAT_INTERVAL)
