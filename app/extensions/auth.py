"""JWT 认证工具"""
import jwt
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import request, jsonify, current_app

from config import JWT_SECRET_KEY, JWT_EXPIRATION_HOURS


def create_token(user: dict) -> str:
    """生成 JWT token"""
    payload = {
        'sub': user['username'],
        'real_name': user.get('real_name', ''),
        'role': user.get('role', '1'),
        'iat': datetime.now(timezone.utc),
        'exp': datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm='HS256')


def decode_token(token: str) -> dict | None:
    """解析 JWT token，返回 payload 或 None"""
    try:
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=['HS256'])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def get_token_from_header() -> str | None:
    """从请求头获取 Bearer token"""
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]
    return None


def login_required(f):
    """JWT 认证装饰器 - 用于 API 路由"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = get_token_from_header()
        if not token:
            return jsonify({'code': 401, 'message': '未登录，请重新登录', 'data': None}), 401

        payload = decode_token(token)
        if payload is None:
            return jsonify({'code': 401, 'message': '登录已过期，请重新登录', 'data': None}), 401

        # 将用户信息注入到请求上下文中
        request.gc_user = {
            'username': payload['sub'],
            'real_name': payload.get('real_name', ''),
            'role': payload.get('role', '1'),
        }
        return f(*args, **kwargs)

    return decorated
