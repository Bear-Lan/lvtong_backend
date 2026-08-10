import time
from msgtransceiver import IMsgCallback, MsgTransceiver

# 组播源在 192.168.0.188，Windows 用 WLAN 入组才能收到
# 若改在 Ubuntu 上听，可改为 192.168.88.201 或 192.168.0.188
IFACE_IP = "192.168.0.138"


class MyCallback(IMsgCallback):
    def onMsgHandle(self, strMsg):
        print(f"收到消息: {strMsg}", flush=True)


if __name__ == "__main__":
    cb = MyCallback()
    # log_all=True：不过滤也打印，方便确认链路上有没有包
    transceiver = MsgTransceiver(
        pMsgCallback=cb,
        iface_ip=IFACE_IP,
        log_all=True,
    )
    transceiver.addMsgFilter("$VMRMC")  # 车辆运动
    transceiver.addMsgFilter("$NTRMC")  # PLC / X光 / 探测器
    print(f"监听组播 224.0.0.100:9000，入组网卡 {IFACE_IP}", flush=True)
    print("过滤前缀: $VMRMC / $NTRMC （log_all 开启，其它包也会 [RAW] 打印）", flush=True)
    print("等待数据包中…（无输出=链路上还没收到组播，不是程序卡死）", flush=True)
    try:
        waited = 0
        while True:
            time.sleep(5)
            waited += 5
            print(f"...仍在听，已等待 {waited}s", flush=True)
    except KeyboardInterrupt:
        print("exit")
