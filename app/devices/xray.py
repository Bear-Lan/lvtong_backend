"""X 光机控制器

启扫：组播 $MDRMC,1,xraycap=true&{车头长}&{small|large}
关闭：组播 $MDRMC,1,xraycap=false
type / 车头长优先取预约受理写入的 _booking_state。
"""
from __future__ import annotations

import requests

from app.devices.base import DeviceBase, DeviceStatus
from app.devices.mcast_cmd import format_xraycap_command, send_xraycap

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
    """从预约状态补齐 type / 车头长。"""
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


def send_xray_capture(enabled: bool, *, car_length=None, vehicle_type: str = '') -> bool:
    """组播 xraycap 启停（供 booking 驳回等场景直接调用）。"""
    vtype, length = _booking_scan_params(car_length, vehicle_type)
    cmd = format_xraycap_command(enabled, length, vtype)
    ok = send_xraycap(enabled, car_length=length, vehicle_type=vtype)
    action = '启扫' if enabled else '关闭'
    print(
        f'[XRAY] xraycap {action} → $MDRMC,1,{cmd} ok={ok}',
        flush=True,
    )
    return ok


def close_xray_capture() -> bool:
    """手动关光机：$MDRMC,1,xraycap=false（急停/重置/闸机等）。"""
    return send_xray_capture(False)


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
        if action == 'stop_scan':
            return self.stop_scan(
                params.get('car_length'),
                params.get('vehicle_type', ''),
            )
        return False

    def start_scan(
        self,
        channel: str,
        car_length: float | None = None,
        scan_id: str = '',
        vehicle_type: str = '',
    ) -> bool:
        _ = scan_id
        if str(channel) == '160':
            print('[XRAY] 160 启扫暂不支持，跳过', flush=True)
            return True

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

        ok = send_xray_capture(True, car_length=car_length, vehicle_type=vehicle_type)
        if not ok:
            self._last_error = 'MDRMC xraycap 发送失败'
        return ok

    def stop_scan(
        self,
        car_length: float | None = None,
        vehicle_type: str = '',
    ) -> bool:
        return send_xray_capture(False, car_length=car_length, vehicle_type=vehicle_type)

    def get_image_url(self, channel: str) -> str:
        return self.config.get(f'image_{channel}_url', '')
