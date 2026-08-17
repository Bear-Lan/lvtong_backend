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
# True：33~35m 触发后按 type 单帧拉图（GET 成功即结束）
USE_PLACEHOLDER_IMAGES = os.environ.get('USE_PLACEHOLDER_IMAGES', 'true').lower() == 'true'

# ---- 预约雷达图（appt）----
RADAR_HEAD_URL = os.environ.get('RADAR_HEAD_URL', image_fetch_url('appt'))

# 现场组播（$VMRMC 距离 / $NTRMC 状态），默认启用
RADAR_TRANSPORT = os.environ.get('RADAR_TRANSPORT', 'multicast').lower()
MCAST_GROUP = os.environ.get('MCAST_GROUP', '224.0.0.100')
MCAST_PORT = int(os.environ.get('MCAST_PORT', '9000'))
# 空=自动猜网卡（优先与 192.168.0.188 同网段）。VPN 抢走路由时可强制：
#   set MCAST_IFACE_IP=192.168.0.138
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

# ---- 车身影像：车身 cam / 车顶雷达 vtop / 车侧雷达 vside ----
BODY_IMAGE_URL = os.environ.get('BODY_IMAGE_URL', image_fetch_url('cam'))
BODY_TOP_IMAGE_URL = os.environ.get('BODY_TOP_IMAGE_URL', image_fetch_url('vtop'))
BODY_SIDE_IMAGE_URL = os.environ.get('BODY_SIDE_IMAGE_URL', image_fetch_url('vside'))
BODY_CAPTURE_DEVICE_ID = os.environ.get('BODY_CAPTURE_DEVICE_ID', 'camera2')

# ---- 透视：200=x200 / 160=x160 / 拼接=merge ----
TRANSPARENT_IMAGE_URL = os.environ.get(
    'TRANSPARENT_IMAGE_URL', image_fetch_url('x200'),
)
YR_TRANSPARENT_IMAGE_URL = os.environ.get(
    'YR_TRANSPARENT_IMAGE_URL', image_fetch_url('x160'),
)
STITCH_IMAGE_URL = os.environ.get('STITCH_IMAGE_URL', image_fetch_url('merge'))

# ---- AI 统一服务 ----
# 主检测入口：行驶证 AI 裁剪、车厢装载率 mask（truck_xray_box）等均走此 URL。
# 部署时优先改环境变量 AI_DETECT_URL；一般无需再配下面两个覆盖项。
AI_DETECT_URL = os.environ.get(
    'AI_DETECT_URL', 'http://220.249.109.154:90/detect'
)
AI_LICENSE_CROP_URL = os.environ.get('AI_LICENSE_CROP_URL') or AI_DETECT_URL
AI_LOAD_RATE_URL = os.environ.get('AI_LOAD_RATE_URL') or AI_DETECT_URL

# 其它 AI 相关（路径/端口可能不同，单独配置）
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
