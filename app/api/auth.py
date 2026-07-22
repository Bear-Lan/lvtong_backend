"""认证与系统 API 蓝图"""
from flask import Blueprint, request, jsonify

from app.db.user import DBUser
from app.extensions.auth import create_token, login_required

app_api = Blueprint('api', __name__, url_prefix='/api')


def ok(data=None, message='success'):
    """统一成功响应"""
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    """统一失败响应"""
    return jsonify({'code': code, 'message': message, 'data': data}), code


@app_api.route('/health')
def health():
    """健康检查"""
    return ok({'status': 'ok', 'version': '2.0.0'})


# ==================== 认证接口 ====================

@app_api.route('/auth/login', methods=['POST'])
def login():
    """用户登录"""
    body = request.get_json(silent=True)
    if not body:
        return fail(400, '请求数据不能为空')

    username = body.get('username', '').strip()
    password = body.get('password', '').strip()

    if not username or not password:
        return fail(400, '用户名和密码不能为空')

    dbuser = DBUser()
    user = dbuser.loginUser(username, password)
    if user is None:
        return fail(400, '用户名或密码不正确')

    token = create_token(user)
    return ok({
        'token': token,
        'user': {
            'username': user['username'],
            'realName': user['real_name'],
            'phone': user.get('phone', ''),
            'role': int(user['role']),
        },
    }, '登录成功')


@app_api.route('/auth/me', methods=['GET'])
@login_required
def get_current_user():
    """获取当前登录用户信息"""
    user = request.gc_user
    return ok({
        'username': user['username'],
        'realName': user['real_name'],
        'phone': user.get('phone', ''),
        'role': int(user['role']),
    })


@app_api.route('/auth/change-password', methods=['POST'])
@login_required
def change_password():
    """修改密码"""
    body = request.get_json(silent=True)
    if not body:
        return fail(400, '请求数据不能为空')

    password = body.get('password', '').strip()
    if not password:
        return fail(400, '新密码不能为空')

    user = request.gc_user
    dbuser = DBUser()
    if dbuser.changePassword(user['username'], password):
        return ok(message='密码修改成功')
    else:
        return fail(500, '密码修改失败')
