"""LAS/LAZ → 正交 PNG；预览坐标比例保持一致，不把点云包络当车辆测量值。"""
from __future__ import annotations

from dataclasses import dataclass
import io
import json
import math

import cv2
import laspy
import numpy as np
from requests.structures import CaseInsensitiveDict

MAX_POINTS = 10_000_000
MAX_RENDER_POINTS = 500_000
MAX_EDGE = 4096
MAX_DECODED_BYTES = 512 * 1024 * 1024


@dataclass
class RadarFrame:
    image: bytes
    headers: CaseInsensitiveDict
    metadata: dict


def _positive(value, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f'{name} 必须为有限正数')
    return number


def decode_radar_payload(data: bytes, headers=None, settings=None) -> RadarFrame:
    settings = settings or {}
    headers = CaseInsensitiveDict(headers or {})
    if not data:
        raise ValueError('雷达尚未返回数据')
    if data.lstrip().startswith((b'{', b'[', b'\xef\xbb\xbf')):
        try:
            obj = json.loads(data)
        except (ValueError, UnicodeError) as error:
            raise ValueError('雷达返回了无效 JSON，未取得点云') from error
        detail = ''
        if isinstance(obj, dict):
            detail = str(obj.get('error') or obj.get('message') or '')[:200]
        raise ValueError('雷达尚未生成有效点云' + (f'：{detail}' if detail else ''))
    if data.startswith(b'LASF'):
        return _decode_las(data, headers, settings)
    # 保留已配置的旧图像接口，但必须真能解码，不能缓存错误页为图片。
    if not data.startswith((b'\xff\xd8', b'\x89PNG', b'BM', b'RIFF')):
        raise ValueError('雷达响应不是 LAS/LAZ 或有效图像')
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('雷达图像损坏')
    return RadarFrame(data, headers, {'sourceFormat': 'image'})


def _decode_las(data: bytes, headers, settings) -> RadarFrame:
    axes = str(settings.get('RADAR_LAZ_AXES', 'xz')).lower()
    if len(axes) != 2 or any(a not in 'xyz' for a in axes) or axes[0] == axes[1]:
        raise ValueError('RADAR_LAZ_AXES 必须为两个不同的 xyz 坐标轴')
    unit = _positive(settings.get('RADAR_LAZ_METERS_PER_UNIT', 1), '点云单位比例')
    resolution = _positive(settings.get('RADAR_LAZ_RESOLUTION', .01), '点云分辨率')
    cloud, lows, highs, count, compressed = _read_las(data, unit)
    return _project_las(cloud, lows, highs, count, compressed, headers, axes, unit, resolution)


def _read_las(data: bytes, unit: float):
    lows = np.full(3, np.inf)
    highs = np.full(3, -np.inf)
    samples = []
    try:
        with laspy.open(io.BytesIO(data), read_evlrs=False) as reader:
            header = reader.header
            count = header.point_count
            compressed = header.are_points_compressed
            if not 0 < count <= MAX_POINTS:
                raise ValueError(f'点云点数为空或超出限制（{count}）')
            if count * header.point_format.size > MAX_DECODED_BYTES:
                raise ValueError('点云解压后大小超出限制')
            if not np.all(np.isfinite(header.scales)) or np.any(header.scales <= 0):
                raise ValueError('点云比例无效')
            if not np.all(np.isfinite(header.offsets)):
                raise ValueError('点云偏移无效')
            stride = max(1, math.ceil(count / MAX_RENDER_POINTS))
            seen, valid = 0, 0
            for points in reader.chunk_iterator(50_000):
                xyz = np.column_stack((points.x, points.y, points.z)) * unit
                mask = np.isfinite(xyz).all(axis=1)
                good = xyz[mask]
                if len(good):
                    lows = np.minimum(lows, good.min(axis=0))
                    highs = np.maximum(highs, good.max(axis=0))
                    valid += len(good)
                # 全局固定步长采样，内存不随输入点数无限增长。
                start = (-seen) % stride
                selected = xyz[start::stride]
                selected = selected[np.isfinite(selected).all(axis=1)]
                if len(selected):
                    samples.append(selected)
                seen += len(points)
            if seen != count:
                raise ValueError(f'点云记录不完整：声明 {count} 点，解出 {seen} 点')
            if not valid or not samples:
                raise ValueError('点云没有有效坐标')
    except Exception as error:
        raise ValueError(f'LAS/LAZ 解码失败：{error}') from error

    cloud = np.concatenate(samples)
    return cloud, lows, highs, count, compressed


