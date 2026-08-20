#!/usr/bin/env python3
"""海康摄像头语音能力探测 + 试播 demo（独立脚本，不依赖后端服务运行）。

用法（在 flask_backend 目录、已激活 venv）:

  # 手动指定
  python tools/demo_hik_voice.py --ip 192.168.88.101 --user admin --password YOUR_PASS

  # 从本机 DB 读 cam_booking
  python tools/demo_hik_voice.py --from-db

  # 只探测能力，不试播
  python tools/demo_hik_voice.py --from-db --probe-only

  # 试播指定文案
  python tools/demo_hik_voice.py --from-db --text "绿通车辆，按键检测"

会依次探测常见 ISAPI 路径，并尝试:
  1) Parking 文本播报 voiceBroadcastInfo
  2) Event AudioAlarm 相关
  3) TwoWayAudio 通道 open/close（真正推音频需 ulaw 文件，见 --ulaw）
  4) TCP :50000（Qt TTSVoice 协议）
"""
from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth

# 保证可从任意 cwd 运行时找到 app.*
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _auth(user: str, password: str):
    return HTTPDigestAuth(user, password) if user else None


def _req(method: str, url: str, auth, **kwargs):
    kwargs.setdefault('timeout', 4)
    kwargs.setdefault('auth', auth)
    try:
        r = requests.request(method, url, **kwargs)
        body = (r.text or '')[:300].replace('\n', ' ')
        return r.status_code, body, None
    except Exception as e:
        return None, '', e


def probe(ip: str, port: int, user: str, password: str) -> None:
    base = f'http://{ip}:{port}'
    auth = _auth(user, password)
    paths = [
        ('GET', '/ISAPI/System/deviceInfo'),
        ('GET', '/ISAPI/System/capabilities'),
        ('GET', '/ISAPI/System/Audio/channels'),
        ('GET', '/ISAPI/System/TwoWayAudio/channels'),
        ('GET', '/ISAPI/Parking/channels/1/voiceBroadcastInfo'),
        ('GET', '/ISAPI/Event/triggers/notifications/AudioAlarm'),
        ('GET', '/ISAPI/Event/triggers/notifications/AudioAlarm?format=json'),
        ('GET', '/ISAPI/Intelligent/AlertStream'),
        ('GET', '/ISAPI/AccessControl/EventCardLinkageCfg/CustomAudio?format=json'),
        ('GET', '/ISAPI/VideoIntercom/capabilities'),
    ]
    print(f'\n=== PROBE {base} user={user!r} ===')
    for method, path in paths:
        code, body, err = _req(method, base + path, auth)
        if err:
            print(f'  {method:4} {path}\n       ERR {err}')
        else:
            print(f'  {method:4} {path}\n       HTTP {code}  {body[:180]}')


def try_voice_broadcast(ip: str, port: int, user: str, password: str, text: str) -> bool:
    """车牌/门口机常见文本 TTS。"""
    url = f'http://{ip}:{port}/ISAPI/Parking/channels/1/voiceBroadcastInfo'
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<VoiceBroadcastInfo version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
        f'<information min="0" max="128">{text[:120]}</information>'
        '</VoiceBroadcastInfo>'
    )
    auth = _auth(user, password)
    print(f'\n=== TRY voiceBroadcastInfo text={text!r} ===')
    for method in ('PUT', 'POST'):
        code, resp, err = _req(
            method,
            url,
            auth,
            data=body.encode('utf-8'),
            headers={'Content-Type': 'application/xml; charset="UTF-8"'},
        )
        if err:
            print(f'  {method} ERR {err}')
            continue
        print(f'  {method} HTTP {code}  {resp[:200]}')
        if code in (200, 201):
            print('  → 成功（若相机带喇叭且支持该接口，应能听到）')
            return True
    return False


def try_audio_alarm_json(ip: str, port: int, user: str, password: str) -> None:
    """部分机型用 AudioAlarm JSON；此处只探测写权限，不强制改配置。"""
    url = f'http://{ip}:{port}/ISAPI/Event/triggers/notifications/AudioAlarm?format=json'
    print('\n=== GET AudioAlarm (json) ===')
    code, body, err = _req('GET', url, _auth(user, password))
    if err:
        print(f'  ERR {err}')
    else:
        print(f'  HTTP {code}  {body[:400]}')


