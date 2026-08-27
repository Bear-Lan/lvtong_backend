"""X 光机控制器

启扫：GET {base}/xray/capture?type=small|large&triggeroffset={车头长}
type / triggeroffset 优先取预约受理写入的 _booking_state。
"""
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests

from app.devices.base import DeviceBase, DeviceStatus

# 与前端 bookingMath.BIG_HEIGHT_THRESHOLD 一致：>2.8m → large
_BIG_HEIGHT_THRESHOLD = 2.8


def _vehicle_type_from_height(height) -> str:
    try:
        h = float(height)
    except (TypeError, ValueError):
        h = 3.0
    return 'large' if h > _BIG_HEIGHT_THRESHOLD else 'small'


def _normalize_vehicle_type(raw: str, height=None) -> str:
    s = (raw or '').strip().lower()
    if s in ('large', 'big'):
        return 'large'
    if s == 'small':
        return 'small'
    return _vehicle_type_from_height(height)


def _booking_scan_params(
    car_length: float | None = None,
    vehicle_type: str = '',
) -> tuple[str, float]:
    """从预约状态补齐 type / triggeroffset。"""
    height = None
    length = car_length
    vtype = vehicle_type
    try:
        from app.api.booking import get_booking_state

        st = get_booking_state() or {}
        height = st.get('car_height')
        if length is None or (isinstance(length, (int, float)) and length <= 0):
            try:
                length = float(st.get('car_length') or 1.5)
            except (TypeError, ValueError):
                length = 1.5
        if not vtype:
            vtype = str(st.get('vehicle_type') or '')
    except Exception:
        if length is None:
            length = 1.5
    try:
        length_f = float(length)
    except (TypeError, ValueError):
        length_f = 1.5
    if length_f <= 0:
        length_f = 1.5
    return _normalize_vehicle_type(vtype, height), length_f


def build_capture_url(base: str, vehicle_type: str, triggeroffset: float) -> str:
    """拼启扫 URL；去掉旧 frames/id 等，只保留 type + triggeroffset。"""
    base = (base or '').strip()
    if not base:
        return ''
    parts = urlsplit(base)
    # 若配置已带 ?frames=…&triggeroffset=…&id= 等，整段 query 重写
    path = parts.path or '/xray/capture'
    if not path.endswith('/capture') and '/xray/capture' not in path:
        # 允许只配到 host:port
        path = '/xray/capture' if path in ('', '/') else path.rstrip('/') + '/xray/capture'
    query = urlencode({
        'type': vehicle_type,
        'triggeroffset': f'{triggeroffset:g}',
    })
    return urlunsplit((parts.scheme, parts.netloc, path, query, ''))


class XRayController(DeviceBase):

    def initialize(self) -> bool:
        ok = self.health_check()
        self.status = DeviceStatus.Online if ok else DeviceStatus.Offline
        if not ok:
            self._last_error = 'X光机无法连接'
        return ok

    def health_check(self) -> bool:
        url = self.config.get('health_url', '')
        if not url:
            return True
        try:
            resp = requests.get(url, timeout=5)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def reconnect(self) -> bool:
        return self.initialize()

    def execute_action(self, action: str, params: dict | None = None) -> bool:
        params = params or {}
        if action == 'start_scan':
            return self.start_scan(
                params.get('channel', '200'),
                params.get('car_length'),
                params.get('scan_id', ''),
                params.get('vehicle_type', ''),
            )
        return False

    def _resolve_start_base(self, channel: str) -> str:
        """启扫 base URL：只读 devices.py / 环境变量（不读 DB）。"""
        try:
            from config.devices import (
                XRAY_START_TRANSPARENT_URL,
                XRAY_START_YR_TRANSPARENT_URL,
            )
            if str(channel) == '160':
                return (XRAY_START_YR_TRANSPARENT_URL or XRAY_START_TRANSPARENT_URL or '').strip()
            return (XRAY_START_TRANSPARENT_URL or '').strip()
        except Exception:
            return ''

    def start_scan(
        self,
        channel: str,
        car_length: float | None = None,
        scan_id: str = '',
        vehicle_type: str = '',
    ) -> bool:
        # 预约「透视关」：一律不发启扫
        try:
            from app.api.booking import get_booking_state
            if not bool((get_booking_state() or {}).get('is_check_xray', True)):
                print(
                    f'[XRAY] start_scan skipped (透视关) ch={channel}',
                    flush=True,
                )
                return True
        except Exception:
            pass

        base = self._resolve_start_base(channel)
        if not base:
            self._last_error = f'未配置启扫 URL (channel={channel})'
            return False

        vtype, offset = _booking_scan_params(car_length, vehicle_type)
        full_url = build_capture_url(base, vtype, offset)
        # 兼容旧配置：若 base 已是完整 capture 且带 query，build 已重写；
        # scan_id 新协议不用，忽略
        _ = scan_id
        try:
            from app.services.middleware_http import middleware_get

            print(
                f'[XRAY] start_scan ch={channel} → {full_url}',
                flush=True,
            )
            resp = middleware_get(full_url, timeout=10)
            ok = resp.status_code == 200
            if not ok:
                self._last_error = f'HTTP {resp.status_code}'
            return ok
        except Exception as e:
            self._last_error = str(e)
            print(f'[XRAY] start_scan 失败: {e}', flush=True)
            return False

    def get_image_url(self, channel: str) -> str:
        return self.config.get(f'image_{channel}_url', '')
