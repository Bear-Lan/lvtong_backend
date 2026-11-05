"""生成 MediaMTX 按需 H.264 转码配置；摄像头参数保持原样。"""
from __future__ import annotations

import copy
import ipaddress
import os
from pathlib import Path
import shlex
import shutil
import subprocess

CAMERA_PATHS = tuple(f'cam{i}' for i in range(1, 7))


def configure_webrtc_hosts(config: dict, hosts: str) -> None:
    if not hosts.strip():
        return
    addresses = list(dict.fromkeys(item.strip() for item in hosts.split(',') if item.strip()))
    if not addresses or any(ipaddress.ip_address(address).version != 4 for address in addresses):
        raise ValueError('VIDEO_WEBRTC_HOSTS 必须为逗号分隔的现场 LAN/Tailscale IPv4')
    config.update(webrtcIPsFromInterfaces=False, webrtcAdditionalHosts=addresses,
                  webrtcLocalUDPAddress='0.0.0.0:8189', webrtcLocalTCPAddress='0.0.0.0:8189',
                  udpMaxPayloadSize=1180)


def ffmpeg_path() -> str:
    configured = os.environ.get('FFMPEG_EXE', '').strip()
    if configured:
        if not Path(configured).is_file():
            raise ValueError('FFMPEG_EXE 文件不存在')
        return str(Path(configured).resolve())
    found = shutil.which('ffmpeg')
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def choose_encoder(ffmpeg: str) -> str:
    requested = os.environ.get('VIDEO_ENCODER', 'auto').strip().lower()
    if requested not in ('auto', 'libx264', 'h264_nvenc'):
        raise ValueError('VIDEO_ENCODER 只能为 auto/libx264/h264_nvenc')
    if requested == 'libx264':
        return requested
    # 检测真实驱动可用性，而不只看 FFmpeg 是否列出 nvenc。
    try:
        result = subprocess.run(
            [ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
             'color=size=128x128:rate=1', '-frames:v', '1', '-an', '-c:v',
             'h264_nvenc', '-f', 'null', '-'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=12,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if result.returncode == 0:
            return 'h264_nvenc'
    except (OSError, subprocess.TimeoutExpired):
        pass
    if requested == 'h264_nvenc':
        raise RuntimeError('现场 NVIDIA 硬件编码测试未通过')
    return 'libx264'


def transcode_args(ffmpeg: str, source_url: str, output_url: str, *, encoder: str,
                   width: int = 1280, fps: int = 15) -> list[str]:
    if encoder not in ('libx264', 'h264_nvenc'):
        raise ValueError('不支持的 H.264 编码器')
    if not 320 <= width <= 3840 or width % 2 or not 1 <= fps <= 30:
        raise ValueError('VIDEO_MAX_WIDTH 必须为320～3840偶数，VIDEO_FPS为1～30')
    args = [ffmpeg, '-hide_banner', '-loglevel', 'warning', '-nostdin',
            '-rtsp_transport', 'tcp', '-timeout', '15000000', '-threads', '2', '-i', source_url,
            '-map', '0:v:0', '-an', '-sn', '-dn',
            '-vf', f"fps={fps},scale=w='min({width},iw)':h=-2,format=yuv420p",
            '-c:v', encoder, '-profile:v', 'baseline', '-bf', '0', '-g', str(fps),
            '-b:v', '1800k', '-maxrate', '2500k', '-bufsize', '2500k']
    if encoder == 'libx264':
        args += ['-preset', 'ultrafast', '-tune', 'zerolatency', '-threads', '2']
    else:
        args += ['-preset', 'p1', '-tune', 'ull', '-rc', 'cbr', '-zerolatency', '1']
    return args + ['-f', 'rtsp', '-rtsp_transport', 'tcp', output_url]


def build_transcode_config(base: dict, ffmpeg: str, encoder: str, *,
                           width: int = 1280, fps: int = 15) -> dict:
    """同路多读者共享一个编码进程；无人观看10秒后由 MediaMTX 回收。"""
    result = copy.deepcopy(base)
    rtsp_address = str(result.get('rtspAddress', ':8554'))
    port = int(rtsp_address.rsplit(':', 1)[-1])
    paths = result.setdefault('paths', {})
    for name in CAMERA_PATHS:
        source = paths.get(name, {}).get('source')
        if not isinstance(source, str) or not source.startswith(('rtsp://', 'rtsps://')):
            raise ValueError(f'{name} 缺少有效相机 RTSP source，拒绝生成不完整转码配置')
        raw_name = name + '_input'
        if raw_name in paths:
            raise ValueError(f'{raw_name} 路径冲突')
        raw = copy.deepcopy(paths[name])
        raw.update(sourceOnDemand=True, sourceOnDemandStartTimeout='20s',
                   sourceOnDemandCloseAfter='10s')
        paths[raw_name] = raw
        args = transcode_args(ffmpeg, f'rtsp://127.0.0.1:{port}/{raw_name}',
                              f'rtsp://127.0.0.1:{port}/{name}', encoder=encoder,
                              width=width, fps=fps)
        paths[name] = {
            'source': 'publisher', 'sourceOnDemand': False,
            # MediaMTX 在 Windows 也先用 shellquote 分词，不能用 list2cmdline，
            # 否则 Windows 路径中的反斜杠及 FFmpeg 滤镜引号会被吃掉。
            'runOnDemand': shlex.join(args),
            'runOnDemandRestart': True, 'runOnDemandStartTimeout': '30s',
            'runOnDemandCloseAfter': '10s',
        }
    return result
