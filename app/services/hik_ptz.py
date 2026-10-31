"""海康球机 PTZ 控制。

视频预览与云台控制相互独立：Pad 端视频走 MediaMTX/WHEP，云台则由后端
通过摄像机 ISAPI 控制，因此浏览器不需要安装海康插件。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import requests
from requests.auth import HTTPDigestAuth

from app.db.device import DBDevice


PTZ_DEVICE_ID = 'cam_ptz'
WATCHDOG_SECONDS = 4.0


class PtzError(RuntimeError):
    """可安全返回给前端的 PTZ 错误。"""


@dataclass(frozen=True)
class _Camera:
    base_url: str
    username: str
    password: str
    channel_id: int
    verify_tls: bool


_DIRECTIONS = {
    'up': (0, 1, 0),
    'down': (0, -1, 0),
    'left': (-1, 0, 0),
    'right': (1, 0, 0),
    'up-left': (-1, 1, 0),
    'down-left': (-1, -1, 0),
    'up-right': (1, 1, 0),
    'down-right': (1, -1, 0),
    'zoom-in': (0, 0, 1),
    'zoom-out': (0, 0, -1),
}


class HikPtzService:
    def __init__(self):
        # 串行下发，保证“按下”请求即使较慢，也不会晚于后续“松手停止”生效。
        self._command_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._watchdogs: dict[str, threading.Timer] = {}
        self._generations: dict[str, int] = {}

    def continuous(self, device_id: str, action: str, direction: str, speed: int) -> dict:
        self._validate_device_id(device_id)
        if action not in ('start', 'stop'):
            raise PtzError('action 须为 start 或 stop')
        if direction not in (*_DIRECTIONS.keys(), 'auto-pan'):
            raise PtzError('不支持的云台方向')
        if not 1 <= speed <= 100:
            raise PtzError('speed 须在 1-100 之间')

        camera = self._load_camera(device_id)
        with self._command_lock:
            if action == 'stop':
                self._cancel_watchdog(device_id)
                self._send_stop(camera, direction)
            else:
                self._send_start(camera, direction, speed)
                self._arm_watchdog(device_id, camera, direction)
        return {
            'deviceId': device_id,
            'action': action,
            'direction': direction,
            'speed': speed,
        }

    def preset(self, device_id: str, action: str, preset_id: int) -> dict:
        self._validate_device_id(device_id)
        if action not in ('set', 'goto'):
            raise PtzError('action 须为 set 或 goto')
        if not 1 <= preset_id <= 9:
            raise PtzError('预置位仅支持 1-9')

        camera = self._load_camera(device_id)
        base = f'/ISAPI/PTZCtrl/channels/{camera.channel_id}/presets/{preset_id}'
        with self._command_lock:
            if action == 'set':
                body = (
                    "<?xml version='1.0' encoding='UTF-8'?>"
                    f'<PTZPreset><id>{preset_id}</id></PTZPreset>'
                )
                self._put(camera, base, body)
            else:
                self._put(camera, f'{base}/goto', '')
        return {'deviceId': device_id, 'action': action, 'preset': preset_id}

    @staticmethod
    def _validate_device_id(device_id: str):
        # 该接口只允许操作 Pad 球机，避免任意 device_id 被当作网络目标。
        if device_id != PTZ_DEVICE_ID:
            raise PtzError(f'不允许控制该设备: {device_id}')

    @staticmethod
    def _load_camera(device_id: str) -> _Camera:
        row = DBDevice().getDevice(device_id)
        if not row:
            raise PtzError(f'设备不存在: {device_id}')
        cfg = row.get('config') if isinstance(row.get('config'), dict) else {}
        ip = str(row.get('ip_address') or '').strip()
        if not ip:
            raise PtzError(f'设备 {device_id} 未配置 IP')
        # PTZ 可单独指定 Web/ISAPI 端口，避免改动 Web 端仍在使用的设备配置。
        try:
            port = int(cfg.get('ptzPort', cfg.get('ptz_port', row.get('port') or 80)))
        except (TypeError, ValueError):
            port = 80
        if not 1 <= port <= 65535:
            raise PtzError('PTZ Web 端口须在 1-65535 之间')
        try:
            channel_id = int(cfg.get('channelId', cfg.get('channel_id', 1)))
        except (TypeError, ValueError):
            channel_id = 1
        scheme = str(cfg.get('ptzScheme') or ('https' if port == 443 else 'http')).lower()
        if scheme not in ('http', 'https'):
            raise PtzError('ptzScheme 只能是 http 或 https')
        verify_tls = bool(cfg.get('ptzVerifyTls', False))
        return _Camera(
            base_url=f'{scheme}://{ip}:{port}',
            username=str(row.get('username') or 'admin'),
            password=str(row.get('password') or ''),
            channel_id=max(1, channel_id),
            verify_tls=verify_tls,
        )

    def _send_start(self, camera: _Camera, direction: str, speed: int):
        if direction == 'auto-pan':
            body = self._auto_pan_xml(speed)
            path = f'/ISAPI/PTZCtrl/channels/{camera.channel_id}/autoPan'
        else:
            pan, tilt, zoom = _DIRECTIONS[direction]
            body = self._continuous_xml(pan * speed, tilt * speed, zoom * speed)
            path = f'/ISAPI/PTZCtrl/channels/{camera.channel_id}/continuous'
        self._put(camera, path, body)

    def _send_stop(self, camera: _Camera, direction: str):
        if direction == 'auto-pan':
            body = self._auto_pan_xml(0)
            path = f'/ISAPI/PTZCtrl/channels/{camera.channel_id}/autoPan'
        else:
            body = self._continuous_xml(0, 0, 0)
            path = f'/ISAPI/PTZCtrl/channels/{camera.channel_id}/continuous'
        self._put(camera, path, body)

    @staticmethod
    def _continuous_xml(pan: int, tilt: int, zoom: int) -> str:
        return (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f'<PTZData><pan>{pan}</pan><tilt>{tilt}</tilt><zoom>{zoom}</zoom></PTZData>'
        )

    @staticmethod
    def _auto_pan_xml(speed: int) -> str:
        return (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f'<autoPanData><autoPan>{speed}</autoPan></autoPanData>'
        )

    @staticmethod
    def _put(camera: _Camera, path: str, body: str):
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.put(
                f'{camera.base_url}{path}',
                data=body.encode('utf-8'),
                headers={'Content-Type': 'application/xml; charset=UTF-8'},
                auth=HTTPDigestAuth(camera.username, camera.password),
                timeout=(2.0, 3.0),
                verify=camera.verify_tls,
            )
        except requests.RequestException as exc:
            raise PtzError(f'连接球机失败: {exc.__class__.__name__}') from exc
        finally:
            session.close()
        if not response.ok:
            detail = ''
            try:
                detail = response.text.strip()[:300]
            except Exception:
                pass
            suffix = f'，设备返回: {detail}' if detail else ''
            raise PtzError(f'球机拒绝 PTZ 指令（HTTP {response.status_code}）{suffix}')

    def _arm_watchdog(self, device_id: str, camera: _Camera, direction: str):
        with self._state_lock:
            old = self._watchdogs.pop(device_id, None)
            if old:
                old.cancel()
            generation = self._generations.get(device_id, 0) + 1
            self._generations[device_id] = generation
            timer = threading.Timer(
                WATCHDOG_SECONDS,
                self._watchdog_stop,
                args=(device_id, generation, camera, direction),
            )
            timer.daemon = True
            self._watchdogs[device_id] = timer
            timer.start()

    def _cancel_watchdog(self, device_id: str):
        with self._state_lock:
            self._generations[device_id] = self._generations.get(device_id, 0) + 1
            timer = self._watchdogs.pop(device_id, None)
            if timer:
                timer.cancel()

    def _watchdog_stop(
        self,
        device_id: str,
        generation: int,
        camera: _Camera,
        direction: str,
    ):
        with self._state_lock:
            if self._generations.get(device_id) != generation:
                return
            self._watchdogs.pop(device_id, None)
        try:
            with self._command_lock:
                self._send_stop(camera, direction)
        except Exception as exc:
            print(f'[PTZ] 安全停止失败: {exc}', flush=True)


_service: HikPtzService | None = None
_service_lock = threading.Lock()


def get_hik_ptz_service() -> HikPtzService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = HikPtzService()
    return _service