def try_twoway_open_close(ip: str, port: int, user: str, password: str) -> int | None:
    """探测双向对讲通道；真正放音要推 G.711 流。返回 (channel, sessionId)。"""
    base = f'http://{ip}:{port}/ISAPI/System/TwoWayAudio/channels'
    auth = _auth(user, password)
    print('\n=== TwoWayAudio channels ===')
    code, body, err = _req('GET', base, auth)
    if err:
        print(f'  GET ERR {err}')
        return None
    print(f'  GET HTTP {code}  {body[:400]}')
    if code != 200:
        return None

    ch = 1
    for action in ('close', 'open'):
        u = f'{base}/{ch}/{action}'
        c, b, e = _req('PUT', u, auth, data=b'', headers={'Content-Length': '0'})
        if e:
            print(f'  PUT {action} ERR {e}')
        else:
            print(f'  PUT {action} HTTP {c}  {b[:200]}')
            if action == 'open' and c in (200, 201):
                print('  → 通道已打开；可用 --ulaw file.ulaw 或 --beep 推流试听')
    # 探测完先关掉，避免占着会话
    _req('PUT', f'{base}/{ch}/close', auth, data=b'', headers={'Content-Length': '0'})
    return ch


def _make_beep_ulaw(seconds: float = 1.0, freq: float = 880.0, sr: int = 8000) -> bytes:
    """纯 Python 生成 μ-law 蜂鸣（不依赖 audioop/ffmpeg）。"""
    import math

    # ITU-T G.711 μ-law 查表式近似
    def lin2ulaw(sample: int) -> int:
        BIAS = 0x84
        CLIP = 32635
        sign = 0x80 if sample < 0 else 0
        if sample < 0:
            sample = -sample
        if sample > CLIP:
            sample = CLIP
        sample = sample + BIAS
        exponent = 7
        mask = 0x4000
        while exponent > 0 and not (sample & mask):
            mask >>= 1
            exponent -= 1
        mantissa = (sample >> (exponent + 3)) & 0x0F
        return ~(sign | (exponent << 4) | mantissa) & 0xFF

    n = int(sr * seconds)
    out = bytearray(n)
    amp = 12000
    for i in range(n):
        v = int(amp * math.sin(2 * math.pi * freq * i / sr))
        out[i] = lin2ulaw(v)
    return bytes(out)


def try_push_ulaw(
    ip: str, port: int, user: str, password: str, channel: int, ulaw_path: str
) -> bool:
    """按实时速率推 G.711 μ-law 到 audioData（最接近「相机喇叭放一段音」）。"""
    import re
    import time

    path = Path(ulaw_path)
    if not path.is_file():
        print(f'  ulaw 文件不存在: {path}')
        return False
    data = path.read_bytes()
    auth = _auth(user, password)
    base = f'http://{ip}:{port}/ISAPI/System/TwoWayAudio/channels/{channel}'
    print(f'\n=== PUSH ulaw {path} ({len(data)} bytes) ===')
    _req('PUT', f'{base}/close', auth, data=b'', headers={'Content-Length': '0'})
    code, body, err = _req('PUT', f'{base}/open', auth, data=b'', headers={'Content-Length': '0'})
    if err or code not in (200, 201):
        print(f'  open 失败 HTTP {code} {err or body}')
        return False

    session_id = ''
    m = re.search(r'<sessionId>([^<]+)</sessionId>', body or '')
    if m:
        session_id = m.group(1).strip()
        print(f'  sessionId={session_id}')

    url = f'{base}/audioData'
    if session_id:
        url = f'{url}?sessionId={session_id}'

    # 方式 A：整包 PUT（部分机型可接受短音）
    try:
        r = requests.put(
            url,
            data=data,
            headers={
                'Content-Type': 'application/octet-stream',
                'Connection': 'keep-alive',
            },
            auth=auth,
            timeout=30,
        )
        print(f'  audioData(整包) HTTP {r.status_code}  {(r.text or "")[:200]}')
        if r.status_code in (200, 201):
            _req('PUT', f'{base}/close', auth, data=b'', headers={'Content-Length': '0'})
            return True
    except Exception as e:
        print(f'  audioData(整包) ERR {e}')

    # 方式 B：按 8kHz 实时分片（更贴近对讲）
    print('  再试实时分片推送…')
    _req('PUT', f'{base}/close', auth, data=b'', headers={'Content-Length': '0'})
    code, body, err = _req('PUT', f'{base}/open', auth, data=b'', headers={'Content-Length': '0'})
    session_id = ''
    m = re.search(r'<sessionId>([^<]+)</sessionId>', body or '')
    if m:
        session_id = m.group(1).strip()
    url = f'{base}/audioData'
    if session_id:
        url = f'{url}?sessionId={session_id}'

    chunk = 320
    ok = False
    try:
        r = requests.put(
            url,
            data=_iter_realtime(data, chunk, 8000),
            headers={'Content-Type': 'application/octet-stream'},
            auth=auth,
            timeout=60,
        )
        print(f'  audioData(分片) HTTP {r.status_code}  {(r.text or "")[:200]}')
        ok = r.status_code in (200, 201)
    except Exception as e:
        print(f'  audioData(分片) ERR {e}')
        ok = False
    finally:
        _req('PUT', f'{base}/close', auth, data=b'', headers={'Content-Length': '0'})
        print('  channel closed')
    return ok


