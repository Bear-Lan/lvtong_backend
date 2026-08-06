"""内置实时视频检测服务（Flask 侧）。

重活（RTSP/YOLO/渲染）在独立子进程 live_detect_worker 中运行，
避免 torch 与 gevent 抢 GIL 导致登录 502。

约束：
- 状态锁用 gevent.lock（RLock），不能用 threading.Lock。
- 不要在原生线程里查 DB（psycopg2 被 monkey-patch 后与 gevent hub 死锁）。
- 延迟启动用 gevent.spawn_later。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Generator
from urllib.parse import quote

try:
    from gevent.lock import RLock as _StateLock
except Exception:
    import threading as _threading
    _StateLock = _threading.RLock

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DEVICE_ID = 'camera4'
_DEFAULT_MODEL = _ROOT / 'tools' / 'yolo' / 'yolo11n.pt'
_ZONES_PATH = _ROOT / 'config' / 'live_zones.json'
_RUNTIME_DIR = _ROOT / 'uploads' / 'live_runtime'
_MJPEG_BOUNDARY = 'live-frame'
_MAX_MJPEG_CLIENTS = 8

_state_lock = _StateLock()
_proc = None
_running = False
_reconnect_requested = False
_device_id_used = ''
_device_ip = ''
_mjpeg_clients = 0
_cached_alarms = []
_alarms_mtime = 0.0
_jpeg_box = [None, 0.0]


def _resolved_device_id():
    return (os.environ.get('LIVE_DETECT_DEVICE_ID') or _DEFAULT_DEVICE_ID).strip()


def _resolved_model_path():
    raw = (os.environ.get('LIVE_DETECT_MODEL_PATH') or '').strip()
    return Path(raw) if raw else _DEFAULT_MODEL


def _alarms_dir():
    try:
        from config import IMAGE_STORAGE_ROOT
        root = Path(IMAGE_STORAGE_ROOT)
    except Exception:
        root = _ROOT / 'uploads'
    d = root / 'live_alarms'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _runtime_paths():
    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    return {
        'dir': _RUNTIME_DIR,
        'jpeg': _RUNTIME_DIR / 'latest.jpg',
        'jpeg0': _RUNTIME_DIR / 'latest.0.jpg',
        'jpeg1': _RUNTIME_DIR / 'latest.1.jpg',
        'slot': _RUNTIME_DIR / 'current.slot',
        'status': _RUNTIME_DIR / 'status.json',
        'alarms': _RUNTIME_DIR / 'alarms.jsonl',
        'stop': _RUNTIME_DIR / 'STOP',
    }


def _is_valid_jpeg(data):
    return bool(data and len(data) > 100 and data[:2] == b'\xff\xd8' and data[-2:] == b'\xff\xd9')


def _read_double_buffer_jpeg():
    paths = _runtime_paths()
    slot = None
    try:
        if paths['slot'].is_file():
            slot = paths['slot'].read_text(encoding='utf-8').strip()
    except Exception:
        slot = None
    candidates = []
    if slot in ('0', '1'):
        candidates.append(paths['jpeg0'] if slot == '0' else paths['jpeg1'])
        candidates.append(paths['jpeg1'] if slot == '0' else paths['jpeg0'])
    else:
        candidates.extend([paths['jpeg0'], paths['jpeg1'], paths['jpeg']])
    for p in candidates:
        for _ in range(3):
            try:
                if not p.is_file():
                    break
                data = p.read_bytes()
            except Exception:
                time.sleep(0.005)
                continue
            if _is_valid_jpeg(data):
                return data
            time.sleep(0.005)
    return None


def get_latest_jpeg():
    cached = _jpeg_box[0]
    data = _read_double_buffer_jpeg()
    if _is_valid_jpeg(data):
        _jpeg_box[0] = data
        _jpeg_box[1] = time.time()
        return data
    if _is_valid_jpeg(cached):
        return cached
    return None


def _load_device_by_device_id(device_id):
    from sqlalchemy import text
    from app.db.engine import engine
    did = (device_id or '').strip()
    if not did:
        return None
    with engine.connect() as conn:
        row = conn.execute(
            text('SELECT id, device_id, device_name, ip_address, port, username, password, config '
                 'FROM devices WHERE device_id = :did'),
            {'did': did},
        ).mappings().first()
    return dict(row) if row else None


def build_hik_rtsp_from_device(row, *, stream_type=1):
    ip = (row.get('ip_address') or '').strip()
    if not ip:
        raise ValueError('设备未配置 IP')
    user = quote((row.get('username') or 'admin').strip(), safe='')
    pwd = quote((row.get('password') or ''), safe='')
    cfg = row.get('config') or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    try:
        channel_id = int(cfg.get('channelId', cfg.get('channel_id', 1)))
    except (TypeError, ValueError):
        channel_id = 1
    st = int(stream_type)
    if st not in (1, 2):
        st = 1
    channel_num = max(1, channel_id) * 100 + st
    return f'rtsp://{user}:{pwd}@{ip}:554/Streaming/Channels/{channel_num}'


def _normalize_zones(raw_zones):
    zones = []
    for z in raw_zones or []:
        if not isinstance(z, dict):
            continue
        pts = z.get('points') or []
        if len(pts) < 3:
            continue
        kind = z.get('kind') or 'warning'
        if kind not in ('warning', 'danger'):
            kind = 'warning'
        zones.append({
            'id': str(z.get('id') or uuid.uuid4()),
            'name': str(z.get('name') or ''),
            'kind': kind,
            'points': [{'x': float(p.get('x', 0)), 'y': float(p.get('y', 0))}
                       for p in pts if isinstance(p, dict)],
            'visible': bool(z.get('visible', True)),
        })
    return zones


def load_zones(force=False):
    data = {'enter_seconds': 0.5, 'exit_seconds': 0.2, 'spatial_dedup_seconds': 5.0, 'zones': []}
    if not _ZONES_PATH.is_file():
        return data
    try:
        raw = json.loads(_ZONES_PATH.read_text(encoding='utf-8'))
        if isinstance(raw, dict):
            data.update({k: raw[k] for k in data if k in raw})
            if isinstance(raw.get('zones'), list):
                data['zones'] = raw['zones']
    except Exception as e:
        print(f'[LiveDetect] 读取区域配置失败: {e}')
    data['zones'] = _normalize_zones(data['zones'])
    return data


def save_zones(payload):
    data = {
        'enter_seconds': float(payload.get('enter_seconds', 0.5)),
        'exit_seconds': float(payload.get('exit_seconds', 0.2)),
        'spatial_dedup_seconds': float(payload.get('spatial_dedup_seconds', 5.0)),
        'zones': _normalize_zones(payload.get('zones', [])),
    }
    tmp = _ZONES_PATH.with_name(_ZONES_PATH.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, _ZONES_PATH)
    return data


def _read_status_file():
    path = _runtime_paths()['status']
    if not path.is_file():
        return {'connection': 'stopped', 'resolution': '--', 'display_fps': 0.0,
                'detection_fps': 0.0, 'device': '--', 'model': '--', 'error': None}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {'connection': 'starting', 'resolution': '--', 'display_fps': 0.0,
                'detection_fps': 0.0, 'device': '--', 'model': '--', 'error': 'status 读取失败'}


def get_status():
    st = _read_status_file()
    with _state_lock:
        running = _running
        did = _device_id_used
        dip = _device_ip
        clients = _mjpeg_clients
        alive = _proc is not None and _proc.poll() is None
    st['running'] = running and alive
    st['device_id'] = did or st.get('device_id') or ''
    st['device_ip'] = dip or st.get('device_ip') or ''
    st['mjpeg_clients'] = clients
    if running and not alive:
        st['connection'] = 'stopped'
        st['error'] = st.get('error') or '检测子进程已退出'
    return st


def get_alarms(limit=20):
    global _cached_alarms, _alarms_mtime
    path = _runtime_paths()['alarms']
    try:
        mtime = path.stat().st_mtime if path.is_file() else 0.0
    except OSError:
        mtime = 0.0
    if mtime != _alarms_mtime and path.is_file():
        items = []
        try:
            for line in path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    pass
            items.reverse()
            _cached_alarms = items[:100]
            _alarms_mtime = mtime
        except Exception:
            pass
    return list(_cached_alarms)[: max(1, min(int(limit), 100))]


def is_running():
    with _state_lock:
        return _running and _proc is not None and _proc.poll() is None


def start_live_detect():
    """启动检测子进程（不在本进程加载 torch）。"""
    global _proc, _running, _device_id_used, _device_ip, _reconnect_requested
    with _state_lock:
        if _running and _proc is not None and _proc.poll() is None:
            print('[LiveDetect] 已在运行')
            return True
        _running = True
    did = _resolved_device_id()
    row = _load_device_by_device_id(did)
    if not row:
        with _state_lock:
            _running = False
        print(f'[LiveDetect] devices.device_id={did} 不存在')
        return False
    try:
        st = int(os.environ.get('LIVE_DETECT_STREAM_TYPE', '1'))
    except ValueError:
        st = 1
    try:
        rtsp = build_hik_rtsp_from_device(row, stream_type=st)
    except Exception as e:
        with _state_lock:
            _running = False
        print(f'[LiveDetect] 构造 RTSP 失败: {e}')
        return False
    with _state_lock:
        old = _proc
        _proc = None
    if old is not None and old.poll() is None:
        try:
            _runtime_paths()['stop'].write_text('1', encoding='utf-8')
        except Exception:
            pass
        try:
            old.kill()
            old.wait(timeout=1)
        except Exception:
            pass
    paths = _runtime_paths()
    try:
        if paths['stop'].exists():
            paths['stop'].unlink()
    except Exception:
        pass
    model = _resolved_model_path()
    ip = (row.get('ip_address') or '').strip()
    cmd = [sys.executable, '-m', 'app.services.live_detect_worker',
           '--rtsp', rtsp, '--model', str(model), '--zones', str(_ZONES_PATH),
           '--out-dir', str(paths['dir']), '--alarms-dir', str(_alarms_dir()),
           '--stop-file', str(paths['stop']), '--device-id', did, '--device-ip', ip]
    creationflags = 0
    if sys.platform.startswith('win'):
        creationflags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    log_path = paths['dir'] / 'worker.log'
    try:
        log_f = open(log_path, 'w', encoding='utf-8')
    except Exception:
        log_f = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(cmd, cwd=str(_ROOT), stdout=log_f,
                                 stderr=subprocess.STDOUT, creationflags=creationflags)
    except Exception as e:
        print(f'[LiveDetect] 启动子进程失败: {e}')
        try:
            if log_f is not subprocess.DEVNULL:
                log_f.close()
        except Exception:
            pass
        with _state_lock:
            _running = False
        return False
    with _state_lock:
        _proc = proc
        _running = True
        _device_id_used = did
        _device_ip = ip
        _reconnect_requested = False
    try:
        paths['status'].write_text(json.dumps({
            'connection': 'starting', 'resolution': '--', 'display_fps': 0.0,
            'detection_fps': 0.0, 'device': '--', 'model': model.name,
            'error': None, 'device_id': did, 'device_ip': ip,
        }, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass
    print(f'[LiveDetect] 已启动子进程 pid={proc.pid} device_id={did} ip={ip} model={model.name}')
    return True


def stop_live_detect():
    global _proc, _running
    with _state_lock:
        proc = _proc
        _running = False
        _proc = None
    paths = _runtime_paths()
    try:
        paths['stop'].write_text('1', encoding='utf-8')
    except Exception:
        pass
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass
    try:
        if paths['stop'].exists():
            paths['stop'].unlink()
    except Exception:
        pass
    print('[LiveDetect] 已停止')


def reconnect():
    global _reconnect_requested
    with _state_lock:
        if _reconnect_requested:
            return False, 409, '重连进行中'
        _reconnect_requested = True
    try:
        stop_live_detect()
        time.sleep(0.3)
        ok = start_live_detect()
        return (True, 202, 'accepted') if ok else (False, 500, '启动失败')
    finally:
        with _state_lock:
            _reconnect_requested = False


def schedule_live_detect_start(delay_sec=2.0):
    """HTTP 起来后再异步启动子进程。

    必须用 gevent.spawn_later：start_live_detect 会查 DB（psycopg2），
    在 monkey-patch 后的原生线程里访问 DB/socket 会与 gevent hub 死锁，
    表现为登录全面 502。
    """
    raw = os.environ.get('LIVE_DETECT_AUTO_START', 'true').strip().lower()
    if raw not in ('1', 'true', 'yes', 'on'):
        print('[LiveDetect] 已跳过自动启动（LIVE_DETECT_AUTO_START=false）')
        return
    delay = max(0.0, float(delay_sec))

    def _deferred():
        try:
            start_live_detect()
        except Exception as e:
            print(f'[LiveDetect] 延迟启动失败: {e}')

    try:
        import gevent
        gevent.spawn_later(delay, _deferred)
    except Exception as e:
        print(f'[LiveDetect] gevent 不可用，回退同步启动: {e}')
        try:
            _deferred()
        except Exception:
            pass


def mjpeg_generator():
    """multipart/x-mixed-replace MJPEG 生成器。

    只读内存 _jpeg_box（由原生 watcher 线程维护），全程无磁盘 I/O，
    不卡 gevent hub → 不会拖死 /api/live/status、/socket.io 等其它请求。

    关键：不发空 part。第一个 part 必须是真实 JPEG，
    否则浏览器 <img> 会把空 part 当损坏图片触发 onerror → 「实时视频不可用」。
    """
    global _mjpeg_clients
    with _state_lock:
        if _mjpeg_clients >= _MAX_MJPEG_CLIENTS:
            return
        _mjpeg_clients += 1
    boundary = f'--{_MJPEG_BOUNDARY}'.encode('ascii')
    last_sent = 0.0
    last_jpeg = None
    try:
        # 等第一帧（最多 ~6s），期间不发任何 part（避免空 part 触发 onerror）
        first = None
        deadline = time.time() + 6.0
        while time.time() < deadline:
            first = get_latest_jpeg()
            if first:
                break
            time.sleep(0.1)
        if not first:
            return
        last_jpeg = first
        now = time.time()
        last_sent = now
        yield (
            boundary + b'\r\n'
            b'Content-Type: image/jpeg\r\n'
            b'Content-Length: ' + str(len(first)).encode('ascii') + b'\r\n\r\n'
            + first + b'\r\n'
        )
        while True:
            now = time.time()
            jpeg = get_latest_jpeg()
            if jpeg and jpeg is not last_jpeg:
                last_jpeg = jpeg
                last_sent = now
                yield (
                    boundary + b'\r\n'
                    b'Content-Type: image/jpeg\r\n'
                    b'Content-Length: ' + str(len(jpeg)).encode('ascii') + b'\r\n\r\n'
                    + jpeg + b'\r\n'
                )
            elif jpeg and (now - last_sent) >= 1.0:
                # 无新帧时每 ~1s 发一次缓存帧保活，避免灰屏
                yield (
                    boundary + b'\r\n'
                    b'Content-Type: image/jpeg\r\n'
                    b'Content-Length: ' + str(len(jpeg)).encode('ascii') + b'\r\n\r\n'
                    + jpeg + b'\r\n'
                )
                last_sent = now
            try:
                time.sleep(0.05)
            except Exception:
                break
    finally:
        with _state_lock:
            _mjpeg_clients = max(0, _mjpeg_clients - 1)
