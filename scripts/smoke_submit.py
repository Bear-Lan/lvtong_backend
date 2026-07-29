"""E2E smoke test: submit + dedup + /api/images/<rel>"""
import sys, os, tempfile, urllib.parse, json
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from flask import Flask, send_file
from app.api.inspection import inspection_api
from app.api.auth import app_api as auth_api
from app.api.file import file_api
from app.extensions.auth import create_token
from config import IMAGE_STORAGE_ROOT

app = Flask(__name__)
app.register_blueprint(inspection_api)
app.register_blueprint(auth_api)
app.register_blueprint(file_api)


@app.route('/api/images/<path:rel>')
def serve_storage_image(rel: str):
    norm_root = os.path.normpath(IMAGE_STORAGE_ROOT)
    if not rel or '..' in rel.split('/'):
        return json.dumps({'code': 400, 'message': '非法路径'}), 400
    target = os.path.normpath(os.path.join(norm_root, rel))
    try:
        if os.path.commonpath([target, norm_root]) != norm_root:
            return json.dumps({'code': 403, 'message': '拒绝'}), 403
    except ValueError:
        return json.dumps({'code': 403, 'message': '拒绝'}), 403
    if not os.path.isfile(target):
        return json.dumps({'code': 404, 'message': '文件不存在'}), 404
    return send_file(target)


client = app.test_client()
token = create_token({'username': 'admin', 'real_name': 'admin', 'role': '1'})
H = {'Authorization': f'Bearer {token}'}

tmp = tempfile.mkdtemp()
imgs = []
for i in range(3):
    p = os.path.join(tmp, f'src_{i}.jpg')
    with open(p, 'wb') as f:
        f.write(f'mock-{i}'.encode())
    imgs.append(p)

body = {
    'plate_number': 'TEST_A001',
    'driver_phone': '13800000001',
    'goods_type': '10101',
    'vehicle_size': '12000x2500x3500',
    'vehicle_type': '16',
    'vehicle_container_type': '3.1',
    'load_rate': 80,
    'load_weight': 5.5,
    'head_image_path': imgs[0],
    'body_image_path': '/api/image?path=' + urllib.parse.quote(imgs[1], safe=''),
    'tail_image_path': '/api/images/' + os.path.basename(imgs[2]),
    'license_image_path': imgs[2],
    'goods_image_path': imgs[0] + '|' + imgs[1],
    'evidences_image_path': imgs[2],
    'operator_name': 'admin',
}

r = client.post('/api/inspection/submit', json=body, headers=H)
print('submit #1 status=', r.status_code)
d = r.get_json()
print(json.dumps(d, ensure_ascii=False, indent=2))
assert d['code'] == 0, d
first_id = d['data']['id']

r2 = client.get(f'/api/inspection/{first_id}', headers=H)
d2 = r2.get_json()['data']
for f in ('head_image_path', 'body_image_path', 'license_image_path'):
    print(f, '=', d2[f])
    assert d2[f].startswith('/api/images/'), f
assert '|' in d2['goods_image_path'], '多图应保留 |'

body['driver_phone'] = '13800000002'
r3 = client.post('/api/inspection/submit', json=body, headers=H)
d3 = r3.get_json()
print('submit #2 =', json.dumps(d3, ensure_ascii=False))
assert d3['data']['id'] == first_id and d3['data'].get('deduped'), '应去重命中'

r4 = client.get(d2['head_image_path'])
print('GET', d2['head_image_path'], '->', r4.status_code)
assert r4.status_code == 200

r5 = client.get('/api/images/../etc/passwd')
print('GET /api/images/../etc/passwd ->', r5.status_code)
assert r5.status_code in (400, 403)

print()
print('=== E2E submit + dedup + /api/images/<rel> 全部通过 ===')
