"""栏杆机控制器

现场中间件 HTTP（优先）:
  GET {GATE_CONTROL_URL}?boombarrier=true|false
  true=抬杆，false=落杆
  返回例: {"message":"success","status":"true"}
  注意：对方 Content-Length 常多报，需容忍 IncompleteRead。

未配置 GATE_CONTROL_URL 时回退串口（旧 Qt 协议）:
  开闸: "alb open\\n" / 关闸: "alb close\\n"
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
from urllib.parse import urlencode, urlparse

import serial

from app.devices.base import DeviceBase, DeviceStatus


class GateController(DeviceBase):

    HEARTBEAT_INTERVAL = 0.3  # 串口模式心跳

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._serial: serial.Serial | None = None
        self._is_open_state = False
        self._heartbeat_thread: threading.Thread | None = None
        self._running = False
        self._control_url = self._resolve_control_url()

    def _resolve_control_url(self) -> str:
        from config import GATE_CONTROL_URL

        url = (self.config.get('control_url') or GATE_CONTROL_URL or '').strip()
        return url.rstrip('?&')

    @property
    def use_http(self) -> bool:
        return bool(self._control_url)

    # ========== 初始化 ==========

    def initialize(self) -> bool:
        if self.use_http:
            return self._init_http()
        return self._init_serial()

    def health_check(self) -> bool:
        if self.use_http:
            return self._tcp_reachable()
        return self._serial is not None and self._serial.is_open

    def reconnect(self) -> bool:
        self.shutdown()
        self._control_url = self._resolve_control_url()
        return self.initialize()

    @property
    def is_opened(self) -> bool:
        return bool(self._is_open_state)

    def execute_action(self, action: str, params: dict | None = None) -> bool:
        if action == 'toggle':
            # 对齐 Qt GateUIController::onIconClicked：开↔关切换
            return self.execute_action('close' if self._is_open_state else 'open', params)
        if action == 'open':
            ok = self._set_boom(True)
            if ok:
                self._is_open_state = True
            return ok
        if action == 'close':
            ok = self._set_boom(False)
            if ok:
                self._is_open_state = False
            return ok
        return False

    def shutdown(self):
        self._running = False
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None

    # ========== HTTP ==========

    def _init_http(self) -> bool:
        if self._tcp_reachable():
            self.status = DeviceStatus.Online
            self._last_error = ''
            print(f'[Gate:{self.device_id}] HTTP 栏杆 {self._control_url}', flush=True)
            return True
        self._last_error = f'栏杆中间件不可达: {self._control_url}'
        self.status = DeviceStatus.Offline
        print(f'[Gate:{self.device_id}] {self._last_error}', flush=True)
        return False

    def _tcp_reachable(self) -> bool:
        try:
            u = urlparse(self._control_url)
            host = u.hostname
            if not host:
                return False
            port = u.port or (443 if u.scheme == 'https' else 80)
            with socket.create_connection((host, port), timeout=2.0):
                return True
        except OSError as e:
            self._last_error = str(e)
            return False

    def _set_boom(self, raised: bool) -> bool:
        if self.use_http:
            return self._http_set_boom(raised)
        cmd = b'alb open\n' if raised else b'alb close\n'
        return self._send_command(cmd)

    def _http_set_boom(self, raised: bool) -> bool:
        """GET ?boombarrier=true|false；兼容 Content-Length 多报。"""
        u = urlparse(self._control_url)
        if not u.hostname:
            self._last_error = 'GATE_CONTROL_URL 无效'
            return False

        path = u.path or '/dev/control'
        qs = urlencode({'boombarrier': 'true' if raised else 'false'})
        if u.query:
            path = f'{path}?{u.query}&{qs}'
        else:
            path = f'{path}?{qs}'

        port = u.port or (443 if u.scheme == 'https' else 80)
        conn: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        try:
            if u.scheme == 'https':
                conn = http.client.HTTPSConnection(u.hostname, port, timeout=5)
            else:
                conn = http.client.HTTPConnection(u.hostname, port, timeout=5)
            conn.request('GET', path)
            resp = conn.getresponse()
            try:
                body = resp.read()
            except http.client.IncompleteRead as e:
                body = e.partial or b''
            text = body.decode('utf-8', errors='replace').strip()
            code = resp.status
        except Exception as e:
            self._last_error = str(e)
            self.status = DeviceStatus.Offline
            print(f'[Gate:{self.device_id}] HTTP 控制失败: {e}', flush=True)
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        ok = False
        try:
            data = json.loads(text) if text else {}
            msg = str(data.get('message', '')).lower()
            st = str(data.get('status', '')).lower()
            ok = code == 200 and (msg == 'success' or st in ('true', '1', 'ok'))
        except json.JSONDecodeError:
            ok = code == 200 and 'success' in text.lower()

        if ok:
            self.status = DeviceStatus.Online
            self._last_error = ''
            print(
                f'[Gate:{self.device_id}] boombarrier={"true" if raised else "false"} → {text}',
                flush=True,
            )
        else:
            self._last_error = f'HTTP {code} body={text[:120]}'
            print(f'[Gate:{self.device_id}] 控制未成功: {self._last_error}', flush=True)
        return ok

    # ========== 串口回退 ==========

    def _init_serial(self) -> bool:
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
                write_timeout=0.5,
            )
            self._serial.reset_input_buffer()

            self._running = True
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True
            )
            self._heartbeat_thread.start()

            self.status = DeviceStatus.Online
            print(f'[Gate:{self.device_id}] 串口模式 {port}', flush=True)
            return True

        except serial.SerialException as e:
            self._last_error = f'串口打开失败: {e}'
            self.status = DeviceStatus.Offline
            return False

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
        """每 300ms 发心跳 + 检查串口状态回传（仅串口模式）"""
        fail_count = 0
        while self._running:
            try:
                if self._serial and self._serial.is_open:
                    self._serial.write(b' heart beat\n')
                    self._serial.flush()

                    if self._serial.in_waiting:
                        line = self._serial.readline().decode('utf-8', errors='ignore').strip()
                        if 'status 1 2 1' in line and self._is_open_state:
                            self._serial.write(b'alb open\n')
                        elif 'status 1 4 1' in line and not self._is_open_state:
                            self._serial.write(b'alb close\n')

                    fail_count = 0

            except (serial.SerialException, OSError):
                self.status = DeviceStatus.Offline
                fail_count += 1
                print(f'[Gate:{self.device_id}] 串口通信失败 ({fail_count})')
                if fail_count >= 5:
                    print(f'[Gate:{self.device_id}] 连续失败 {fail_count} 次，停止心跳')
                    self._running = False
                    try:
                        if self._serial and self._serial.is_open:
                            self._serial.close()
                    except Exception:
                        pass
                    self._serial = None
                    break
            except Exception:
                pass

            threading.Event().wait(self.HEARTBEAT_INTERVAL)
