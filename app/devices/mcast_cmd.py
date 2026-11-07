"""向现场中间件发控制命令 $MDRMC（UDP 组播，与 device_demo.pubMsg 一致）。

协议例：
  $MDRMC,1,boombarrier=true
  $MDRMC,1,boombarrier=false
  $MDRMC,1,camcap=true
  $MDRMC,1,camcap=false
  $MDRMC,1,xraycap=true&1.8&large
  $MDRMC,3,redlight=true,greenlight=false,leddisplay=LTCL_YYJC

默认发往 224.0.0.100:9000；出口网卡取 MCAST_IFACE_IP / 自动猜测。
"""
from __future__ import annotations

import socket
import threading

_lock = threading.Lock()


def send_mdrmc(*commands: str) -> bool:
    """发送一条或多条控制命令。"""
    cmds = [c.strip() for c in commands if c and str(c).strip()]
    if not cmds:
        return False
    msg = f'$MDRMC,{len(cmds)},' + ','.join(cmds)
    return publish_mdrmc(msg)


def publish_mdrmc(message: str) -> bool:
    from config import MCAST_GROUP, MCAST_IFACE_IP, MCAST_PORT
    from app.devices.radar import _guess_mcast_iface

    group = MCAST_GROUP or '224.0.0.100'
    port = int(MCAST_PORT or 9000)
    iface = _guess_mcast_iface(MCAST_IFACE_IP or '')
    payload = message.encode('utf-8')

    with _lock:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if iface and iface != '0.0.0.0':
                try:
                    sock.bind((iface, 0))
                except OSError:
                    pass
                try:
                    sock.setsockopt(
                        socket.IPPROTO_IP,
                        socket.IP_MULTICAST_IF,
                        socket.inet_aton(iface),
                    )
                except OSError:
                    pass
            sock.sendto(payload, (group, port))
            print(f'[MDRMC] 组播 → {group}:{port} via {iface}  {message}', flush=True)
            return True
        except OSError as e:
            print(f'[MDRMC] 组播失败 {group}:{port}: {e}', flush=True)
            return False
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def send_camcap(enabled: bool) -> bool:
    """车身相机采集开关：$MDRMC,1,camcap=true|false"""
    flag = 'true' if enabled else 'false'
    return send_mdrmc(f'camcap={flag}')


def send_geomcap(enabled: bool) -> bool:
    """车辆几何信息采集开关；与只读LAZ接口独立。"""
    return send_mdrmc(f'geomcap={"true" if enabled else "false"}')


def format_xraycap_command(
    enabled: bool,
    car_length: float,
    vehicle_type: str,
) -> str:
    """xraycap 命令体：true&车头长&车型 或 false"""
    flag = 'true' if enabled else 'false'
    if not enabled:
        return f'xraycap={flag}'
    try:
        length = f'{float(car_length):g}'
    except (TypeError, ValueError):
        length = '1.5'
    vtype = (vehicle_type or 'small').strip().lower()
    if vtype in ('big',):
        vtype = 'large'
    if vtype not in ('small', 'large'):
        vtype = 'small'
    return f'xraycap={flag}&{length}&{vtype}'


def send_xraycap(
    enabled: bool,
    *,
    car_length: float = 1.5,
    vehicle_type: str = 'small',
) -> bool:
    """透视光机开关：$MDRMC,1,xraycap=true&1.8&large"""
    cmd = format_xraycap_command(enabled, car_length, vehicle_type)
    return send_mdrmc(cmd)
