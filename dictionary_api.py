"""字典数据 API 蓝图

提供农产品、货车类型、货箱类型、不合格类型等字典查询。
"""
from flask import Blueprint, request, jsonify

from dbm.dbproduct import DBProduct
from dbm.dbdictionary import DBDictionary
from util.auth import login_required

dict_api = Blueprint('dict', __name__, url_prefix='/api/dict')


def ok(data=None, message='success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    return jsonify({'code': code, 'message': message, 'data': data}), code


# ==================== 农产品 ====================

@dict_api.route('/products', methods=['GET'])
@login_required
def list_products():
    """查询农产品列表（支持搜索/分页）

    GET /api/dict/products?product_type=&variety_name=&pinyin=&page=1&page_size=50
    """
    product_type = request.args.get('product_type', '')
    variety_name = request.args.get('variety_name', '')
    pinyin = request.args.get('pinyin', '')
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 50))

    db = DBProduct()
    total = db.getProductsCount(product_type, variety_name, pinyin)
    items = db.getProductsWithFilter(product_type, variety_name, pinyin,
                                     page=page, page_size=page_size)
    return ok({'items': items, 'total': total, 'page': page, 'page_size': page_size})


@dict_api.route('/products/all', methods=['GET'])
@login_required
def all_products():
    """获取全部农产品（用于下拉选择等场景）"""
    db = DBProduct()
    return ok(db.getAllProducts())


# ==================== 货车类型 ====================

@dict_api.route('/truck-types', methods=['GET'])
@login_required
def truck_types():
    """货车类型列表"""
    db = DBDictionary()
    return ok(db.getAllTruckTypes())


# ==================== 货箱类型 ====================

@dict_api.route('/container-types', methods=['GET'])
@login_required
def container_types():
    """货箱类型列表"""
    db = DBDictionary()
    return ok(db.getAllContainerTypes())


# ==================== 不合格类型 ====================

@dict_api.route('/no-pass-types', methods=['GET'])
@login_required
def no_pass_types():
    """不合格类型列表"""
    db = DBDictionary()
    return ok(db.getAllNoPassTypes())


# ==================== 收费站 ====================

@dict_api.route('/stations/<station_id>', methods=['GET'])
@login_required
def station_info(station_id):
    """收费站信息"""
    db = DBDictionary()
    station = db.getStationById(station_id)
    if station:
        return ok(station)
    return fail(404, '收费站不存在')
