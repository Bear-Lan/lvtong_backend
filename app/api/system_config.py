"""管理员维护参数；保存不发令，重启后端后整体生效。"""
from flask import Blueprint, request, jsonify
from app.extensions.auth import login_required
from app.services.system_config import get_store, value, ConfigConflict

system_config_api = Blueprint('system_config', __name__, url_prefix='/api/config')


@system_config_api.route('', methods=['GET', 'PATCH'])
@login_required
def maintain_config():
    if str(request.gc_user.get('role')) not in ('0', 'admin'):
        return jsonify(code=403, message='仅管理员可维护系统参数', data=None), 403
    store = get_store()
    if store is None:
        return jsonify(code=503, message='参数库未初始化', data=None), 503
    try:
        if request.method == 'PATCH':
            body = request.get_json(silent=True) or {}
            store.save(body.get('values'), request.gc_user['username'], body.get('revisions'))
        rows = [{**row, 'activeValue': value(row['key']), 'pendingRestart': value(row['key']) != row['value']} for row in store.rows()]
        return jsonify(code=0, message='保存后请重启后端使参数生效' if request.method == 'PATCH' else 'success', data=rows)
    except ConfigConflict as error:
        return jsonify(code=409, message=str(error), data=None), 409
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return jsonify(code=400, message=str(error), data=None), 400