def _project_las(cloud, lows, highs, count, compressed, headers, axes, unit, resolution):
    headers = headers.copy()
    horizontal, vertical = ('xyz'.index(a) for a in axes)
    span = highs - lows
    if not np.isfinite(span).all():
        raise ValueError('点云坐标范围无效')
    # 同比例缩放两轴；原图超过上限时同步更新米/像素，保证拖线换算一致。
    resolution = max(resolution, float(max(span[horizontal], span[vertical])) / (MAX_EDGE - 1))
    width = min(MAX_EDGE, max(2, math.ceil(span[horizontal] / resolution) + 1))
    height = min(MAX_EDGE, max(2, math.ceil(span[vertical] / resolution) + 1))
    px = np.clip(np.rint((cloud[:, horizontal] - lows[horizontal]) / resolution), 0, width - 1).astype(int)
    py = np.clip(np.rint((highs[vertical] - cloud[:, vertical]) / resolution), 0, height - 1).astype(int)
    image = np.full((height, width, 3), 255, np.uint8)
    image[py, px] = (140, 85, 20)
    encoded, png = cv2.imencode('.png', image)
    if not encoded:
        raise ValueError('点云预览编码失败')
    # 上游图像像素包络不能复用于新投影。保留物理测量头，不凭点云范围推断车高。
    headers.pop('Image-Envelope', None)
    headers.pop('VehicleHeader-Envelope', None)
    headers['Image-Resolution'] = str(resolution)
    return RadarFrame(png.tobytes(), headers, {
        'sourceFormat': 'laz' if compressed else 'las',
        'pointCount': count, 'renderedPointCount': len(cloud),
        'coordinateAxes': axes, 'metersPerUnit': unit,
        'boundsMeters': {'min': lows.tolist(), 'max': highs.tolist()},
        'metersPerPixel': resolution,
    })


def decode_radar_payload_in_pool(data: bytes, headers=None, settings=None) -> RadarFrame:
    # lazrs/numpy/cv2 的 CPU 工作放到真实线程，避免占用距离 WS 的 greenlet。
    from app.services.middleware_http import _run_in_pool
    return _run_in_pool(lambda: decode_radar_payload(data, headers, settings))


def decode_body_projection_pair(data: bytes, settings=None) -> dict[str, RadarFrame]:
    """同一份LAS/LAZ只解压一次，XY俯视、XZ侧视；两图保持相同米/像素。"""
    if not data.startswith(b'LASF'):
        # 复用无数据/错误JSON的明确错误信息，不把图像接口误当成点云。
        decode_radar_payload(data)
        raise ValueError('车顶车侧投影需要 LAS/LAZ 点云文件')
    settings = settings or {}
    unit = _positive(settings.get('RADAR_LAZ_METERS_PER_UNIT', 1), '点云单位比例')
    resolution = _positive(settings.get('RADAR_LAZ_RESOLUTION', .01), '点云分辨率')
    cloud, lows, highs, count, compressed = _read_las(data, unit)
    resolution = max(resolution, float(max(highs - lows)) / (MAX_EDGE - 1))
    return {view: _project_las(cloud, lows, highs, count, compressed, CaseInsensitiveDict(), axes, unit, resolution)
            for view, axes in [('top', 'xy'), ('side', 'xz')]}
