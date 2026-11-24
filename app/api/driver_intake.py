"""检测系统内网资料入口。浏览器仅使用系统登录，不持有接收服务凭据。"""
from flask import Blueprint, current_app, jsonify, request, send_file
from app.extensions.auth import login_required
from app.services.inspection_session import SessionConflict, broadcast

intake_api = Blueprint('driver_intake', __name__, url_prefix='/api/driver-intake')


def service():
    return current_app.extensions['driver_intake']


@intake_api.get('/candidates')
@login_required
def candidates():
    try:
        offset, limit = int(request.args.get('offset', 0)), int(request.args.get('limit', 50))
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError('分页无效')
        return jsonify(code=0, message='success', data=service().candidates(
            request.args.get('q', '').strip(), request.args.get('earlier') == '1', offset, limit))
    except ValueError as exc:
        return jsonify(code=400, message=str(exc), data=None), 400


@intake_api.get('/records/<rid>/photos/<photo_id>')
@login_required
def photo(rid, photo_id):
    try:
        item = service().get(rid)
        selected = next((p for p in item['photos'] if p['id'] == photo_id), None)
        if not selected:
            raise KeyError()
        service().materialize(item)
        return send_file(service().photo_path(item, selected), mimetype='image/jpeg', conditional=False)
    except KeyError:
        return jsonify(code=404, message='照片不存在', data=None), 404
    except Exception:
        return jsonify(code=503, message='照片暂不可用，请稍后重试', data=None), 503


@intake_api.post('/selection')
@login_required
def selection():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) - {'uuid','sessionId','revision','replace','allowMismatch'}:
        return jsonify(code=400, message='请求参数无效', data=None), 400
    if type(body.get('revision')) is not int or not isinstance(body.get('sessionId'), str) or body.get('uuid') is not None and not isinstance(body['uuid'], str):
        return jsonify(code=400, message='会话和版本无效', data=None), 400
    if any(k in body and type(body[k]) is not bool for k in ('replace', 'allowMismatch')):
        return jsonify(code=400, message='确认参数须为布尔值', data=None), 400
    try:
        snapshot = service().associate(body.get('uuid'), body['sessionId'], body['revision'], request.gc_user['username'],
            body.get('replace', False), body.get('allowMismatch', False))
        broadcast(snapshot)
        return jsonify(code=0, message='success', data=snapshot)
    except SessionConflict as exc:
        return jsonify(code=409, message=str(exc), data=service().sessions.snapshot()), 409
    except KeyError:
        return jsonify(code=404, message='资料不存在', data=None), 404
    except ValueError as exc:
        return jsonify(code=400, message=str(exc), data=None), 400
    except Exception:
        return jsonify(code=503, message='资料尚未完整就绪，请稍后重试', data=None), 503
