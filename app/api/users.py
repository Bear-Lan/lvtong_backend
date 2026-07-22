"""用户管理 API 蓝图"""
from flask import Blueprint, request, jsonify

from app.db.user import DBUser
from app.extensions.auth import login_required

user_api = Blueprint('user', __name__, url_prefix='/api/user')


def ok(data=None, message='success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    return jsonify({'code': code, 'message': message, 'data': data}), code


@user_api.route('/query')
@login_required
def query_user():
    """查询用户列表
    GET /api/user/query?username=all   → 查询全部
    GET /api/user/query?username=xxx   → 查询指定用户
    """
    username = request.args.get('username', 'all')
    dbuser = DBUser()
    if username == 'all':
        users = dbuser.users()
    else:
        users = dbuser.users(username)
    return ok(users)


@user_api.route('/delete/<username>', methods=['DELETE'])
@login_required
def delete_user(username):
    """删除用户
    DELETE /api/user/delete/<username>
    """
    dbuser = DBUser()
    try:
        dbuser.delUser(username)
        return ok(message=f'用户 {username} 已删除')
    except Exception as e:
        print(f'[ERROR] 删除用户失败: {e}')
        return fail(500, f'删除失败: {str(e)}')


@user_api.route('/update/<username>', methods=['POST', 'PUT'])
@login_required
def update_user(username):
    """创建或更新用户
    POST /api/user/update/<username>
    Body: { "password": "...", "real_name": "...", "role": "..." }
    """
    body = request.get_json(silent=True)
    if not body:
        return fail(400, '请求数据不能为空')

    dbuser = DBUser()
    try:
        dbuser.updateUser(username, body)
        return ok(message=f'用户 {username} 已保存')
    except Exception as e:
        print(f'[ERROR] 更新用户失败: {e}')
        return fail(500, f'操作失败: {str(e)}')
