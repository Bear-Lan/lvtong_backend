"""X 光机控制器

通过 HTTP 控制 X 光机（200kV / 160kV）。
参考 Qt 中的 HttpImageFetcher / startCaptureStitchImg()。
"""
import requests


class XRayController:
    """X 光机 HTTP 接口封装

    主要接口：
    - start URL: 启动探测（带车长参数）
    - image URL: 持续拉取图像
    - stop URL: 停止探测
    - stitch URL: 获取 200+160 拼接图
    """

    def __init__(self):
        self._base_urls: dict[str, str] = {}

    def configure(self, urls: dict):
        """配置各通道 URL

        urls = {
            'start_transparent': '',  # 200kV 启动
            'start_yrtransparent': '', # 160kV 启动
            'image_transparent': '',  # 200kV 图像
            'image_yrtransparent': '', # 160kV 图像
            'stitch_url': '',          # 拼接图像
        }
        """
        self._base_urls.update(urls)

    def start_scan(self, channel: str, car_length: float, scan_id: str = '') -> bool:
        """启动 X 光扫描

        channel: "200" 或 "160"
        参考 Qt 中 m_transparentImageFetcher / m_yrtransparentImageFetcher 启动逻辑
        """
        if channel == '200':
            key = 'start_transparent'
        elif channel == '160':
            key = 'start_yrtransparent'
        else:
            return False

        url = self._base_urls.get(key, '')
        if not url:
            print(f'[XRAY] 模拟 startScan {channel}kV, carLength={car_length}')
            return True

        full_url = f'{url}{car_length}'
        if scan_id:
            full_url += f'&id={scan_id}'

        try:
            resp = requests.get(full_url, timeout=10)
            return resp.status_code == 200
        except requests.RequestException as e:
            print(f'[XRAY] startScan 失败: {e}')
            return False

    def get_image_url(self, channel: str) -> str:
        """获取 X 光图像拉取 URL

        前端可直接用此 URL 轮询图像。
        """
        if channel == '200':
            return self._base_urls.get('image_transparent', '')
        elif channel == '160':
            return self._base_urls.get('image_yrtransparent', '')
        return ''

    def get_stitch_url(self) -> str:
        """获取拼接图像 URL"""
        return self._base_urls.get('stitch_url', '')
