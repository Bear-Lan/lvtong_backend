"""车辆检测记录 API 蓝图

提供检测记录的提交、查询、修改、删除等接口。
参考 Qt VehicleDatabase / LvTongPro::onConfirmClicked()。
"""
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify

from app.db.vehicle import DBVehicle
from app.db.product import DBProduct
from app.services.image_store import db_path_to_api, persist_to_storage
from app.db.product import DBProduct
from app.db.dictionary import DBDictionary
from app.extensions.auth import login_required

inspection_api = Blueprint('inspection', __name__, url_prefix='/api/inspection')


# 需要转换的图片路径字段（| 分隔的需要特殊处理）
_SINGLE_IMAGE_FIELDS = [
    'head_image_path', 'tail_image_path', 'top_image_path',
    'body_image_path', 'transparent_image_path',
    'license_image_path', 'license_image_path1', 'license_image_path2',
    'pass_code_image_path', 'passcode_image_path',
]
_MULTI_IMAGE_FIELDS = ['goods_image_path', 'evidences_image_path']

# 字段名 → 图片分类（用于复制落盘时决定子目录）
_IMAGE_CATEGORY = {
    'head_image_path': 'head',
    'tail_image_path': 'tail',
    'top_image_path': 'top',
    'body_image_path': 'body',
    'transparent_image_path': 'transparent',
    'license_image_path': 'license',
    'license_image_path1': 'license_gc',
    'license_image_path2': 'license_gc2',
    'pass_code_image_path': 'passcode',
    'passcode_image_path': 'passcode',
    'goods_image_path': 'goods',
    'evidences_image_path': 'evidence',
}


def _persist_images(body: dict) -> None:
    """把 body 中的所有图片字段统一落地为磁盘绝对路径。

    接收形态（来自前端的"引用"）：
      - 绝对路径 / 相对路径 / /api/images/<rel> / /api/image?path=... / 空 / 外部 URL

    落盘规则：
      - 单图字段：原值或空串 → persist_to_storage(value, category)
      - 多图字段：按 | 切（兼 容 ,），逐项持久化，过滤空，最终用 | 拼回
      - 所有结果为磁盘绝对路径；前端展示时由 db_path_to_api 转 /api/images/<rel>
    """
    for field in _SINGLE_IMAGE_FIELDS:
        if field not in body and field.replace('code', 'code', 0) == field:
            # 不存在就跳过；存在但为 None 时也跳过（保持原样不入库）
            continue
        val = body.get(field)
        if val:
            body[field] = persist_to_storage(str(val), _IMAGE_CATEGORY.get(field, 'misc'))
    for field in _MULTI_IMAGE_FIELDS:
        val = body.get(field)
        if not val:
            continue
        s = str(val)
        # 安全切分 | 或 ,：dataUrl 整段占位成 token，避开其中可能的 ,
        import re as _re
        # data: 起，至下一个 | 或字符串尾止（标准 base64 无空白字符，所以 \S+ 安全）
        token_re = _re.compile(r"data:\S+")
        tokens: list[str] = []
        def _put(m):
            tokens.append(m.group(0))
            return f'__DATAURL_{len(tokens) - 1}__'
        s_safe = token_re.sub(_put, s)
        parts = [p.strip() for p in s_safe.replace(',', '|').split('|') if p.strip()]
        persisted = []
        for p in parts:
            pm = _re.fullmatch(r'__DATAURL_(\d+)__', p)
            if pm:
                p = tokens[int(pm.group(1))]
            ap = persist_to_storage(p, _IMAGE_CATEGORY.get(field, 'misc'))
            if ap:
                persisted.append(ap)
        body[field] = '|'.join(persisted)


def _convert_image_paths(record: dict) -> dict:
    """将记录中的本地路径转换为 API URL（前端展示用）。

    多图字段按 | split（写入时已统一）。
    """
    for field in _SINGLE_IMAGE_FIELDS:
        if record.get(field):
            record[field] = db_path_to_api(record[field])
    for field in _MULTI_IMAGE_FIELDS:
        if record.get(field):
            parts = record[field].split('|')
            record[field] = '|'.join(
                db_path_to_api(p.strip()) or p for p in parts
            )
    return record


# 模块级缓存：避免每次解析都查询数据库
_truck_type_map: dict | None = None
_container_type_map: dict | None = None