def _iter_realtime(data: bytes, chunk: int, rate: int):
    import time

    sleep_s = chunk / float(rate)
    for i in range(0, len(data), chunk):
        yield data[i : i + chunk]
        time.sleep(sleep_s)


def try_tcp_tts(ip: str, text: str, tcp_port: int = 50000) -> bool:
    """Qt TTSVoice：`#` + GBK。"""
    print(f'\n=== TRY TCP {ip}:{tcp_port} text={text!r} ===')
    payload = b'#' + text.encode('gbk', errors='ignore')
    try:
        with socket.create_connection((ip, tcp_port), timeout=2.0) as sock:
            sock.sendall(payload)
        print('  → TCP 发送成功（若对端是 TTS 盒应能听到）')
        return True
    except OSError as e:
        print(f'  → TCP 失败: {e}')
        return False


def load_from_db(device_id: str) -> tuple[str, int, str, str]:
    from app.db.device import DBDevice

    row = DBDevice().getDevice(device_id)
    if not row:
        raise SystemExit(f'DB 中无设备 {device_id}')
    ip = row.get('ip_address') or ''
    if not ip:
        raise SystemExit(f'{device_id} 无 ip_address')
    port = int(row.get('port') or 80) or 80
    user = row.get('username') or 'admin'
    password = row.get('password') or ''
    print(f'[DB] {device_id} → {ip}:{port} user={user!r}')
    return ip, port, user, password


def main() -> int:
    ap = argparse.ArgumentParser(description='海康摄像头语音探测/试播')
    ap.add_argument('--ip', default='')
    ap.add_argument('--port', type=int, default=80)
    ap.add_argument('--user', default='admin')
    ap.add_argument('--password', default='')
    ap.add_argument('--from-db', action='store_true', help='从 DB 读 cam_booking')
    ap.add_argument('--device-id', default='cam_booking')
    ap.add_argument('--text', default='绿通车辆，按键检测')
    ap.add_argument('--probe-only', action='store_true')
    ap.add_argument('--ulaw', default='', help='可选：G.711 μ-law 文件路径，推到喇叭试听')
    ap.add_argument('--beep', action='store_true', help='生成 1 秒蜂鸣并推到喇叭试听')
    ap.add_argument('--tcp-port', type=int, default=50000)
    args = ap.parse_args()

    if args.from_db:
        ip, port, user, password = load_from_db(args.device_id)
    else:
        if not args.ip:
            ap.error('请提供 --ip 或 --from-db')
        ip, port, user, password = args.ip, args.port, args.user, args.password
        if not password:
            print('警告: --password 为空，Digest 认证很可能失败')

    probe(ip, port, user, password)
    if args.probe_only:
        return 0

    try_voice_broadcast(ip, port, user, password, args.text)
    try_audio_alarm_json(ip, port, user, password)
    ch = try_twoway_open_close(ip, port, user, password)
    ulaw_path = args.ulaw
    if args.beep:
        beep = Path('tools/_beep.ulaw')
        beep.parent.mkdir(parents=True, exist_ok=True)
        beep.write_bytes(_make_beep_ulaw(1.0))
        ulaw_path = str(beep)
        print(f'\n[beep] 已生成 {beep} ({beep.stat().st_size} bytes)')
    if ulaw_path and ch:
        try_push_ulaw(ip, port, user, password, ch, ulaw_path)
    try_tcp_tts(ip, args.text, args.tcp_port)

    print(
        '\n=== 结论提示 ===\n'
        '- voiceBroadcastInfo 200 → 用文本 TTS（车牌/门口机常见）\n'
        '- TwoWayAudio open 200 → 相机有喇叭通道，需推 G.711/AAC 流（不是纯文本）\n'
        '- TCP 50000 通 → 更像独立 TTS 盒（Qt TTSVoice），不一定是相机本身\n'
        '- 全失败 → 对照相机 Web：音频/对讲/语音播报 是否开启，并核对账号权限\n'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
