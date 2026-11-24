"""实物主推荐、透视待核辅助：精确映射业务字典，不生成货物或混装结论。"""
from __future__ import annotations

from typing import Any
from app.services.goods_pinyin_map import GOODS_PINYIN_MAP

# 非品类标签，不参与回填
_SKIP_LABELS = frozenset({
    'hunzhuang_review',
    'non_greenpass_review',
})


def _score_of(item: dict) -> float:
    try:
        return float(item['score']) if not isinstance(item.get('score'), bool) else float('nan')
    except (KeyError, TypeError, ValueError):
        return float('nan')


def _pack_result(goods: list[dict[str, Any]], task: str) -> dict[str, Any]:
    return {
        'task': task,
        'goods': goods,
        'goods_type': '|'.join(g['code'] for g in goods if g.get('code')),
        'goods_name': '|'.join(g['name'] for g in goods if g.get('name')),
        # 该链路只给候选，不透传上游可能附带的混装业务结论。
        'mixed_load': None,
        'mixed_load_labels': None,
        'count': len(goods),
    }


def detect_product_real(image_bytes: bytes, filename: str = 'goods.jpg') -> dict[str, Any]:
    """实拍照片生成推荐，不生成混装结论。"""
    if not image_bytes:
        raise ValueError('缺少货物实物图')
    from app.services.ai_client import request_json
    payload = request_json('/v1/goods/real',
        files={'image': (filename or 'goods.jpg', image_bytes, 'application/octet-stream')})
    result = payload.get('data')
    if not isinstance(result, dict) or not isinstance(result.get('detections'), list):
        raise RuntimeError('AI实拍货品响应缺少data.detections')
    goods = _map_goods(result['detections'], 'real', 'unverified')
    return _pack_result(goods, 'goods_real')


def _map_goods(detections: list, source: str, validity: str) -> list[dict[str, Any]]:
    """仅精确映射已有业务字典；未知/歧义标签不造新品种、不改变分类。"""
    from app.db.product import DBProduct
    db = DBProduct()
    best = {}
    for item in detections:
        if not isinstance(item, dict):
            continue
        class_id = str(item.get('class_id') or '').strip()
        score = _score_of(item)
        if not class_id or class_id in _SKIP_LABELS or not 0 <= score <= 1:
            continue
        row = db.getProductByCode(class_id)
        if not row:
            name = GOODS_PINYIN_MAP.get(class_id)
            matches = db.getProductByName(name) if name else []
            row = matches[0] if len(matches) == 1 else None
        if not row:
            continue
        code = str(row.get('product_code') or '')
        if not code or (code in best and best[code]['score'] >= score):
            continue
        best[code] = {'code': code, 'name': row['variety_name'],
                      'pinyin': row.get('variety_name_pinyin') or '', 'score': score,
                      'source': source, 'validity': validity}
    return sorted(best.values(), key=lambda g: (-g['score'], g['code']))


def detect_product_xray(image_bytes: bytes, filename: str = 'xray.jpg') -> dict[str, Any]:
    """经已核实的组合接口定位后逐区域分类，不把定位分当相关性概率。"""
    if not image_bytes:
        raise ValueError('缺少透视拼接图')
    from app.services.ai_client import request_json
    payload = request_json('/v1/xray/analyze',
        files={'image': (filename or 'xray.jpg', image_bytes, 'application/octet-stream')},
        data={'top_k': '3'}, read_timeout=110)
    result = payload.get('data')
    if not isinstance(result, dict) or not isinstance(result.get('regions'), list) or not isinstance(result.get('classifications'), list):
        raise RuntimeError('AI透视响应缺少data.regions或data.classifications')
    region_ids = {str(row['id']) for row in result['regions']
                  if isinstance(row, dict) and row.get('id') is not None}
    if len(region_ids) != len(result['regions']):
        raise RuntimeError('AI透视区域id缺失或重复')
    candidates = []
    classified = set()
    for row in result['classifications']:
        if not isinstance(row, dict) or str(row.get('region_id')) not in region_ids:
            continue
        region_id = str(row['region_id'])
        if region_id in classified:
            raise RuntimeError('AI透视区域重复分类')
        classified.add(region_id)
        choices = row.get('candidates')
        if not isinstance(choices, list):
            raise RuntimeError('AI透视区域响应缺少candidates')
        # 每区域只取原始Top-1；不得跳过未知Top-1改拿Top-2冒充识别结果。
        if choices and isinstance(choices[0], dict):
            candidates.append(choices[0])
    if classified != region_ids:
        raise RuntimeError('AI透视区域缺少对应分类')
    # 当前服务无相关性g、验证支持数等字段，绝不冒称accepted。
    goods = _map_goods(candidates, 'xray', 'uncertain')
    packed = _pack_result(goods, 'goods_xray')
    packed['verification_message'] = '透视接口缺少完整可信校验字段，仅作待核辅助，不确定货物或混装'
    return packed
