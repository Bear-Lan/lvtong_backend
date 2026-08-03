"""从 devices 表同步主页实时视频 RTSP 到 MediaMTX 配置，并拉起进程。"""
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
# 默认连 public.devices.id = 21（camera2，192.168.88.101）
_DEFAULT_DEVICE_PK = 21


def _bin_path() -> Path:
    if sys.platform.startswith('win'):
        return _MTX_DIR / 'mediamtx.exe'
    return _MTX_DIR / 'mediamtx'


def _cfg_path() -> Path:
    return _MTX_DIR / 'mediamtx.local.yml'


def is_enabled() -> bool:
    raw = os.environ.get('MEDIAMTX_AUTO_START', 'true').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def _load_device_row(device_pk: int) -> dict | None:
    from sqlalchemy import text
    from app.db.engine import engine

    with engine.connect() as conn:
        row = conn.execute(
            text(
                'SELECT id, device_id, device_name, ip_address, port, username, password, config '
                'FROM devices WHERE id = :pk'
            ),
            {'pk': device_pk},
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


def sync_cam1_source_from_db(device_pk: int | None = None) -> str | None:
    """把 devices.id 对应相机的 RTSP 写入 mediamtx.local.yml 的 paths.cam1.source。"""
    pk = device_pk
    if pk is None:
        pk = int(os.environ.get('LIVE_VIDEO_DEVICE_PK', str(_DEFAULT_DEVICE_PK)))

    row = _load_device_row(pk)
    if not row:
        print(f'[MediaMTX] devices.id={pk} 不存在，跳过 RTSP 同步')
        return None

    # 主页预览可用子码流压延时：LIVE_VIDEO_STREAM_TYPE=2
    stream_type = int(os.environ.get('LIVE_VIDEO_STREAM_TYPE', '1'))
    rtsp = build_hik_rtsp_from_device(row, stream_type=stream_type)
    cfg_path = _cfg_path()
    if not cfg_path.is_file():
        print(f'[MediaMTX] 缺少配置文件: {cfg_path}')
        return None

    text = cfg_path.read_text(encoding='utf-8')
    new_text, n = re.subn(
        r'(?m)^(\s+source:\s*).+$',
        r'\g<1>' + rtsp,
        text,
        count=1,
    )
    if n == 0:
        print('[MediaMTX] 未能写入 cam1.source，请检查 yml 结构')
        return None

    cfg_path.write_text(new_text, encoding='utf-8')
    ip = row.get('ip_address')
    did = row.get('device_id')
    print(f'[MediaMTX] 已从 DB 同步 RTSP: devices.id={pk} ({did}) ip={ip}')
    return rtsp


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
        print(f'[MediaMTX] 同步 DB RTSP 失败: {e}')

    if _proc is not None and _proc.poll() is None:
        print('[MediaMTX] 已在运行')
        return True

    bin_path = _bin_path()
    cfg = _cfg_path()
    if not cfg.is_file():
        print(f'[MediaMTX] 缺少配置: {cfg}')
        return False

    if not bin_path.is_file():
        print(f'[MediaMTX] 未找到 {bin_path.name}，请将 mediamtx.exe 放到 tools/mediamtx/')
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
        # 再读一遍确认配置 IP，避免看错文件
        try:
            yml = cfg.read_text(encoding='utf-8')
            m = re.search(r'@([0-9.]+):554', yml)
            ip_hint = m.group(1) if m else '?'
        except Exception:
            ip_hint = '?'
        print(
            f'[MediaMTX] 已启动 pid={_proc.pid}  source_ip={ip_hint}  '
            f'WHEP=http://127.0.0.1:8889/cam1/whep'
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
