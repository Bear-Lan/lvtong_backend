"""新版实拍货品识别：业务字典映射、去重及置信度排序，只提供推荐。"""
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
        return float(item.get('score') or 0)
    except (TypeError, ValueError):
        return 0.0


def _pack_result(payload: dict, goods: list[dict[str, Any]], task: str) -> dict[str, Any]:
    return {
        'task': task,
        'goods': goods,
        'goods_type': '|'.join(g['code'] for g in goods if g.get('code')),
        'goods_name': '|'.join(g['name'] for g in goods if g.get('name')),
        'mixed_load': bool(payload.get('mixed_load')) if 'mixed_load' in payload else None,
        'mixed_load_labels': payload.get('mixed_load_labels'),
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
    # class_id为字符串类别标识（例如pingguo），不是模型index或业务产品编号。
    from app.db.product import DBProduct
    db = DBProduct()
    best = {}
    for item in result['detections']:
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
                      'pinyin': row.get('variety_name_pinyin') or '', 'score': score}
    goods = sorted(best.values(), key=lambda g: g['score'], reverse=True)
    return _pack_result(payload, goods, 'goods_real')


def detect_product_xray(image_bytes: bytes, filename: str = 'xray.jpg') -> dict[str, Any]:
    """货物透视图识别 task=product_xray（建议用拼接图）。"""
    if not image_bytes:
        raise ValueError('缺少透视拼接图')
    raise RuntimeError('透视区域检测与分类暂未启用')
