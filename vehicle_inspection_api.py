"""车辆检测记录 API 蓝图

提供检测记录的提交、查询、修改、删除等接口。
参考 Qt VehicleDatabase / LvTongPro::onConfirmClicked()。
"""
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify

from dbm.dbvehicle import DBVehicle
from dbm.dbproduct import DBProduct
from util.auth import login_required

inspection_api = Blueprint('inspection', __name__, url_prefix='/api/inspection')


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
