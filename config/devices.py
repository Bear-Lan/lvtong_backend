"""设备 & 外部服务 URL 配置 — 对齐 Qt config/app.json"""
import os

# ---- 临时占位出图（现场中间层未齐时，六类图共用同一张）----
# 200/160 透视、拼接、车身/点云、预约雷达 均暂用此链接
PLACEHOLDER_IMAGE_ID = os.environ.get('PLACEHOLDER_IMAGE_ID', '20260706183600')
PLACEHOLDER_IMAGE_URL = os.environ.get(
    'PLACEHOLDER_IMAGE_URL',
    f'http://192.168.88.201:5006/xray/image?id={PLACEHOLDER_IMAGE_ID}',
)
# True：受理后自动拉占位图并推送 body/transparent 到前端
USE_PLACEHOLDER_IMAGES = os.environ.get('USE_PLACEHOLDER_IMAGES', 'true').lower() == 'true'

# ---- 雷达（预约弹窗来车图）——暂指占位图 ----
RADAR_HEAD_URL = os.environ.get('RADAR_HEAD_URL', PLACEHOLDER_IMAGE_URL)

# 现场组播（$VMRMC 距离 / $NTRMC 状态），默认启用
RADAR_TRANSPORT = os.environ.get('RADAR_TRANSPORT', 'multicast').lower()
MCAST_GROUP = os.environ.get('MCAST_GROUP', '224.0.0.100')
MCAST_PORT = int(os.environ.get('MCAST_PORT', '9000'))
MCAST_IFACE_IP = os.environ.get('MCAST_IFACE_IP', '')

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

# ---- 车身影像（面板车身/点云位暂用占位图；枪机抓图仍走海康 ISAPI）----
BODY_IMAGE_URL = os.environ.get('BODY_IMAGE_URL', PLACEHOLDER_IMAGE_URL)
BODY_CAPTURE_DEVICE_ID = os.environ.get('BODY_CAPTURE_DEVICE_ID', 'camera2')

# ---- 透视 / 拼接（暂用同一占位图）----
TRANSPARENT_IMAGE_URL = os.environ.get(
    'TRANSPARENT_IMAGE_URL',
    'http://192.168.88.201:5006/xray/image?id=',
)
YR_TRANSPARENT_IMAGE_URL = os.environ.get(
    'YR_TRANSPARENT_IMAGE_URL',
    'http://192.168.88.201:5006/xray/image?id=',
)
STITCH_IMAGE_URL = os.environ.get(
    'STITCH_IMAGE_URL',
    'http://192.168.88.201:5006/xray/image?id=',
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
