"""真实 LAZ 同帧投影与三维数据；无数据时等待，不回退演示文件。"""

from app.services.middleware_http import middleware_get_payload, _run_in_pool
from app.services.radar_point_cloud import decode_body_radar_data
from app.services.temp_image_cache import put_and_url

def fetch_body_projections(settings: dict) -> dict:
    response = middleware_get_payload(settings['RADAR_POINT_CLOUD_URL'], timeout=(3, 8))
    return project_body_response(response, settings)


def project_body_response(response, settings: dict) -> dict:
    if response.status_code != 200:
        raise ValueError(f'点云接口 HTTP {response.status_code}')
    frames, geometry = _run_in_pool(lambda: decode_body_radar_data(response.content, settings))
    views = {}
    for view, frame in frames.items():
        # 保留原始投影尺寸与无损PNG，避免缩略/有损压缩改变点云测量比例。
        views[view] = {'imageUrl': put_and_url(frame.image, 'image/png'),
                       'metersPerPixel': frame.metadata['metersPerPixel'],
                       'coordinateAxes': frame.metadata['coordinateAxes']}
    return {'views': views, 'source': 'live', 'sourceLabel': '实时点云',
            'pointCloud': frames['side'].metadata, 'geometry': geometry}
