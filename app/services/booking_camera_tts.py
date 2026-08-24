"""预约机语音播报（预置音频 + TwoWayAudio 实时推流）

预置：resources/announce/step{1-5}.wav（8kHz mono PCM）
编码：G.711 μ-law
推流：按 20ms/帧实时写入（禁止整包灌入）

重新生成 wav：python tools/gen_announce_audio.py
"""
from __future__ import annotations

import hashlib
import http.client
import os
import re
import struct
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests
from requests.auth import HTTPDigestAuth

ANNOUNCE_TEXT: dict[int, str] = {
    1: '绿通车辆，按键检测',
    2: '申请已接收，请原地等待',
    3: '申请已受理，请等待放行',
    4: '待检车辆，请通行',
    5: '检测中，请勿跟车',
}

_VOICE_CAM_ID = (
    os.environ.get('LANE_VOICE_DEVICE_ID', '').strip()
    or os.environ.get('TALK_VIDEO_DEVICE_ID', '').strip()
    or 'cam_booking'
)
_AUDIO_CHANNEL = int(os.environ.get('LANE_VOICE_AUDIO_CHANNEL', '1') or '1')
_CHUNK = 320  # 40ms @ 8kHz（减少 sleep 次数）
_SAMPLE_RATE = 8000

_ROOT = Path(__file__).resolve().parents[2]
_ANNOUNCE_DIR = Path(
    os.environ.get('LANE_ANNOUNCE_DIR', str(_ROOT / 'resources' / 'announce'))
)

_ulaw_cache: dict[int, bytes] = {}
_cache_lock = threading.Lock()
_play_lock = threading.Lock()
_play_gen = 0
_play_gen_lock = threading.Lock()
_playing_step: int | None = None
_digest_nc = 1
_digest_nc_lock = threading.Lock()
# TwoWayAudio 会话保活：招揽连播复用；对讲才 close
_session_lock = threading.Lock()
_session: dict[str, Any] | None = None
# 空闲招揽：上一句语声结束时刻
_timing_lock = threading.Lock()
_speech_end_mono = 0.0
# 长连接泵：招揽循环；step2 等可注入同一 PUT；abort 后才 close
_keep_lock = threading.Lock()
_keep_active = False
_keep_abort = threading.Event()
_keep_done = threading.Event()
_keep_done.set()
_inject_job: dict[str, Any] | None = None


def _ts() -> str:
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]


def get_idle_timing() -> tuple[float, float]:
    """(上一句语声结束 monotonic, open 预算)。保活会话时 open 预算为 0。"""
    with _timing_lock:
        return _speech_end_mono, 0.0


def _mark_speech_end() -> None:
    global _speech_end_mono
    with _timing_lock:
        _speech_end_mono = time.monotonic()


def speak_step(
    step: int,
    *,
    interrupt: bool = True,
    blocking: bool = False,
) -> bool:
    """播预置音频。

    interrupt=False：正在播同一句则跳过（空闲招揽，防自打断）
    interrupt=True：打断其它句子；若同一句正在播且 blocking，则等它播完（不重开）
    blocking=True：在当前线程等到本句结束再返回
    """
    step = int(step)
    if step not in ANNOUNCE_TEXT:
        return False
    text = ANNOUNCE_TEXT[step]

    if not interrupt:
        try:
            from app.services.voice_channel import get_voice_channel

            st = get_voice_channel().status()
            if st.get('busy') and st.get('owner') == 'talk':
                print(
                    f'[{_ts()}] [CAM-TTS] 跳过 step{step}（对讲占用中）',
                    flush=True,
                )
                return False
        except Exception:
            pass

    wait_same_gen: int | None = None
    with _play_gen_lock:
        global _play_gen, _playing_step
        if _playing_step == step:
            if not interrupt or blocking:
                # 同一句已在播：异步调用直接视为成功；阻塞调用则等待结束
                wait_same_gen = _play_gen
            else:
                _play_gen += 1
                my_gen = _play_gen
        else:
            if not interrupt and _playing_step is not None:
                print(
                    f'[{_ts()}] [CAM-TTS] 跳过 step{step}（其它句子播放中且 interrupt=False）',
                    flush=True,
                )
                return False
            _play_gen += 1
            my_gen = _play_gen

    if wait_same_gen is not None:
        print(
            f'[{_ts()}] [CAM-TTS] step{step} 已在播放 gen={wait_same_gen}，'
            f'{"等待结束" if blocking else "跳过重复"}',
            flush=True,
        )
        if blocking:
            _wait_gen_finished(wait_same_gen, step)
        return True

    if blocking:
        _speak_run(step, text, my_gen, steal=interrupt)
        return True

    threading.Thread(
        target=_speak_run,
        args=(step, text, my_gen, interrupt),
        daemon=True,
        name=f'cam-tts-{step}',
    ).start()
    return True


