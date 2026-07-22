"""设备 & 外部服务 URL 配置 — 对齐 Qt config/app.json"""
import os

# ---- 雷达 ----
RADAR_HEAD_URL = os.environ.get(
    'RADAR_HEAD_URL', 'http://192.168.88.201:5001/radar/image'
)

# ---- X 光机 ----
XRAY_BASE_URL = os.environ.get(
    'XRAY_BASE_URL', 'http://192.168.88.201:5006'
)
XRAY_START_TRANSPARENT_URL = os.environ.get(
    'XRAY_START_TRANSPARENT_URL',
    'http://192.168.88.201:5003/xray/capture?frames=500&triggeroffset=2.5&id='
)
XRAY_START_YR_TRANSPARENT_URL = os.environ.get(
    'XRAY_START_YR_TRANSPARENT_URL',
    'http://192.168.88.201:5006/xray/capture?frames=500&triggeroffset=2.5&id='
)

# ---- 车身影像 ----
BODY_IMAGE_URL = os.environ.get(
    'BODY_IMAGE_URL', 'http://192.168.88.201:5002/body/image'
)

# ---- 透视影像 ----
TRANSPARENT_IMAGE_URL = os.environ.get(
    'TRANSPARENT_IMAGE_URL', 'http://192.168.88.201:5003/xray/image?id='
)
YR_TRANSPARENT_IMAGE_URL = os.environ.get(
    'YR_TRANSPARENT_IMAGE_URL', 'http://192.168.88.201:5006/xray/image?id='
)

# ---- 拼接图像 ----
STITCH_IMAGE_URL = os.environ.get(
    'STITCH_IMAGE_URL', 'http://192.168.88.201:5006/xray/stitch?id='
)

# ---- AI / OCR ----
AI_MODEL_URL = os.environ.get(
    'AI_MODEL_URL', 'http://192.168.88.245:8899'
)
OCR_SERVICE_URL = os.environ.get(
    'OCR_SERVICE_URL', 'http://192.168.88.245:8890'
)

# ---- PLC / 门禁 / LED / TTS ----
PLC_CONTROL_URL = os.environ.get('PLC_CONTROL_URL', '')
GATE_CONTROL_URL = os.environ.get('GATE_CONTROL_URL', '')
LED_CONTROL_URL = os.environ.get('LED_CONTROL_URL', '')
TTS_CONTROL_URL = os.environ.get('TTS_CONTROL_URL', '')

# ---- 图像存储 ----
IMAGE_STORAGE_ROOT = os.environ.get(
    'IMAGE_STORAGE_ROOT', r'E:/code_product/gcms_src/captures'
)
