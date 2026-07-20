"""LED 显示屏控制器 — pyserial 直连串口

对齐 Qt Led (device/led.cpp)
串口协议: 二进制 hex 指令（21 字节/条）

步骤指令（从 Qt Led::createOpenCommand 直接移植）:
  1: FE 5C 4B 89 15 00 00 00 66 92 79 95 72 02 00 00 00 01 FE FF FF
  2: FE 5C 4B 89 15 00 00 00 66 92 79 95 72 02 00 00 00 02 FD FF FF
  ...
  7: FE 5C 4B 89 15 00 00 00 66 92 79 95 72 02 00 00 00 07 F8 FF FF
"""
import serial
from app.services.device_base import DeviceBase, DeviceStatus


class LedController(DeviceBase):
    """LED 显示屏 — 串口直连"""

    # 每步对应的 21 字节 hex 指令（从 Qt Led::createOpenCommand 逐字节移植）
    STEPS = {
        1: bytes([0xFE, 0x5C, 0x4B, 0x89, 0x15, 0x00, 0x00, 0x00, 0x66, 0x92,
                  0x79, 0x95, 0x72, 0x02, 0x00, 0x00, 0x00, 0x01, 0xFE, 0xFF, 0xFF]),
        2: bytes([0xFE, 0x5C, 0x4B, 0x89, 0x15, 0x00, 0x00, 0x00, 0x66, 0x92,
                  0x79, 0x95, 0x72, 0x02, 0x00, 0x00, 0x00, 0x02, 0xFD, 0xFF, 0xFF]),
        3: bytes([0xFE, 0x5C, 0x4B, 0x89, 0x15, 0x00, 0x00, 0x00, 0x66, 0x92,
                  0x79, 0x95, 0x72, 0x02, 0x00, 0x00, 0x00, 0x03, 0xFC, 0xFF, 0xFF]),
        4: bytes([0xFE, 0x5C, 0x4B, 0x89, 0x15, 0x00, 0x00, 0x00, 0x66, 0x92,
                  0x79, 0x95, 0x72, 0x02, 0x00, 0x00, 0x00, 0x04, 0xFB, 0xFF, 0xFF]),
        5: bytes([0xFE, 0x5C, 0x4B, 0x89, 0x15, 0x00, 0x00, 0x00, 0x66, 0x92,
                  0x79, 0x95, 0x72, 0x02, 0x00, 0x00, 0x00, 0x05, 0xFA, 0xFF, 0xFF]),
        6: bytes([0xFE, 0x5C, 0x4B, 0x89, 0x15, 0x00, 0x00, 0x00, 0x66, 0x92,
                  0x79, 0x95, 0x72, 0x02, 0x00, 0x00, 0x00, 0x06, 0xF9, 0xFF, 0xFF]),
        7: bytes([0xFE, 0x5C, 0x4B, 0x89, 0x15, 0x00, 0x00, 0x00, 0x66, 0x92,
                  0x79, 0x95, 0x72, 0x02, 0x00, 0x00, 0x00, 0x07, 0xF8, 0xFF, 0xFF]),
    }

    STEP_MSG = {
        1: '绿通车辆 按键检测',
        2: '已接收 请原地等待',
        3: '已受理 请等待放行',
        4: '待检车辆 请通行 勿停车倒车',
        5: '检测中 请勿跟车',
        6: '检测中...请勿靠近',
        7: '检测完成 请通行',
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._serial: serial.Serial | None = None

    # ========== 初始化 ==========

    def initialize(self) -> bool:
        try:
            port = self.ip_address or self.config.get('port_name', 'COM3')
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

            self.status = DeviceStatus.Online
            return True

        except serial.SerialException as e:
            self._last_error = f'LED串口打开失败: {e}'
            self.status = DeviceStatus.Offline
            return False

    def health_check(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def reconnect(self) -> bool:
        self.shutdown()
        return self.initialize()

    def execute_action(self, action: str, params: dict | None = None) -> bool:
        if action.startswith('set_step'):
            step = int(action.replace('set_step', ''))
            return self.set_step(step)
        return False

    def set_step(self, step: int) -> bool:
        """发送步骤指令到 LED 屏"""
        data = self.STEPS.get(step)
        if data is None:
            return False
        if not self._serial or not self._serial.is_open:
            return False
        try:
            self._serial.write(data)
            self._serial.flush()
            return True
        except serial.SerialException:
            self.status = DeviceStatus.Offline
            return False

    def shutdown(self):
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None