def speak_text(text: str) -> bool:
    text = (text or '').strip()
    for step, t in ANNOUNCE_TEXT.items():
        if t == text:
            return speak_step(step)
    print(f'[{_ts()}] [CAM-TTS] 无预置音频: {text}', flush=True)
    return False


def stop_speak() -> None:
    with _play_gen_lock:
        global _play_gen, _playing_step
        _play_gen += 1
        _playing_step = None
    # 不在这里 close：预约键/step2 还要接在招揽长连接上，对讲前走 release_voice_channel


def _wait_gen_finished(gen: int, step: int, timeout: float = 45.0) -> None:
    """等待某次播放结束（同 gen 且 playing_step 清空，或已被更新的 gen 打断）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _play_gen_lock:
            if _play_gen != gen:
                return
            if _playing_step is None:
                return
            if _playing_step != step:
                return
        time.sleep(0.05)
    print(f'[{_ts()}] [CAM-TTS] 等待 step{step} gen={gen} 结束超时', flush=True)


def _speak_run(step: int, text: str, my_gen: int, steal: bool = True) -> None:
    global _playing_step

    def should_stop() -> bool:
        with _play_gen_lock:
            return _play_gen != my_gen

    t0 = time.monotonic()
    print(f'[{_ts()}] [CAM-TTS] 开始 step{step} gen={my_gen}「{text}」', flush=True)

    from app.services.voice_channel import get_voice_channel

    vc = get_voice_channel()
    lock_token: str | None = None

    try:
        ulaw = _load_step_ulaw(step)
    except Exception as e:
        print(f'[{_ts()}] [CAM-TTS] 加载 step{step} 失败: {e}', flush=True)
        return

    cam = _resolve_voice_camera()
    if not cam:
        print(f'[{_ts()}] [CAM-TTS] 未找到语音相机 {_VOICE_CAM_ID}', flush=True)
        return

    ip = cam.get('ip_address') or ''
    user = cam.get('username') or 'admin'
    password = cam.get('password') or ''
    port = int(cam.get('port') or 80) or 80
    dur = len(ulaw) / float(_SAMPLE_RATE)
    print(
        f'[{_ts()}] [CAM-TTS] 目标 {ip}:{port} ch={_AUDIO_CHANNEL} '
        f'ulaw={len(ulaw)}B 预计{dur:.2f}s',
        flush=True,
    )

    sent = 0
    reason = ''
    ok = False
    with _play_lock:
        if should_stop():
            print(
                f'[{_ts()}] [CAM-TTS] 未开推即被打断 step{step} gen={my_gen}',
                flush=True,
            )
            return

        got = vc.acquire('announce', wait=False, steal=steal)
        if not got.get('ok'):
            print(
                f'[{_ts()}] [CAM-TTS] 未拿到通道锁 step{step}: {got.get("error")}',
                flush=True,
            )
            return
        lock_token = got.get('token')
        reentrant = bool(got.get('reentrant'))

        with _play_gen_lock:
            _playing_step = step
        try:
            ok, sent, reason = _push_ulaw_realtime(
                ip, port, user, password, _AUDIO_CHANNEL, ulaw, should_stop
            )
        finally:
            with _play_gen_lock:
                if _play_gen == my_gen:
                    _playing_step = None
            # 接到招揽长连接上时锁是招揽线程的，这里不能 release
            if lock_token and not reentrant:
                vc.release(owner='announce', token=lock_token)

    elapsed = time.monotonic() - t0
    if should_stop():
        print(
            f'[{_ts()}] [CAM-TTS] 中断 step{step} 已推 {sent}/{len(ulaw)}B '
            f'用时{elapsed:.2f}s reason={reason}',
            flush=True,
        )
    elif ok:
        _mark_speech_end()
        print(
            f'[{_ts()}] [CAM-TTS] OK [{ip}] step{step}「{text}」'
            f' 推满 {sent}B 用时{elapsed:.2f}s',
            flush=True,
        )
    else:
        print(
            f'[{_ts()}] [CAM-TTS] 失败 [{ip}] step{step}「{text}」'
            f' 已推 {sent}/{len(ulaw)}B 用时{elapsed:.2f}s reason={reason}',
            flush=True,
        )


# ---------- 预置音频 → μ-law ----------

def _load_step_ulaw(step: int) -> bytes:
    with _cache_lock:
        hit = _ulaw_cache.get(step)
        if hit:
            return hit

    ulaw_path = _ANNOUNCE_DIR / f'step{step}.ulaw'
    if ulaw_path.is_file():
        data = ulaw_path.read_bytes()
    else:
        wav_path = _ANNOUNCE_DIR / f'step{step}.wav'
        if not wav_path.is_file():
            raise FileNotFoundError(
                f'缺少 {wav_path}，请先运行: python tools/gen_announce_audio.py'
            )
        pcm = _read_wav_pcm8k_mono(wav_path)
        data = _pcm16_to_ulaw(pcm)

    if len(data) < 320:
        raise RuntimeError(f'音频过短: step{step}')
    with _cache_lock:
        _ulaw_cache[step] = data
    return data


def get_step_audio_sec(step: int) -> float:
    """预置音频内容时长（不含相机 close/open 开销）。"""
    ulaw = _load_step_ulaw(step)
    return len(ulaw) / float(_SAMPLE_RATE)


def _read_wav_pcm8k_mono(path: Path) -> bytes:
    with wave.open(str(path), 'rb') as wf:
        if wf.getsampwidth() != 2:
            raise RuntimeError(f'仅支持 16bit WAV: {path}')
        if wf.getnchannels() != 1:
            raise RuntimeError(f'仅支持单声道 WAV: {path}')
        if wf.getframerate() != 8000:
            raise RuntimeError(f'仅支持 8kHz WAV: {path}')
        return wf.readframes(wf.getnframes())


def _lin2ulaw(sample: int) -> int:
    bias = 0x84
    clip = 32635
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    if sample > clip:
        sample = clip
    sample = sample + bias
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        mask >>= 1
        exponent -= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _pcm16_to_ulaw(pcm: bytes) -> bytes:
    n = len(pcm) // 2
    out = bytearray(n)
    for i in range(n):
        s = struct.unpack_from('<h', pcm, i * 2)[0]
        out[i] = _lin2ulaw(s)
    return bytes(out)


# ---------- 设备 ----------

def _resolve_voice_camera() -> dict[str, Any] | None:
    try:
        from app.db.device import DBDevice

        row = DBDevice().getDevice(_VOICE_CAM_ID)
        if row and row.get('ip_address'):
            return row
    except Exception as e:
        print(f'[{_ts()}] [CAM-TTS] 读设备失败: {e}', flush=True)

    ip = os.environ.get('LANE_VOICE_CAM_IP', '').strip()
    if not ip:
        return None
    return {
        'ip_address': ip,
        'port': int(os.environ.get('LANE_VOICE_CAM_HTTP_PORT', '80')),
        'username': os.environ.get('LANE_VOICE_CAM_USER', 'admin'),
        'password': os.environ.get('LANE_VOICE_CAM_PASSWORD', ''),
    }


def _auth(user: str, password: str) -> HTTPDigestAuth | None:
    return HTTPDigestAuth(user, password) if user else None


# ---------- Digest + 实时推流 ----------

def _md5(s: str) -> str:
    return hashlib.md5(s.encode('utf-8')).hexdigest()


def _parse_digest(header: str) -> dict[str, str] | None:
    m = re.match(r'Digest\s+(.*)', header or '', re.I)
    if not m:
        return None
    out: dict[str, str] = {}
    for km in re.finditer(r'(\w+)=("([^"]*)"|([^,]*))', m.group(1)):
        out[km.group(1)] = km.group(3) if km.group(3) is not None else (km.group(4) or '')
    return out


def _build_digest(
    challenge: dict[str, str],
    method: str,
    uri: str,
    user: str,
    password: str,
) -> str:
    with _digest_nc_lock:
        global _digest_nc
        nc_i = _digest_nc
        _digest_nc += 1
    realm = challenge.get('realm', '')
    nonce = challenge.get('nonce', '')
    qop = (challenge.get('qop') or '').split(',')[0].strip()
    opaque = challenge.get('opaque')
    cnonce = os.urandom(8).hex()
    nc = f'{nc_i:08d}'
    ha1 = _md5(f'{user}:{realm}:{password}')
    ha2 = _md5(f'{method}:{uri}')
    if qop:
        response = _md5(f'{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}')
    else:
        response = _md5(f'{ha1}:{nonce}:{ha2}')
    parts = [
        f'Digest username="{user}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
    ]
    if qop:
        parts.extend([f'qop={qop}', f'nc={nc}', f'cnonce="{cnonce}"'])
    if opaque:
        parts.append(f'opaque="{opaque}"')
    return ', '.join(parts)


def _talk_put(
    ip: str,
    port: int,
    user: str,
    password: str,
    path: str,
    timeout: float = 5.0,
) -> tuple[int, str]:
    auth = _auth(user, password)
    url = f'http://{ip}:{port}{path}'
    try:
        r = requests.put(
            url,
            auth=auth,
            timeout=timeout,
            headers={'Content-Length': '0'},
        )
        return r.status_code, r.text or ''
    except Exception as e:
        return 0, str(e)


def _open_twoway(
    ip: str,
    http_port: int,
    user: str,
    password: str,
    channel: int,
) -> tuple[int, str]:
    """open TwoWayAudio；403/409 表示通道仍占用 → close 一次后重试。"""
    base = f'/ISAPI/System/TwoWayAudio/channels/{channel}'
    code, body = _talk_put(ip, http_port, user, password, f'{base}/open')
    print(f'[{_ts()}] [CAM-TTS] open HTTP {code}', flush=True)
    if code in (200, 201):
        return code, body
    if code in (403, 409):
        print(f'[{_ts()}] [CAM-TTS] 通道占用，close 后重试 open', flush=True)
        _force_close_twoway(ip, http_port, user, password, channel)
        code, body = _talk_put(ip, http_port, user, password, f'{base}/open')
        print(f'[{_ts()}] [CAM-TTS] open 重试 HTTP {code}', flush=True)
    return code, body


def _session_uri(sess: dict[str, Any]) -> str:
    base = f'/ISAPI/System/TwoWayAudio/channels/{sess["channel"]}'
    sid = sess.get('session_id') or ''
    return f'{base}/audioData?sessionId={sid}' if sid else f'{base}/audioData'


def _drop_session(*, close: bool = True) -> None:
    global _session
    with _session_lock:
        sess = _session
        _session = None
    if not sess or not close:
        return
    _force_close_twoway(
        sess['ip'], sess['port'], sess['user'], sess['password'], sess['channel']
    )


def _ensure_session(
    ip: str,
    http_port: int,
    user: str,
    password: str,
    channel: int,
) -> tuple[str | None, str]:
    """复用已打开的 TwoWayAudio；没有则 open。返回 (uri, err)。"""
    global _session
    with _session_lock:
        sess = _session
        if (
            sess
            and sess.get('ip') == ip
            and sess.get('port') == http_port
            and sess.get('channel') == channel
        ):
            print(
                f'[{_ts()}] [CAM-TTS] 复用会话 sessionId={sess.get("session_id")}',
                flush=True,
            )
            return _session_uri(sess), ''

    if sess:
        _drop_session(close=True)

    code, body = _open_twoway(ip, http_port, user, password, channel)
    if code not in (200, 201):
        return None, f'open-{code}:{body[:80]}'

    session_id = ''
    m = re.search(r'<sessionId>([^<]+)</sessionId>', body)
    if m:
        session_id = m.group(1).strip()
        print(f'[{_ts()}] [CAM-TTS] sessionId={session_id}', flush=True)

    sess = {
        'ip': ip,
        'port': http_port,
        'user': user,
        'password': password,
        'channel': channel,
        'session_id': session_id,
    }
    with _session_lock:
        _session = sess
    return _session_uri(sess), ''


def _inject_pending() -> bool:
    with _keep_lock:
        return _inject_job is not None


def _pop_inject() -> dict[str, Any] | None:
    global _inject_job
    with _keep_lock:
        job = _inject_job
        _inject_job = None
        return job


def abort_keepalive(*, timeout: float = 3.0) -> None:
    """打断招揽长连接并等待它 close。"""
    global _keep_active
    with _keep_lock:
        active = _keep_active
    if not active:
        return
    print(f'[{_ts()}] [CAM-TTS] 结束招揽长连接（交给对讲/复位）', flush=True)
    _keep_abort.set()
    _keep_done.wait(timeout=max(0.2, float(timeout)))


def inject_ulaw_on_keepalive(
    ulaw: bytes,
    should_stop: Callable[[], bool],
) -> tuple[bool, int, str] | None:
    """若招揽长连接还在，把这段音频接到同一条 PUT 上。没有泵则返回 None。"""
    global _inject_job
    job = {
        'ulaw': ulaw,
        'event': threading.Event(),
        'ok': False,
        'sent': 0,
        'reason': '',
    }
    deadline = time.monotonic() + 45.0
    queued = False
    while time.monotonic() < deadline:
        if should_stop():
            return False, 0, 'interrupted'
        with _keep_lock:
            if not _keep_active:
                return None
            if _inject_job is None:
                _inject_job = job
                queued = True
                break
        time.sleep(0.05)
    if not queued:
        return None
    print(f'[{_ts()}] [CAM-TTS] 注入长连接 {len(ulaw)}B（不断开会话）', flush=True)
    while not job['event'].wait(timeout=0.05):
        if should_stop() or time.monotonic() >= deadline:
            with _keep_lock:
                if _inject_job is job:
                    _inject_job = None
            return False, 0, 'inject-timeout'
    return bool(job['ok']), int(job['sent']), str(job['reason'] or '')


def _push_ulaw_realtime(
    ip: str,
    http_port: int,
    user: str,
    password: str,
    channel: int,
    ulaw: bytes,
    should_stop: Callable[[], bool],
) -> tuple[bool, int, str]:
    if not ip or not ulaw:
        return False, 0, 'empty'

    injected = inject_ulaw_on_keepalive(ulaw, should_stop)
    if injected is not None:
        ok, sent, reason = injected
        if ok or reason not in ('pump-closed',):
            return injected
        print(f'[{_ts()}] [CAM-TTS] 长连接已结束，改为单独推流', flush=True)

    uri, err = _ensure_session(ip, http_port, user, password, channel)
    if not uri:
        return False, 0, err

    try:
        ok, sent, reason = _stream_ulaw_http(
            ip, http_port, user, password, uri, ulaw, should_stop
        )
    except Exception as e:
        print(f'[{_ts()}] [CAM-TTS] audioData 异常: {e}', flush=True)
        ok, sent, reason = False, 0, f'exc:{e}'

    if ok:
        return ok, sent, reason

    # 会话失效：close 后重开再推一次
    print(f'[{_ts()}] [CAM-TTS] 会话失效 ({reason})，重连后再推', flush=True)
    _drop_session(close=True)
    if should_stop():
        return False, sent, reason
    uri, err = _ensure_session(ip, http_port, user, password, channel)
    if not uri:
        return False, sent, err or reason
    try:
        return _stream_ulaw_http(
            ip, http_port, user, password, uri, ulaw, should_stop
        )
    except Exception as e:
        _drop_session(close=True)
        return False, sent, f'exc:{e}'


def _force_close_twoway(
    ip: str,
    http_port: int,
    user: str,
    password: str,
    channel: int,
) -> None:
    """连续 close，尽量释放预约机 TwoWayAudio。"""
    base = f'/ISAPI/System/TwoWayAudio/channels/{channel}'
    # 一次即可；二次 close 往往多耗 ~1s 且收益很小
    code, _ = _talk_put(ip, http_port, user, password, f'{base}/close', timeout=2.0)
    print(f'[{_ts()}] [CAM-TTS] close HTTP {code}', flush=True)


def release_voice_channel(*, settle_sec: float = 0.0) -> None:
    """打断招揽长连接并 close，供前端对讲。talk 占用时跳过。"""
    from app.services.voice_channel import get_voice_channel

    vc = get_voice_channel()
    st = vc.status()
    if st.get('owner') == 'talk':
        print(f'[{_ts()}] [CAM-TTS] 对讲占用中，跳过 release close', flush=True)
        return

    abort_keepalive(timeout=3.0)

    got = vc.acquire('announce', wait=False, steal=False)
    if not got.get('ok'):
        print(
            f'[{_ts()}] [CAM-TTS] 跳过 release: {got.get("error")}',
            flush=True,
        )
        return
    lock_token = got.get('token')
    reentrant = bool(got.get('reentrant'))

    try:
        print(
            f'[{_ts()}] [CAM-TTS] 释放 TwoWayAudio'
            + (f' settle {settle_sec:.2f}s' if settle_sec > 0 else ''),
            flush=True,
        )
        _drop_session(close=True)
        if settle_sec > 0:
            time.sleep(settle_sec)
    finally:
        if not reentrant:
            vc.release(owner='announce', token=lock_token)


def _probe_digest(
    ip: str, port: int, user: str, password: str, uri: str
) -> tuple[str | None, str]:
    conn = http.client.HTTPConnection(ip, port, timeout=10)
    try:
        conn.request(
            'PUT', uri, body=b'', headers={'Content-Length': '0', 'Connection': 'close'}
        )
        resp = conn.getresponse()
        resp.read()
        if resp.status == 401:
            challenge = _parse_digest(resp.getheader('WWW-Authenticate') or '')
            if not challenge:
                return None, 'digest-parse'
            return _build_digest(challenge, 'PUT', uri, user, password), ''
        if resp.status >= 400:
            return None, f'probe-{resp.status}'
        return None, ''
    except Exception as e:
        return None, f'probe-exc:{e}'
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _write_paced(
    conn: http.client.HTTPConnection,
    data: bytes,
    should_stop: Callable[[], bool],
) -> tuple[bool, int, str]:
    offset = 0
    t0 = time.perf_counter()
    last_log = t0
    while offset < len(data):
        if should_stop():
            return False, offset, 'interrupted'
        end = min(offset + _CHUNK, len(data))
        try:
            conn.send(data[offset:end])
        except OSError as e:
            return False, offset, f'send:{e}'
        offset = end
        target = t0 + offset / float(_SAMPLE_RATE)
        delay = target - time.perf_counter()
        if delay > 0.001:
            time.sleep(delay)
        now = time.perf_counter()
        if now - last_log >= 1.0:
            print(
                f'[{_ts()}] [CAM-TTS] 推流进度 {offset}/{len(data)} '
                f'({100.0 * offset / len(data):.0f}%)',
                flush=True,
            )
            last_log = now
    return True, offset, 'ok'


def run_idle_keepalive(
    step: int,
    gap_sec: float,
    should_continue: Callable[[], bool],
    *,
    on_clip: Callable[[], None] | None = None,
) -> None:
    """空闲招揽长连接：open 一次，同一条 audioData 上循环「话术 + 静音」。"""
    global _keep_active, _inject_job
    from app.services.voice_channel import get_voice_channel

    ulaw = _load_step_ulaw(step)
    gap_n = max(0, int(float(gap_sec) * _SAMPLE_RATE))
    silence = bytes([0xFF]) * gap_n
    text = ANNOUNCE_TEXT.get(step, '')

    cam = _resolve_voice_camera()
    if not cam:
        print(f'[{_ts()}] [CAM-TTS] 未找到语音相机，无法招揽', flush=True)
        return
    ip = cam.get('ip_address') or ''
    user = cam.get('username') or 'admin'
    password = cam.get('password') or ''
    port = int(cam.get('port') or 80) or 80

    vc = get_voice_channel()
    got = vc.acquire('announce', wait=False, steal=False)
    if not got.get('ok'):
        print(f'[{_ts()}] [CAM-TTS] 招揽未拿到通道锁: {got.get("error")}', flush=True)
        return
    lock_token = got.get('token')
    conn: http.client.HTTPConnection | None = None
    written = 0
    budget = _SAMPLE_RATE * 60 * 60  # 1 小时 μ-law
    hold = bytes([0xFF]) * _CHUNK
    hold_since: float | None = None

    _keep_abort.clear()
    _keep_done.clear()

    def clip_stop() -> bool:
        return _keep_abort.is_set() or _inject_pending() or not should_continue()

    def finish_inject(job: dict[str, Any], ok: bool, sent: int, reason: str) -> None:
        job['ok'] = ok
        job['sent'] = sent
        job['reason'] = reason
        job['event'].set()

    try:
        uri, err = _ensure_session(ip, port, user, password, _AUDIO_CHANNEL)
        if not uri:
            print(f'[{_ts()}] [CAM-TTS] 招揽 open 失败: {err}', flush=True)
            return
        auth_header, err = _probe_digest(ip, port, user, password, uri)
        if err:
            print(f'[{_ts()}] [CAM-TTS] 招揽 digest 失败: {err}', flush=True)
            _drop_session(close=True)
            return

        headers = {
            'Content-Type': 'application/octet-stream',
            'Content-Length': str(budget),
            'Connection': 'keep-alive',
        }
        if auth_header:
            headers['Authorization'] = auth_header

        conn = http.client.HTTPConnection(ip, port, timeout=120)
        conn.putrequest('PUT', uri)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        with _keep_lock:
            _keep_active = True
        print(
            f'[{_ts()}] [CAM-TTS] 招揽长连接已建立（句间静音 {gap_sec:.0f}s）',
            flush=True,
        )

        clip_n = 0
        while not _keep_abort.is_set() and written + max(len(ulaw), _CHUNK) < budget:
            job = _pop_inject()
            if job:
                hold_since = None
                print(
                    f'[{_ts()}] [CAM-TTS] 长连接接播 {len(job["ulaw"])}B',
                    flush=True,
                )
                ok, n, reason = _write_paced(
                    conn, job['ulaw'], lambda: _keep_abort.is_set()
                )
                written += n
                finish_inject(job, ok, n, reason)
                # step2 接播完立刻收尾，把通道交给对讲
                print(f'[{_ts()}] [CAM-TTS] 接播结束，关闭长连接', flush=True)
                break

            if should_continue():
                hold_since = None
                clip_n += 1
                print(
                    f'[{_ts()}] [CAM-TTS] 招揽第 {clip_n} 句「{text}」',
                    flush=True,
                )
                ok, n, reason = _write_paced(conn, ulaw, clip_stop)
                written += n
                if not ok and reason != 'interrupted':
                    print(f'[{_ts()}] [CAM-TTS] 招揽话术中断: {reason}', flush=True)
                    break
                if ok and on_clip:
                    try:
                        on_clip()
                    except Exception:
                        pass
                if (
                    should_continue()
                    and not _inject_pending()
                    and not _keep_abort.is_set()
                ):
                    print(
                        f'[{_ts()}] [CAM-TTS] 句间静音 {gap_sec:.0f}s（同一连接）',
                        flush=True,
                    )
                    ok, n, reason = _write_paced(conn, silence, clip_stop)
                    written += n
                    if not ok and reason != 'interrupted':
                        print(
                            f'[{_ts()}] [CAM-TTS] 招揽静音中断: {reason}',
                            flush=True,
                        )
                        break
                continue

            if hold_since is None:
                hold_since = time.monotonic()
                print(
                    f'[{_ts()}] [CAM-TTS] 招揽已停，长连接保持（等 step2 / 对讲）',
                    flush=True,
                )
            if time.monotonic() - hold_since > 20.0:
                print(f'[{_ts()}] [CAM-TTS] 长连接保持超时，关闭', flush=True)
                break

            def hold_stop() -> bool:
                return _keep_abort.is_set() or _inject_pending()

            ok, n, reason = _write_paced(conn, hold, hold_stop)
            written += n
            if not ok and reason != 'interrupted':
                break
    except Exception as e:
        print(f'[{_ts()}] [CAM-TTS] 招揽长连接异常: {e}', flush=True)
    finally:
        with _keep_lock:
            _keep_active = False
            job = _inject_job
            _inject_job = None
        if job is not None:
            finish_inject(job, False, 0, 'pump-closed')
        if conn is not None:
            try:
                if conn.sock is not None:
                    try:
                        conn.sock.shutdown(2)
                    except Exception:
                        pass
                conn.close()
            except Exception:
                pass
        _drop_session(close=True)
        vc.release(owner='announce', token=lock_token)
        _keep_done.set()
        print(f'[{_ts()}] [CAM-TTS] 招揽长连接已关闭', flush=True)


def _stream_ulaw_http(
    ip: str,
    port: int,
    user: str,
    password: str,
    uri: str,
    ulaw: bytes,
    should_stop: Callable[[], bool],
) -> tuple[bool, int, str]:
    """Digest 探活后按实时速率写 body（绝对时钟 pacing，避免 sleep+发送叠慢）。"""
    conn = http.client.HTTPConnection(ip, port, timeout=10)
    auth_header = None
    try:
        conn.request(
            'PUT', uri, body=b'', headers={'Content-Length': '0', 'Connection': 'close'}
        )
        resp = conn.getresponse()
        resp.read()
        if resp.status == 401:
            challenge = _parse_digest(resp.getheader('WWW-Authenticate') or '')
            if not challenge:
                return False, 0, 'digest-parse'
            auth_header = _build_digest(challenge, 'PUT', uri, user, password)
        elif resp.status >= 400:
            return False, 0, f'probe-{resp.status}'
    finally:
        conn.close()

    if should_stop():
        return False, 0, 'stop-after-probe'

    headers = {
        'Content-Type': 'application/octet-stream',
        'Content-Length': str(len(ulaw)),
        'Connection': 'close',
    }
    if auth_header:
        headers['Authorization'] = auth_header

    audio_s = len(ulaw) / float(_SAMPLE_RATE)
    print(
        f'[{_ts()}] [CAM-TTS] 开始推流 {len(ulaw)}B @{_SAMPLE_RATE}Hz（约 {audio_s:.2f}s）',
        flush=True,
    )

    conn2 = http.client.HTTPConnection(ip, port, timeout=max(60, audio_s + 30))
    offset = 0
    try:
        conn2.putrequest('PUT', uri)
        for k, v in headers.items():
            conn2.putheader(k, v)
        conn2.endheaders()

        t0 = time.perf_counter()
        last_log = t0
        while offset < len(ulaw):
            if should_stop():
                try:
                    conn2.close()
                except Exception:
                    pass
                return False, offset, 'interrupted'
            end = min(offset + _CHUNK, len(ulaw))
            conn2.send(ulaw[offset:end])
            offset = end
            # 按已发送采样对齐墙钟，发送耗时从 sleep 里扣除
            target = t0 + offset / float(_SAMPLE_RATE)
            delay = target - time.perf_counter()
            if delay > 0.001:
                time.sleep(delay)
            now = time.perf_counter()
            if now - last_log >= 1.0:
                print(
                    f'[{_ts()}] [CAM-TTS] 推流进度 {offset}/{len(ulaw)} '
                    f'({100.0 * offset / len(ulaw):.0f}%)',
                    flush=True,
                )
                last_log = now

        # 字节发完即关套接字，不等相机慢响应（否则整句耗时会被拖长）
        try:
            if conn2.sock is not None:
                try:
                    conn2.sock.shutdown(2)
                except Exception:
                    pass
            conn2.close()
        except Exception:
            pass
        print(f'[{_ts()}] [CAM-TTS] 推流字节已发完（会话保持）', flush=True)
        return True, offset, 'ok-sent'
    finally:
        try:
            conn2.close()
        except Exception:
            pass
