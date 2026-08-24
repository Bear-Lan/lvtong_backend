"""栏杆组播联调 demo（与中间件 MsgTransceiver.pubMsg 一致）。

用法:
  cd device_demo
  python main.py              # 默认抬杆一次后退出
  python main.py --down       # 落杆
  python main.py --iface 192.168.0.138
"""
from __future__ import annotations

import argparse

from msgtransceiver import MsgTransceiver

IFACE_DEFAULT = "192.168.0.138"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="发一次 $MDRMC 栏杆命令")
    parser.add_argument("--iface", default=IFACE_DEFAULT, help="组播出口网卡 IP")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--up", action="store_true", help="抬杆（默认）")
    g.add_argument("--down", action="store_true", help="落杆")
    args = parser.parse_args()

    raised = not args.down
    msg = f"$MDRMC,1,boombarrier={'true' if raised else 'false'}"

    trx = MsgTransceiver(iface_ip=args.iface)
    trx.pubMsg(msg)
    print("已发送，退出", flush=True)
