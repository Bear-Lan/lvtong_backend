"""车顶/车侧同帧点云投影；示范回退只用于显示，明确标注来源。"""
from pathlib import Path

from app.services.middleware_http import middleware_get_payload, _run_in_pool
from app.services.radar_point_cloud import decode_body_radar_data
from app.services.temp_image_cache import put_and_url

SAMPLE_PATH = Path(__file__).resolve().parents[2] / 'samples' / 'vehicle.laz'


def fetch_body_projections(settings: dict) -> dict:
    url = settings['RADAR_POINT_CLOUD_URL']
    source, detail = 'live', ''
    try:
        response = middleware_get_payload(url, timeout=(3, 8))
        if response.status_code != 200:
            raise ValueError(f'点云接口 HTTP {response.status_code}')
        frames, geometry = _run_in_pool(lambda: decode_body_radar_data(response.content, settings))
    except Exception as error:
        if not settings.get('RADAR_POINT_CLOUD_SAMPLE_FALLBACK'):
            raise
        source, detail = 'sample', str(error)[:300]
        frames, geometry = _run_in_pool(lambda: decode_body_radar_data(SAMPLE_PATH.read_bytes(), settings))
    views = {}
    for view, frame in frames.items():
        # 保留原始投影尺寸与无损PNG，避免缩略/有损压缩改变点云测量比例。
        views[view] = {'imageUrl': put_and_url(frame.image, 'image/png'),
                       'metersPerPixel': frame.metadata['metersPerPixel'],
                       'coordinateAxes': frame.metadata['coordinateAxes']}
    return {'views': views, 'source': source, 'sourceLabel': '示范数据' if source == 'sample' else '实时点云',
            'fallbackReason': detail, 'pointCloud': frames['side'].metadata, 'geometry': geometry}
