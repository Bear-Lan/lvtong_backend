"""从 devices 表按 device_id 同步 RTSP 到 MediaMTX 配置，并拉起进程。

- paths.cam1 ← LIVE_VIDEO_DEVICE_ID（默认 camera2）→ 主页实时视频
- paths.cam4 ← TALK_VIDEO_DEVICE_ID（默认 camera4）→ 预约 / 可视对讲
"""
from __future__ import annotations

import atexit
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

_proc: subprocess.Popen | None = None

_MTX_DIR = Path(__file__).resolve().parents[2] / 'tools' / 'mediamtx'
_DEFAULT_LIVE_DEVICE_ID = 'camera2'
_DEFAULT_TALK_DEVICE_ID = 'camera4'


def _bin_path() -> Path:
    if sys.platform.startswith('win'):
        return _MTX_DIR / 'mediamtx.exe'
    return _MTX_DIR / 'mediamtx'


def _cfg_path() -> Path:
    return _MTX_DIR / 'mediamtx.local.yml'


def is_enabled() -> bool:
    raw = os.environ.get('MEDIAMTX_AUTO_START', 'true').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def _load_device_by_device_id(device_id: str) -> dict | None:
    from sqlalchemy import text
    from app.db.engine import engine

    did = (device_id or '').strip()
    if not did:
        return None

    with engine.connect() as conn:
        row = conn.execute(
            text(
                'SELECT id, device_id, device_name, ip_address, port, username, password, config '
                'FROM devices WHERE device_id = :did'
            ),
            {'did': did},
        ).mappings().first()
    return dict(row) if row else None


def build_hik_rtsp_from_device(row: dict, *, stream_type: int = 1) -> str:
    """海康 RTSP：Channels/{channel*100+streamType}，默认通道1主码流 → 101。"""
    ip = (row.get('ip_address') or '').strip()
    if not ip:
        raise ValueError('设备未配置 IP')
    user = quote((row.get('username') or 'admin').strip(), safe='')
    pwd = quote((row.get('password') or ''), safe='')
    cfg = row.get('config') or {}
    if isinstance(cfg, str):
        import json
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


def _write_path_source(path_name: str, rtsp: str) -> bool:
    """只改 paths.<path_name> 块内的 source:，不碰其它 path。"""
    cfg_path = _cfg_path()
    if not cfg_path.is_file():
        print(f'[MediaMTX] 缺少配置文件: {cfg_path}')
        return False

    text = cfg_path.read_text(encoding='utf-8')
    # 匹配 "  cam1:\n    source: ..." 这类块内第一处 source
    pattern = re.compile(
        rf'(?m)^(\s*{re.escape(path_name)}:\s*\n(?:.*\n)*?\s+source:\s*).+$'
    )
    new_text, n = pattern.subn(r'\g<1>' + rtsp, text, count=1)
    if n == 0:
        print(f'[MediaMTX] 未能写入 {path_name}.source，请检查 yml 结构')
        return False

    cfg_path.write_text(new_text, encoding='utf-8')
    return True


def _stream_type() -> int:
    return int(os.environ.get('LIVE_VIDEO_STREAM_TYPE', '1'))


def _sync_path_from_device_id(path_name: str, device_id: str) -> str | None:
    row = _load_device_by_device_id(device_id)
    if not row:
        print(f'[MediaMTX] devices.device_id={device_id} 不存在，跳过 {path_name} 同步')
        return None

    rtsp = build_hik_rtsp_from_device(row, stream_type=_stream_type())
    if not _write_path_source(path_name, rtsp):
        return None

    ip = row.get('ip_address')
    print(
        f'[MediaMTX] 已从 DB 同步 RTSP: path={path_name} '
        f'device_id={device_id} ip={ip} WHEP=/mtx/{path_name}/whep'
    )
    return rtsp


def sync_cam1_source_from_db(device_id: str | None = None) -> str | None:
    """主页实时：device_id → paths.cam1.source（默认 camera2）。"""
    did = (device_id or os.environ.get('LIVE_VIDEO_DEVICE_ID') or _DEFAULT_LIVE_DEVICE_ID).strip()
    return _sync_path_from_device_id('cam1', did)


def sync_cam4_source_from_db(device_id: str | None = None) -> str | None:
    """预约/可视对讲：device_id → paths.cam4.source（默认 camera4）。"""
    did = (device_id or os.environ.get('TALK_VIDEO_DEVICE_ID') or _DEFAULT_TALK_DEVICE_ID).strip()
    return _sync_path_from_device_id('cam4', did)


def _stop_foreign_mediamtx() -> None:
    """启动前清掉已有 MediaMTX，避免端口占用或沿用旧配置。"""
    global _proc
    if _proc is not None and _proc.poll() is None:
        return

    killed = False
    if sys.platform.startswith('win'):
        try:
            r = subprocess.run(
                ['taskkill', '/F', '/IM', 'mediamtx.exe'],
                capture_output=True,
                text=True,
                check=False,
            )
            killed = r.returncode == 0
        except Exception:
            pass
    else:
        try:
            subprocess.run(['pkill', '-f', 'mediamtx'], check=False)
            killed = True
        except Exception:
            pass

    if killed:
        print('[MediaMTX] 已清理旧 MediaMTX 进程')


def start_mediamtx() -> bool:
    """同步 DB 相机 → 若开启自动启动且二进制存在，则后台拉起 MediaMTX。"""
    global _proc
    if not is_enabled():
        print('[MediaMTX] 已跳过自动启动（MEDIAMTX_AUTO_START=false）')
        return False

    try:
        sync_cam1_source_from_db()
    except Exception as e:
        print(f'[MediaMTX] 同步 cam1 RTSP 失败: {e}')

    try:
        sync_cam4_source_from_db()
    except Exception as e:
        print(f'[MediaMTX] 同步 cam4 RTSP 失败: {e}')

    if _proc is not None and _proc.poll() is None:
        print('[MediaMTX] 已在运行')
        return True

    bin_path = _bin_path()
    cfg = _cfg_path()
    if not cfg.is_file():
        print(f'[MediaMTX] 缺少配置: {cfg}')
        return False

    if not bin_path.is_file():
        print(
            f'[MediaMTX] 未找到 {bin_path.name}，'
            f'请将对应平台二进制放到 tools/mediamtx/ '
            f'(Windows: mediamtx.exe；Linux: mediamtx，建议 v1.19.3)'
        )
        return False

    _stop_foreign_mediamtx()

    try:
        creationflags = 0
        if sys.platform.startswith('win'):
            creationflags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        _proc = subprocess.Popen(
            [str(bin_path), str(cfg)],
            cwd=str(_MTX_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        atexit.register(stop_mediamtx)
        print(
            f'[MediaMTX] 已启动 pid={_proc.pid}  '
            f'WHEP=http://127.0.0.1:8889/cam1/whep  '
            f'http://127.0.0.1:8889/cam4/whep'
        )
        return True
    except Exception as e:
        print(f'[MediaMTX] 启动失败: {e}')
        _proc = None
        return False


def stop_mediamtx() -> None:
    global _proc
    if _proc is None:
        return
    try:
        if _proc.poll() is None:
            _proc.terminate()
            try:
                _proc.wait(timeout=5)
            except Exception:
                _proc.kill()
            print('[MediaMTX] 已停止')
    except Exception as e:
        print(f'[MediaMTX] 停止异常: {e}')
    finally:
        _proc = None
