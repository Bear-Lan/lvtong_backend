"""历史记录与统计分析 API 蓝图

提供检测历史记录查询、导出、统计等功能。
参考 Qt HistoryDialog。
"""
from flask import Blueprint, request, jsonify

from app.db.vehicle import DBVehicle
from app.db.log import DBLog
from app.extensions.auth import login_required
from app.api.inspection import _resolve_display_names

history_api = Blueprint('history', __name__, url_prefix='/api/history')


def ok(data=None, message='success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    return jsonify({'code': code, 'message': message, 'data': data}), code


@history_api.route('/list', methods=['GET'])
@login_required
def list_history():
    """历史记录分页查询

    GET /api/history/list?plate_number=&driver_phone=&vehicle_type=&goods_type=&operator_name=&start_time=&end_time=&page=1&page_size=50

    与 /api/inspection/query 功能一致，提供独立入口。
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


@history_api.route('/export', methods=['GET'])
@login_required
def export_history():
    """导出历史记录为 CSV

    GET /api/history/export?plate_number=&...&format=csv
    """
    plate_number = request.args.get('plate_number', '')
    start_time = request.args.get('start_time', '')
    end_time = request.args.get('end_time', '')

    db = DBVehicle()
    # 导出不分页，最多 10000 条
    items = db.getInspectionsWithFilter(
        plate_number=plate_number,
        start_time=start_time or None,
        end_time=end_time or None,
        page=1, page_size=10000
    )

    # 生成 CSV
    import io, csv
    output = io.StringIO()
    if items:
        writer = csv.DictWriter(output, fieldnames=items[0].keys())
        writer.writeheader()
        writer.writerows(items)

    return ok({
        'csv': output.getvalue(),
        'total': len(items),
    }, '导出成功')


@history_api.route('/statistics', methods=['GET'])
@login_required
def statistics():
    """检测统计数据

    GET /api/history/statistics?date_from=&date_to=&group_by=day|operator
    """
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    group_by = request.args.get('group_by', 'day')

    # 默认统计今天
    from datetime import date as dt_date
    today = dt_date.today().isoformat()
    date_from = date_from or today
    date_to = date_to or today

    db = DBVehicle()
    total = db.getInspectionsCount(
        start_time=date_from, end_time=date_to
    )

    return ok({
        'date_from': date_from,
        'date_to': date_to,
        'total_count': total,
        'group_by': group_by,
    })


# ==================== 操作日志 ====================

@history_api.route('/logs', methods=['GET'])
@login_required
def list_logs():
    """操作日志查询

    GET /api/history/logs?level=&category=&operator=&page=1&page_size=50
    """
    level = request.args.get('level', '')
    category = request.args.get('category', '')
    operator = request.args.get('operator', '')
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 50))

    db = DBLog()
    total = db.getLogsCount(level, category, operator)
    items = db.getLogs(level, category, operator, page=page, page_size=page_size)

    return ok({
        'items': items,
        'total': total,
        'page': page,
        'page_size': page_size,
    })
