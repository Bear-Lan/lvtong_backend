"""车辆检测记录 API 蓝图

提供检测记录的提交、查询、修改、删除等接口。
参考 Qt VehicleDatabase / LvTongPro::onConfirmClicked()。
"""
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify

from app.db.vehicle import DBVehicle
from app.db.product import DBProduct
from app.services.image_store import db_path_to_api
from app.db.product import DBProduct
from app.db.dictionary import DBDictionary
from app.extensions.auth import login_required

inspection_api = Blueprint('inspection', __name__, url_prefix='/api/inspection')


# 需要转换的图片路径字段（逗号分隔的需要特殊处理）
_SINGLE_IMAGE_FIELDS = [
    'head_image_path', 'tail_image_path', 'top_image_path',
    'body_image_path', 'transparent_image_path',
    'license_image_path', 'license_image_path1', 'license_image_path2',
    'pass_code_image_path',
]
_MULTI_IMAGE_FIELDS = ['goods_image_path', 'evidences_image_path']


def _convert_image_paths(record: dict) -> dict:
    """将记录中的本地路径转换为 API URL"""
    for field in _SINGLE_IMAGE_FIELDS:
        if record.get(field):
            record[field] = db_path_to_api(record[field])
    for field in _MULTI_IMAGE_FIELDS:
        if record.get(field):
            parts = record[field].split(',')
            record[field] = ','.join(
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


@inspection_api.route('/submit', methods=['POST'])
@login_required
def submit_inspection():
    """提交检测记录

    POST /api/inspection/submit
    Body: VehicleInspection 完整数据

    参考 Qt LvTongPro::onConfirmClicked()
    """
    body = request.get_json(silent=True)
    if not body:
        return fail(400, '请求数据不能为空')

    user = request.gc_user

    # 设置操作员信息
    body.setdefault('operator_name', user['real_name'])
    body.setdefault('inspector_phone', body.get('inspector_phone', ''))

    # 设置提交时间
    if not body.get('inspection_time'):
        body['inspection_time'] = datetime.now(timezone.utc)

    # 获取复核人信息（从请求中读取或默认值）
    body.setdefault('reviewer_name', '')
    body.setdefault('reviewer_phone', '')

    # 写入数据库
    db = DBVehicle()
    inspection_id = db.addInspection(body)

    if inspection_id > 0:
        return ok({'id': inspection_id}, '提交成功')
    else:
        return fail(500, '提交失败')


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