def _get_truck_type_map() -> dict:
    """货车类型编码→名称映射（缓存）"""
    global _truck_type_map
    if _truck_type_map is None:
        _truck_type_map = {}
        try:
            for t in DBDictionary().getAllTruckTypes():
                _truck_type_map[t['type_code']] = t['type_name']
        except Exception:
            pass
    return _truck_type_map


def _get_container_type_map() -> dict:
    """货箱类型编码→名称映射（缓存）"""
    global _container_type_map
    if _container_type_map is None:
        _container_type_map = {}
        try:
            for c in DBDictionary().getAllContainerTypes():
                _container_type_map[c['type_code']] = c['type_name']
        except Exception:
            pass
    return _container_type_map


def _resolve_display_names(record: dict) -> dict:
    """编码→名称解析（对齐 Qt DetailDialog::setVehicleInfo）

    对齐 Qt 端 getInspectionsWithFilter 中的 LEFT JOIN 查询：
      - vehicle_type → btypename (truck_type.type_name)
      - goods_type → cvarietyname (agricultural_products.variety_name)
    """
    # 货车类型: "16" → "仓栅式货运"
    vt = record.get('vehicle_type')
    if vt:
        try:
            name = _get_truck_type_map().get(vt)
            if name:
                record['vehicle_name'] = name
                record['btypename'] = name  # 对齐 Qt 字段名
        except Exception:
            pass

    # 货箱类型: "3.1" → "集装箱"
    vct = record.get('vehicle_container_type')
    if vct:
        try:
            name = _get_container_type_map().get(vct)
            if name:
                record['vehicle_container_name'] = name
        except Exception:
            pass

    # 货物名称: "10101|10404" → "波斯菜|白菜"
    gt = record.get('goods_type')
    if gt:
        try:
            db = DBProduct()
            names = []
            for code in gt.split('|'):
                code = code.strip()
                if code:
                    name = db.getVarietyNameByProductCode(code)
                    names.append(name if name else code)
            record['goods_name'] = '|'.join(names)
        except Exception:
            pass

    # 货物种类: 取自 category
    if not record.get('goods_category') and record.get('goods_name'):
        record['goods_category'] = ''
    return record


