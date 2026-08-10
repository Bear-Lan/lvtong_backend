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
        log_all=False,
    ):
        super().__init__(daemon=True)
        self.mcast_group = mcast_group
        self.mcast_port = mcast_port
        self.iface_ip = iface_ip
        self.log_all = log_all
        self.m_pMsgCallback: IMsgCallback | None = pMsgCallback
        self.m_MsgFilterList = []

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Windows 多网卡时指定入组网卡，避免加到 Wi-Fi 上收不到现场组播
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

    def addMsgFilter(self, strMsgFilter) -> None:
        self.m_MsgFilterList.append(strMsgFilter)

    def pubMsg(self, strMsg: str) -> bool:
        try:
            self.sock.sendto(strMsg.encode("utf-8"), (self.mcast_group, self.mcast_port))
            print(f"{strMsg}")
            return True
        except Exception as e:
            print(f"pub msg error: {e}")
            return False

    def parse(self, strMsg: str) -> None:
        if self.m_pMsgCallback and self.m_MsgFilterList and any(
            strMsg.startswith(k) for k in self.m_MsgFilterList
        ):
            self.m_pMsgCallback.onMsgHandle(strMsg)

    def run(self):
        while True:
            try:
                buf, addr = self.sock.recvfrom(1024)
                raw = buf.decode("utf-8", errors="replace").strip()
                if self.log_all:
                    print(f"[RAW {addr[0]}:{addr[1]}] {raw}")
                self.parse(raw)
            except socket.timeout:
                time.sleep(0.001)
            except Exception as e:
                print("MsgTransceiver error. " + str(e))
