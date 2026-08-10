"""图像采集 API 蓝图

提供检测流程中各类型图像的采集接口。
实际拉图并推前端预览（dataUrl）；落盘在提交检测单时 persist。
"""
from flask import Blueprint, request, jsonify

from app.services.capture_service import capture_category
from app.extensions.auth import login_required

capture_api = Blueprint('capture', __name__, url_prefix='/api/capture')


def ok(data=None, message='success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    return jsonify({'code': code, 'message': message, 'data': data}), code


def _capture_one(category: str, success_msg: str):
    try:
        result = capture_category(category)
        return ok(result, success_msg)
    except Exception as e:
        return fail(500, f'{success_msg.replace("成功", "失败")}: {e}')


@capture_api.route('/head', methods=['POST'])
@login_required
def capture_head():
    return _capture_one('head', '车头照片采集成功')


@capture_api.route('/tail', methods=['POST'])
@login_required
def capture_tail():
    return _capture_one('tail', '车尾照片采集成功')


@capture_api.route('/top', methods=['POST'])
@login_required
def capture_top():
    return _capture_one('top', '车顶照片采集成功')


@capture_api.route('/goods', methods=['POST'])
@login_required
def capture_goods():
    return _capture_one('goods', '货物照片采集成功')


@capture_api.route('/license', methods=['POST'])
@login_required
def capture_license():
    return _capture_one('license', '行驶证照片采集成功')


@capture_api.route('/evidence', methods=['POST'])
@login_required
def capture_evidence():
    return _capture_one('evidence', '证据照采集成功')


@capture_api.route('/bulk', methods=['POST'])
@login_required
def capture_bulk():
    """批量采集（对齐 Qt auto-capture 阶段）"""
    body = request.get_json(silent=True) or {}
    categories = body.get('categories', ['head', 'tail', 'top'])
    results = {}
    errors = {}

    for cat in categories:
        try:
            results[cat] = capture_category(cat)
        except Exception as e:
            errors[cat] = str(e)

    return ok({
        'captured': results,
        'failed': errors,
    }, f'批量采集完成: {len(results)} 成功, {len(errors)} 失败')
