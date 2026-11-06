"""设备 & 外部服务 URL 配置 — 对齐 Qt config/app.json"""
import os

# ---- 中间层出图：http://host:5006/image?type=<cam|vtop|vside|x160|x200|appt|merge> ----
IMAGE_FETCH_BASE = os.environ.get(
    'IMAGE_FETCH_BASE', 'http://192.168.88.201:5006/image'
).rstrip('?&')


def image_fetch_url(image_type: str) -> str:
    """按 type 拼拉图地址。例：image_fetch_url('cam') → .../image?type=cam"""
    t = (image_type or '').strip()
    if not t:
        return IMAGE_FETCH_BASE
    sep = '&' if '?' in IMAGE_FETCH_BASE else '?'
    return f'{IMAGE_FETCH_BASE}{sep}type={t}'


# 兼容旧名：未单独配置时，六类图走新 type 接口
PLACEHOLDER_IMAGE_ID = os.environ.get('PLACEHOLDER_IMAGE_ID', '')
PLACEHOLDER_IMAGE_URL = os.environ.get(
    'PLACEHOLDER_IMAGE_URL',
    image_fetch_url('cam'),
)
# True：距离 <33m 触发后按 type 单帧拉图（GET 成功即结束）
USE_PLACEHOLDER_IMAGES = os.environ.get('USE_PLACEHOLDER_IMAGES', 'true').lower() == 'true'

# ---- 预约雷达图（appt / 几何图）----
RADAR_HEAD_URL = os.environ.get(
    'RADAR_HEAD_URL', 'http://192.168.88.201:5001/appt/image'
)
# LAZ 正交预览：水平/垂直坐标轴及坐标单位。现场文件到达后核对安装方向。
RADAR_LAZ_AXES = os.environ.get('RADAR_LAZ_AXES', 'xz')
RADAR_LAZ_METERS_PER_UNIT = float(os.environ.get('RADAR_LAZ_METERS_PER_UNIT', '1'))
RADAR_LAZ_RESOLUTION = float(os.environ.get('RADAR_LAZ_RESOLUTION', '0.01'))

# 现场组播（$VMRMC 距离 / $NTRMC 状态），默认启用
RADAR_TRANSPORT = os.environ.get('RADAR_TRANSPORT', 'multicast').lower()
MCAST_GROUP = os.environ.get('MCAST_GROUP', '224.0.0.100')
MCAST_PORT = int(os.environ.get('MCAST_PORT', '9000'))
# 空=自动猜网卡（优先与 192.168.0.188 同网段）。VPN 抢走路由时可强制：
#   set MCAST_IFACE_IP=192.168.0.138
MCAST_IFACE_IP = os.environ.get('MCAST_IFACE_IP', '')
# 收包与发令可经过不同网卡；未配置时沿用发令网卡，兼容现有部署。
MCAST_RECV_IFACE_IP = os.environ.get('MCAST_RECV_IFACE_IP', '')

# ---- X 光机启扫（组播）----
# $MDRMC,1,xraycap=true&{车头长}&{small|large}；拉图仍走 :5003/xray/image
XRAY_BASE_URL = os.environ.get(
    'XRAY_BASE_URL', 'http://192.168.88.201:5003'
)
XRAY_START_TRANSPARENT_URL = os.environ.get(
    'XRAY_START_TRANSPARENT_URL',
    'http://192.168.88.201:5003/xray/capture',
)
# 若现场 160 与 200 同口，可与上相同；分口时再改 env
XRAY_START_YR_TRANSPARENT_URL = os.environ.get(
    'XRAY_START_YR_TRANSPARENT_URL',
    XRAY_START_TRANSPARENT_URL,
)

# ---- 车身相机（:5002 拉图）----
# 启停采集：组播 $MDRMC,1,camcap=true|false；轮询 GET /camera/image
BODY_CAMERA_CAPTURE_URL = os.environ.get(
    'BODY_CAMERA_CAPTURE_URL',
    'http://192.168.88.201:5002/camera/capture',
)
BODY_CAMERA_IMAGE_URL = os.environ.get(
    'BODY_CAMERA_IMAGE_URL',
    'http://192.168.88.201:5002/camera/image',
)
# 车身 cam：改为 :5002 持续拉图（vtop/vside 仍走 5006）
BODY_IMAGE_URL = os.environ.get('BODY_IMAGE_URL', BODY_CAMERA_IMAGE_URL)
BODY_TOP_IMAGE_URL = os.environ.get('BODY_TOP_IMAGE_URL', image_fetch_url('vtop'))
BODY_SIDE_IMAGE_URL = os.environ.get('BODY_SIDE_IMAGE_URL', image_fetch_url('vside'))
HEAD_CAPTURE_DEVICE_ID = os.environ.get('HEAD_CAPTURE_DEVICE_ID', 'cam_head')
TAIL_CAPTURE_DEVICE_ID = os.environ.get('TAIL_CAPTURE_DEVICE_ID', 'cam_lane')
TOP_CAPTURE_DEVICE_ID = os.environ.get('TOP_CAPTURE_DEVICE_ID', 'cam_road')

# ---- 透视：200=:5003/xray/image；160/拼接暂不接 ----
XRAY_IMAGE_URL = os.environ.get(
    'XRAY_IMAGE_URL', 'http://192.168.88.201:5003/xray/image',
)
TRANSPARENT_IMAGE_URL = os.environ.get('TRANSPARENT_IMAGE_URL', XRAY_IMAGE_URL)
YR_TRANSPARENT_IMAGE_URL = os.environ.get('YR_TRANSPARENT_IMAGE_URL', '')
STITCH_IMAGE_URL = os.environ.get('STITCH_IMAGE_URL', '')

# ---- AI 统一服务 ----
# 业务：行驶证 AI 裁剪、装载率 mask、货车/货箱识别等 → AI_DETECT_URL (.../detect)
# 探活：同主机 /health（由 AI_DETECT_URL 推导，不必再配 AI_MODEL_URL）
AI_DETECT_URL = os.environ.get(
    'AI_DETECT_URL', 'http://220.249.109.154:90/detect'
)
AI_LICENSE_CROP_URL = os.environ.get('AI_LICENSE_CROP_URL') or AI_DETECT_URL
AI_LOAD_RATE_URL = os.environ.get('AI_LOAD_RATE_URL') or AI_DETECT_URL

# 行驶证 OCR 文本（号牌/核定载质量/总质量）；默认与 gongsi 现场 OCR 一致
OCR_SERVICE_URL = os.environ.get(
    'OCR_SERVICE_URL', 'http://192.168.88.245:8894'
)

# ---- PLC / 门禁 / LED / TTS ----
# PLC / LED / 栏杆：UDP 组播 $MDRMC → 224.0.0.100:9000（出口网卡见 MCAST_IFACE_IP）
TTS_CONTROL_URL = os.environ.get('TTS_CONTROL_URL', '')

# ---- 图像存储 ----
IMAGE_STORAGE_ROOT = os.environ.get(
    'IMAGE_STORAGE_ROOT', r'E:/code_product/gcms_src/captures'
)

