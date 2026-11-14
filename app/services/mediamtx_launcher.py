"""从 devices 表按 device_id 同步 RTSP 到 MediaMTX 配置，并拉起进程。

- paths.cam1 ← LIVE_VIDEO_DEVICE_ID（默认 cam_lane / 103）→ 图像采集枪机
- paths.cam2 ← HEAD_VIDEO_DEVICE_ID（默认 cam_head / 102）→ Pad 主页面/车头采集
- paths.cam3 ← SAFE_VIDEO_DEVICE_ID（默认 cam_safe / 105）→ Pad 周界相机弹窗
- paths.cam4 ← TALK_VIDEO_DEVICE_ID（默认 cam_booking / 101）→ 预约 / 可视对讲
- paths.cam5 ← PTZ_VIDEO_DEVICE_ID（默认 cam_ptz / 104）→ Pad 球机预览
- paths.cam6 ← ROAD_VIDEO_DEVICE_ID（默认 cam_road / 107）→ Pad 车顶采集
"""
from __future__ import annotations

import atexit
import os
import re
import subprocess
import sys
import signal
from pathlib import Path
from urllib.parse import quote
from app.services.camera_stream import rtsp_channel

_proc: subprocess.Popen | None = None

_MTX_DIR = Path(__file__).resolve().parents[2] / 'tools' / 'mediamtx'
_DEFAULT_LIVE_DEVICE_ID = 'cam_lane'
_DEFAULT_HEAD_DEVICE_ID = 'cam_head'
_DEFAULT_SAFE_DEVICE_ID = 'cam_safe'
_DEFAULT_TALK_DEVICE_ID = 'cam_booking'
_DEFAULT_PTZ_DEVICE_ID = 'cam_ptz'
_DEFAULT_ROAD_DEVICE_ID = 'cam_road'


def _bin_path() -> Path:
    if sys.platform.startswith('win'):
        return _MTX_DIR / 'mediamtx.exe'
    return _MTX_DIR / 'mediamtx'


def _cfg_path() -> Path:
    return _MTX_DIR / 'mediamtx.local.yml'


def _runtime_cfg_path() -> Path:
    """模板保留相机源；运行文件使用 H.265 → H.264 按需转码。"""
    import yaml
    from app.services.video_transcode import build_transcode_config, choose_encoder, ffmpeg_path

    base = yaml.safe_load(_cfg_path().read_text(encoding='utf8'))
    ffmpeg = ffmpeg_path()
    encoder = choose_encoder(ffmpeg)
    generated = build_transcode_config(
        base, ffmpeg, encoder,
        width=int(os.environ.get('VIDEO_MAX_WIDTH', '1280')),
        fps=int(os.environ.get('VIDEO_FPS', '15')),
    )
    # 多网卡 + Tailscale 时，避免公网 IPv6 与隧道地址组成不可传媒体的候选对。
    # 各现场显式提供 LAN/Tailscale IPv4，未配置时沿用模板的候选设置。
    from app.services.video_transcode import configure_webrtc_hosts
    configure_webrtc_hosts(generated, os.environ.get('VIDEO_WEBRTC_HOSTS', ''))
    target = _MTX_DIR / 'mediamtx.runtime.yml'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(yaml.safe_dump(generated, allow_unicode=True, sort_keys=False), encoding='utf8')
    temporary.replace(target)
    print(f'[MediaMTX] 按需 H.264 转码 encoder={encoder}，无人观看10秒后释放')
    return target


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
    st = int(stream_type)
    if st not in (1, 2):
        st = 1
    channel_num = rtsp_channel(row, st)
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

    # 照片采集的五路相机只传子码流；cam3 周界相机沿用原设置。
    stream_type = _stream_type() if path_name == 'cam3' else 2
    rtsp = build_hik_rtsp_from_device(row, stream_type=stream_type)
    if not _write_path_source(path_name, rtsp):
        return None

    ip = row.get('ip_address')
    print(
        f'[MediaMTX] 已从 DB 同步 RTSP: path={path_name} '
        f'device_id={device_id} ip={ip} WHEP=/mtx/{path_name}/whep'
    )
    return rtsp


def sync_cam1_source_from_db(device_id: str | None = None) -> str | None:
    """图像采集枪机：device_id → paths.cam1.source（默认 cam_lane）。"""
    did = (device_id or os.environ.get('LIVE_VIDEO_DEVICE_ID') or _DEFAULT_LIVE_DEVICE_ID).strip()
    return _sync_path_from_device_id('cam1', did)


