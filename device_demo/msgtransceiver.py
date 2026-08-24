"""UDP 组播收发（中间件联调用）。默认 224.0.0.100:9000。"""
from __future__ import annotations

import socket
import struct
import threading
import time


class IMsgCallback:
    def onMsgHandle(self, strMsg: str) -> None:
        raise NotImplementedError(" not implement onMsgHandle")


class MsgTransceiver(threading.Thread):
    def __init__(
        self,
        mcast_group="224.0.0.100",
        mcast_port=9000,
        pMsgCallback=None,
        iface_ip="0.0.0.0",
    ):
        super().__init__(daemon=True)
        self.mcast_group = mcast_group
        self.mcast_port = mcast_port
        self.iface_ip = iface_ip
        self.m_pMsgCallback: IMsgCallback | None = pMsgCallback
        self.m_MsgFilterList: list[str] = []

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(iface_ip),
            )
        except OSError:
            pass
        self.sock.bind(("", mcast_port))
        mreq = struct.pack(
            "4s4s",
            socket.inet_aton(mcast_group),
            socket.inet_aton(iface_ip),
        )
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        self.sock.settimeout(0.005)
        self.start()

    def addMsgFilter(self, strMsgFilter: str) -> None:
        self.m_MsgFilterList.append(strMsgFilter)

    def pubMsg(self, strMsg: str) -> bool:
        try:
            self.sock.sendto(strMsg.encode("utf-8"), (self.mcast_group, self.mcast_port))
            print(strMsg, flush=True)
            return True
        except Exception as e:
            print(f"pub msg error: {e}", flush=True)
            return False

    def parse(self, strMsg: str) -> None:
        if self.m_pMsgCallback and self.m_MsgFilterList and any(
            strMsg.startswith(k) for k in self.m_MsgFilterList
        ):
            self.m_pMsgCallback.onMsgHandle(strMsg)

    def run(self) -> None:
        while True:
            try:
                buf, _ = self.sock.recvfrom(1024)
                self.parse(buf.decode("utf-8", errors="replace"))
            except socket.timeout:
                time.sleep(0.001)
            except OSError as e:
                if getattr(e, "winerror", None) == 10038 or e.errno in (9, 10038):
                    break
                time.sleep(0.05)
            except Exception as e:
                print("MsgTransceiver error. " + str(e), flush=True)
                time.sleep(0.05)