def ok(data=None, message='success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    return jsonify({'code': code, 'message': message, 'data': data}), code


def _normalize_goods_type(body: dict) -> None:
    """后端防御性归一：把 goods_type 字符串里的中文名字转成 product_code。

    接收形态（混入都行）：
      - '10101'                  已是编码，原样
      - '葡萄'                   字典查 code
      - '葡萄|白菜'              按 | 切，逐段查
      - 混合 '10101|葡萄'        同上
    """
    raw = body.get('goods_type')
    if not raw:
        return
    raw_str = str(raw)
    # 已经是数字/字母编码为主的，return
    if all(part.strip().isdigit() or not part.strip() for part in raw_str.split('|')):
        # 全是数字分段，原样
        cleaned = '|'.join(p.strip() for p in raw_str.split('|') if p.strip())
        if cleaned and all(p.isdigit() for p in cleaned.split('|')):
            body['goods_type'] = cleaned
            return
    db = DBProduct()
    parts = [p.strip() for p in raw_str.replace(',', '|').split('|') if p.strip()]
    codes: list[str] = []
    for p in parts:
        # 已是纯数字编码
        if p.isdigit() or (p.isascii() and p.isalnum() and not any('\u4e00' <= ch <= '\u9fff' for ch in p)):
            codes.append(p)
            continue
        rows = db.getProductByName(p)
        if rows and rows[0].get('product_code'):
            codes.append(str(rows[0]['product_code']))
        # 查不到就跳过（不抛错）
    body['goods_type'] = '|'.join(codes)


def _normalize_vehicle_size(body: dict) -> None:
    """后端防御性归一：vehicle_size 统一为纯数字 `L|W|H`（单位毫米）。

    接收形态：
      - '0|2550|4000'             已是纯数字，原样
      - '长30.00m|宽2.00m|高4.00m'  解析 3 个数字，拼 '30000|2000|4000'
      - '长30m|宽2m|高4m'         同上
      - '12000x2500x3500'        已知形态（按 x 切），拼 3 段
    """
    raw = body.get('vehicle_size')
    if not raw:
        return
    s = str(raw).strip()
    # 已是纯数字,|,空白
    parts = [p.strip() for p in s.split('|') if p.strip()]
    if len(parts) == 3 and all(p.replace('.', '').isdigit() for p in parts):
        # 已经是 L|W|H 形态
        body['vehicle_size'] = '|'.join(parts)
        return
    # 解析中文字段
    import re
    nums: list[str] = []
    # 优先按 | 切，再在每一段里抓数字
    if '|' in s or 'x' in s.lower():
        sep = '|' if '|' in s else 'x'
        for part in s.split(sep):
            m = re.search(r'(\d+(?:\.\d+)?)', part)
            if m:
                v = float(m.group(1))
                # 单位是 m 的话 * 1000 转 mm
                if 'm' in part.lower() and re.search(r'\d+(?:\.\d+)?\s*m', part, re.IGNORECASE):
                    v = int(round(v * 1000))
                else:
                    v = int(round(v))
                nums.append(str(v))
    else:
        # 单串
        m_all = re.findall(r'(\d+(?:\.\d+)?)', s)
        if len(m_all) >= 3:
            nums = [str(int(round(float(v) * 1000))) if 'm' in s.lower() else str(int(round(float(v)))) for v in m_all[:3]]
    if len(nums) == 3:
        body['vehicle_size'] = '|'.join(nums)


@inspection_api.route('/submit', methods=['POST'])
@login_required
def submit_inspection():
    """提交检测记录

    POST /api/inspection/submit
    Body: VehicleInspection 完整数据（50+ 字段 + 7 个时间戳 + 14 个 passcode_* 字段）

    参考 Qt LvTongPro::slotsOnCommitData()

    流程：
      1. 字段映射（前端 vehicle_container_type → DB vehicle_containertype）
      2. 图片持久化：_persist_images() — 把图片引用统一为磁盘绝对路径
      3. 必填字段校验
      4. 防重：同 plate 在 5 秒内视为重复提交，直接返回已存在 id（防双击/重试）
      5. 默认值填充 + 写库
    """
    body = request.get_json(silent=True)
    if not body:
        return fail(400, '请求数据不能为空')

    user = request.gc_user
    print(f'[INSPECTION] 用户 {user["username"]} 提交检测: plate={body.get("plate_number")} '
          f'goods={body.get("goods_type")} passcode_vehicle={body.get("passcode_vehicle_display_id", "")[:30]}')

    # 字段映射：前端 vehicle_container_type → DB vehicle_containertype
    if 'vehicle_container_type' in body and 'vehicle_containertype' not in body:
        body['vehicle_containertype'] = body.pop('vehicle_container_type')
    body.pop('goods_name', None)  # DB 不存

    # ---- 字段归一：goods_type 中文 → 编码；vehicle_size 中文 → 纯数字 L|W|H ----
    try:
        _normalize_goods_type(body)
        _normalize_vehicle_size(body)
    except Exception as e:
        print(f'[INSPECTION] 字段归一失败（不影响提交）: {e}')

    # ---- 1. 图片持久化（必须在必填校验之前，保证入库的就是绝对路径） ----
    try:
        _persist_images(body)
    except Exception as e:
        print(f'[INSPECTION] 图片持久化失败: {e}')
        return fail(500, f'图片持久化失败: {e}')

    # ---- 2. 必填字段校验（对齐 Qt slotsOnCommitData 12 个必填） ----
    missing = _validate_required_fields(body)
    if missing:
        print(f'[INSPECTION] 必填字段缺失: {missing}')
        return fail(400, f'必填字段缺失: {", ".join(missing)}', {
            'missingFields': missing,
        })

    # ---- 3. 防重：同 plate 在 5 秒内视为重复提交 ----
    plate = (body.get('plate_number') or '').strip()
    if plate:
        try:
            db_dup = DBVehicle()
            dup_id = db_dup.findRecentDuplicateId(plate, within_seconds=5)
            if dup_id:
                print(f'[INSPECTION] 防重命中 plate={plate} → 复用已存在 id={dup_id}')
                return ok({'id': dup_id, 'deduped': True}, '提交成功（防重命中）')
        except Exception as e:
            print(f'[INSPECTION] 防重查询失败（不影响提交）: {e}')

    # ---- 4. 设置操作员信息与提交时间 ----
    body.setdefault('operator_name', user['real_name'])
    body.setdefault('inspector_phone', body.get('inspector_phone', ''))
    body.setdefault('reviewer_phone', body.get('reviewer_phone', ''))
    if not body.get('inspection_time'):
        body['inspection_time'] = datetime.now(timezone.utc)

    # ---- 5. 写入数据库 ----
    try:
        db = DBVehicle()
        inspection_id = db.addInspection(body)
    except Exception as e:
        print(f'[INSPECTION] 写库失败: {e}')
        return fail(500, f'数据库写入失败: {e}')

    if inspection_id > 0:
        print(f'[INSPECTION] 提交成功, id={inspection_id}, 字段数={len(body)}')
        return ok({'id': inspection_id}, '提交成功')
    else:
        return fail(500, '提交失败（addInspection 返回 -1）')


# Qt slotsOnCommitData 12 个必填字段校验
_REQUIRED_FIELDS = [
    ('plate_number', '车牌号'),
    ('driver_phone', '司机电话（11位）'),
    ('goods_type', '货物类型'),
    ('vehicle_size', '车辆长宽高'),
    ('head_image_path', '车头照片'),
    ('body_image_path', '车身照片'),
    ('tail_image_path', '车尾照片'),
    ('license_image_path', '行驶证照片'),
    # 通行码必填（Qt 是 ETC 解析后必填，前端如果没拿到留空不阻断提交）
    # ('passcode_en_station_id', '入口站'),
    # ('passcode_ex_station_id', '出口站'),
    # ('passcode_ex_weight', '出口称量'),
]


def _validate_required_fields(body: dict) -> list:
    """校验 Qt 必填字段，返回缺失字段名列表"""
    missing = []
    for field, desc in _REQUIRED_FIELDS:
        v = body.get(field)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(f'{field}({desc})')
    # driver_phone 必须 11 位
    phone = body.get('driver_phone', '')
    if phone and len(str(phone)) != 11:
        missing.append('driver_phone(必须11位手机号)')
    return missing


@inspection_api.route('/query', methods=['GET'])
@login_required
def query_inspections():
    """分页查询检测记录

    GET /api/inspection/query?plate_number=&driver_phone=&vehicle_type=&goods_type=&operator_name=&start_time=&end_time=&page=1&page_size=50

    参考 Qt VehicleDatabase::getInspectionsWithFilter()
    """
    plate_number = request.args.get('plate_number', '')
    driver_phone = request.args.get('driver_phone', '')
    vehicle_type = request.args.get('vehicle_type', '')
    goods_type = request.args.get('goods_type', '')
    operator_name = request.args.get('operator_name', '')
    start_time = request.args.get('start_time', '')
    end_time = request.args.get('end_time', '')
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 50))

    db = DBVehicle()
    total = db.getInspectionsCount(
        plate_number, driver_phone, vehicle_type, goods_type, operator_name,
        start_time or None, end_time or None
    )
    items = db.getInspectionsWithFilter(
        plate_number, driver_phone, vehicle_type, goods_type, operator_name,
        start_time or None, end_time or None,
        page, page_size
    )

    # 解析编码→显示名称（对齐 Qt 端 LEFT JOIN 查询）
    for item in items:
        _resolve_display_names(item)

    return ok({
        'items': items,
        'total': total,
        'page': page,
        'page_size': page_size,
    })


