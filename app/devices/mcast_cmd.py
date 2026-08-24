"""向现场中间件发控制命令 $MDRMC（UDP 组播，与 device_demo.pubMsg 一致）。

协议例：
  $MDRMC,1,boombarrier=true
  $MDRMC,1,boombarrier=false
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
