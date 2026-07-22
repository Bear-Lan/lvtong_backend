"""应用基础配置 — JWT、数据库、CORS、服务端口"""
import os

# JWT
JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'gcms-jwt-secret-key-2025-beidou-lvtong-inspection')
JWT_EXPIRATION_HOURS = int(os.environ.get('JWT_EXPIRATION_HOURS', '24'))

# 数据库
DB_CONFIG = {
    'host': os.environ.get('DB_HOST', '127.0.0.1'),
    'port': int(os.environ.get('DB_PORT', '5432')),
    'dbname': os.environ.get('DB_NAME', 'gcis'),
    'user': os.environ.get('DB_USER', 'postgres'),
    'password': os.environ.get('DB_PASSWORD', '123456'),
}

# CORS
CORS_ORIGINS = os.environ.get('CORS_ORIGINS', 'http://localhost:5173')

# 服务
HOST = os.environ.get('HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', '8080'))
DEBUG = os.environ.get('DEBUG', 'false').lower() == 'true'
