"""面板预览与原图裁切，不访问设备；只读共享图片目录。"""
from flask import Blueprint, jsonify, request, send_file
from app.extensions.auth import login_required

panel_images_api = Blueprint('panel_images', __name__, url_prefix='/api/panel-images')


def image_root():
    from app.services.inspection_session import get_store
    store = get_store()
    if store is not None:
        return store.image_root
    from config import IMAGE_STORAGE_ROOT
    return IMAGE_STORAGE_ROOT


@panel_images_api.get('/preview/<name>')
def preview(name):
    # 与既有/api/images一样可由<img>直接读取，仅允许内容哈希命名的共享图片。
    from app.services.panel_preview import ensure_preview, run_image_task
    try:
        path = run_image_task(lambda: ensure_preview(image_root(), name))
        return send_file(path, mimetype='image/jpeg', conditional=True, max_age=86400)
    except (ValueError, OSError):
        return jsonify(code=404, message='面板预览不存在', data=None), 404


@panel_images_api.post('/crop')
@login_required
def crop():
    from app.services.panel_preview import crop_original, run_image_task
    from app.services.temp_image_cache import put_and_url
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(code=400, message='框选参数格式无效', data=None), 400
    try:
        raw = run_image_task(lambda: crop_original(image_root(), body.get('sourceUrl'),
            [body.get(k) for k in ('x1', 'y1', 'x2', 'y2')], body.get('displayWidth'), body.get('displayHeight')))
        return jsonify(code=0, data={'url': put_and_url(raw, 'image/png')})
    except (ValueError, TypeError, OSError):
        return jsonify(code=400, message='原图裁切失败，请重新框选', data=None), 400