@inspection_api.route('/<int:inspection_id>', methods=['GET'])
@login_required
def get_inspection(inspection_id):
    """获取单条检测详情"""
    db = DBVehicle()
    record = db.getInspectionById(inspection_id)
    if record:
        record = _convert_image_paths(record)
        record = _resolve_display_names(record)
        return ok(record)
    return fail(404, '记录不存在')


@inspection_api.route('/<int:inspection_id>', methods=['PUT'])
@login_required
def update_inspection(inspection_id):
    """修改检测记录"""
    body = request.get_json(silent=True)
    if not body:
        return fail(400, '请求数据不能为空')

    db = DBVehicle()
    if db.updateInspection(inspection_id, body):
        return ok(message='修改成功')
    return fail(404, '记录不存在或无字段可更新')


@inspection_api.route('/<int:inspection_id>', methods=['DELETE'])
@login_required
def delete_inspection(inspection_id):
    """软删除检测记录"""
    db = DBVehicle()
    if db.deleteInspection(inspection_id):
        return ok(message='删除成功')
    return fail(404, '记录不存在')


@inspection_api.route('/plate/<plate>', methods=['GET'])
@login_required
def by_plate(plate):
    """按车牌号查询检测记录与统计

    参考 Qt VehicleDatabase::getInspectionsByPlateNumber()
    """
    db = DBVehicle()
    records = db.getInspectionsByPlate(plate)
    count = db.getInspectionCountByPlate(plate)
    phone = db.getDriverPhoneByPlate(plate)
    gc_plate = db.getGCPlateByPlate(plate)

    return ok({
        'count': count,
        'driver_phone': phone,
        'gc_plate': gc_plate,
        'records': records,
    })