def sync_cam2_source_from_db(device_id: str | None = None) -> str | None:
    """Web 主页面车头相机：device_id → paths.cam2.source（默认 cam_head）。"""
    did = (device_id or os.environ.get('HEAD_VIDEO_DEVICE_ID') or _DEFAULT_HEAD_DEVICE_ID).strip()
    return _sync_path_from_device_id('cam2', did)


def sync_cam3_source_from_db(device_id: str | None = None) -> str | None:
    """Web 周界相机 A：device_id → paths.cam3.source（默认 cam_safe）。"""
    did = (device_id or os.environ.get('SAFE_VIDEO_DEVICE_ID') or _DEFAULT_SAFE_DEVICE_ID).strip()
    return _sync_path_from_device_id('cam3', did)


def sync_cam4_source_from_db(device_id: str | None = None) -> str | None:
    """预约/可视对讲：device_id → paths.cam4.source（默认 cam_booking）。"""
    did = (device_id or os.environ.get('TALK_VIDEO_DEVICE_ID') or _DEFAULT_TALK_DEVICE_ID).strip()
    return _sync_path_from_device_id('cam4', did)


def sync_cam5_source_from_db(device_id: str | None = None) -> str | None:
    """Pad 球机：device_id → paths.cam5.source（默认 cam_ptz）。"""
    did = (device_id or os.environ.get('PTZ_VIDEO_DEVICE_ID') or _DEFAULT_PTZ_DEVICE_ID).strip()
    return _sync_path_from_device_id('cam5', did)


def sync_cam6_source_from_db(device_id: str | None = None) -> str | None:
    """Pad 车顶相机：device_id → paths.cam6.source（默认 cam_road）。"""
    did = (device_id or os.environ.get('ROAD_VIDEO_DEVICE_ID') or _DEFAULT_ROAD_DEVICE_ID).strip()
    return _sync_path_from_device_id('cam6', did)


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
        sync_cam2_source_from_db()
    except Exception as e:
        print(f'[MediaMTX] 同步 cam2 RTSP 失败: {e}')

    try:
        sync_cam3_source_from_db()
    except Exception as e:
        print(f'[MediaMTX] 同步 cam3 RTSP 失败: {e}')

    try:
        sync_cam4_source_from_db()
    except Exception as e:
        print(f'[MediaMTX] 同步 cam4 RTSP 失败: {e}')

    try:
        sync_cam5_source_from_db()
    except Exception as e:
        print(f'[MediaMTX] 同步 cam5 RTSP 失败: {e}')

    try:
        sync_cam6_source_from_db()
    except Exception as e:
        print(f'[MediaMTX] 同步 cam6 RTSP 失败: {e}')

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

    try:
        cfg = _runtime_cfg_path()
        _stop_foreign_mediamtx()
        creationflags = 0
        if sys.platform.startswith('win'):
            creationflags = (getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
                             | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        log_dir = _MTX_DIR.parents[1] / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / 'mediamtx.log'
        if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
            log_path.replace(log_path.with_suffix('.log.1'))
        with log_path.open('ab') as log:
            _proc = subprocess.Popen(
                [str(bin_path), str(cfg)], cwd=str(_MTX_DIR),
                stdout=log, stderr=subprocess.STDOUT, creationflags=creationflags,
                start_new_session=not sys.platform.startswith('win'),
            )
        atexit.register(stop_mediamtx)
        print(
            f'[MediaMTX] 已启动 pid={_proc.pid}  '
            f'WHEP=http://127.0.0.1:8889/cam1/whep  '
            f'http://127.0.0.1:8889/cam2/whep  '
            f'http://127.0.0.1:8889/cam3/whep  '
            f'http://127.0.0.1:8889/cam4/whep  '
            f'http://127.0.0.1:8889/cam5/whep  '
            f'http://127.0.0.1:8889/cam6/whep'
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
            if sys.platform.startswith('win'):
                # 只清理本次启动的 MediaMTX 进程树，包含其按需 FFmpeg 子进程。
                subprocess.run(['taskkill', '/PID', str(_proc.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=8, check=False,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            else:
                os.killpg(_proc.pid, signal.SIGTERM)
            try:
                _proc.wait(timeout=5)
            except Exception:
                _proc.kill()
            print('[MediaMTX] 已停止')
    except Exception as e:
        print(f'[MediaMTX] 停止异常: {e}')
    finally:
        _proc = None
