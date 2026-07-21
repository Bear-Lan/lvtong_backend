"""应用配置"""
import os

# JWT 配置
JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'gcms-jwt-secret-key-2025-beidou-lvtong-inspection')
JWT_EXPIRATION_HOURS = int(os.environ.get('JWT_EXPIRATION_HOURS', '24'))

# 数据库配置
DB_CONFIG = {
    'host': os.environ.get('DB_HOST', '127.0.0.1'),
    'port': int(os.environ.get('DB_PORT', '5432')),
    'dbname': os.environ.get('DB_NAME', 'gcis'),
    'user': os.environ.get('DB_USER', 'postgres'),
    'password': os.environ.get('DB_PASSWORD', '123456'),
}

# CORS 配置
CORS_ORIGINS = os.environ.get('CORS_ORIGINS', 'http://localhost:5173')

# 服务配置
HOST = os.environ.get('HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', '8080'))
DEBUG = os.environ.get('DEBUG', 'false').lower() == 'true'

# ========== 设备 & 图像源 URL 配置 ==========
# 对应 Qt config/app.json 中的配置

# 雷达来车图
RADAR_HEAD_URL = os.environ.get(
    'RADAR_HEAD_URL',
    'http://192.168.88.201:5001/radar/image'
)

# X 光机基础 URL
XRAY_BASE_URL = os.environ.get(
    'XRAY_BASE_URL',
    'http://192.168.88.201:5006'
)

# X 光启动/停止 URL
XRAY_START_TRANSPARENT_URL = os.environ.get(
    'XRAY_START_TRANSPARENT_URL',
    'http://192.168.88.201:5003/xray/capture?frames=500&triggeroffset=2.5&id='
)
XRAY_START_YR_TRANSPARENT_URL = os.environ.get(
    'XRAY_START_YR_TRANSPARENT_URL',
    'http://192.168.88.201:5006/xray/capture?frames=500&triggeroffset=2.5&id='
)

# 车身影像 URL
BODY_IMAGE_URL = os.environ.get(
    'BODY_IMAGE_URL',
    'http://192.168.88.201:5002/body/image'
)

# 透视影像 URL（持续拉取）
TRANSPARENT_IMAGE_URL = os.environ.get(
    'TRANSPARENT_IMAGE_URL',
    'http://192.168.88.201:5003/xray/image?id='
)
YR_TRANSPARENT_IMAGE_URL = os.environ.get(
    'YR_TRANSPARENT_IMAGE_URL',
    'http://192.168.88.201:5006/xray/image?id='
)

# 拼接图像 URL
STITCH_IMAGE_URL = os.environ.get(
    'STITCH_IMAGE_URL',
    'http://192.168.88.201:5006/xray/stitch?id='
)

# AI 服务 URL
AI_MODEL_URL = os.environ.get(
    'AI_MODEL_URL',
    'http://192.168.88.245:8899'
)

# OCR 服务 URL
OCR_SERVICE_URL = os.environ.get(
    'OCR_SERVICE_URL',
    'http://192.168.88.245:8890'
)

# PLC / 门禁 / LED 控制 URL（硬件中间层）
PLC_CONTROL_URL = os.environ.get('PLC_CONTROL_URL', '')
GATE_CONTROL_URL = os.environ.get('GATE_CONTROL_URL', '')
LED_CONTROL_URL = os.environ.get('LED_CONTROL_URL', '')
TTS_CONTROL_URL = os.environ.get('TTS_CONTROL_URL', '')

# 图像存储根目录（对齐 Qt D:/LvTongFiles/Images/captures）
# 开发环境可设置环境变量 IMAGE_STORAGE_ROOT 覆盖
IMAGE_STORAGE_ROOT = os.environ.get(
    'IMAGE_STORAGE_ROOT',
    r'E:/code_product/gcms_src/captures'
)
