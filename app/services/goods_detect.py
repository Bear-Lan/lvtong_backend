"""货物识别 — 对齐 gongsi_LvTong product_real / product_xray

- product_real：货物实物图 → 拼音 label → 中文 → 农产品字典
- product_xray：透视拼接图 → label 多为 product_code，兼兼容拼音/中文

输入图走项目 AI_DETECT_URL（非 LvTong 的 localhost:8890）。
"""
from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

import requests

from app.services.goods_pinyin_map import GOODS_PINYIN_MAP
from config import AI_DETECT_URL

logger = logging.getLogger('goods_detect')

# 非品类标签，不参与回填
_SKIP_LABELS = frozenset({
    'hunzhuang_review',
    'non_greenpass_review',
})


def _call_ai(
    image_bytes: bytes,
    *,
    task: str,
    score_threshold: str,
    filename: str,
) -> dict:
    files = {
        'image': (filename or 'goods.jpg', BytesIO(image_bytes), 'application/octet-stream'),
    }
    data = {
        'task': task,
        'return_image': '0',
        'return_mask': '0',
        'score_threshold': score_threshold,
    }
    resp = requests.post(AI_DETECT_URL, files=files, data=data, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f'AI HTTP {resp.status_code}: {(resp.text or "")[:300]}')
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError('AI 返回非 JSON object')
    return payload


def _score_of(item: dict) -> float:
    try:
        return float(item.get('score') or 0)
    except (TypeError, ValueError):
        return 0.0


def _label_candidates(item: dict) -> list[str]:
    out: list[str] = []
    for key in ('label', 'base_label'):
        v = item.get(key)
        if v is None:
            continue
        s = str(v).strip()
        if s and s not in out:
            out.append(s)
    return out


def _resolve_one_label(raw: str, db) -> dict[str, str] | None:
    """单个 AI label → {code, name, pinyin}；查不到返回 None。"""
    key = (raw or '').strip()
    if not key or key in _SKIP_LABELS:
        return None

    # 1) 已是 product_code
    row = db.getProductByCode(key)
    if row:
        return {
            'code': str(row.get('product_code') or key),
            'name': str(row.get('variety_name') or ''),
            'pinyin': str(row.get('variety_name_pinyin') or key),
        }

    # 2) 拼音 → 中文（LvTong GOODS_PINYIN_MAP）
    chinese = GOODS_PINYIN_MAP.get(key) or GOODS_PINYIN_MAP.get(key.lower())
    if chinese:
        rows = db.getProductByName(chinese)
        if rows:
            r = rows[0]
            return {
                'code': str(r.get('product_code') or ''),
                'name': str(r.get('variety_name') or chinese),
                'pinyin': str(r.get('variety_name_pinyin') or key),
            }
        # 字典无此品，仍回填中文名（提交时后端再归一，查不到会丢）
        return {'code': '', 'name': chinese, 'pinyin': key}

    # 3) 直接当中文品名查
    rows = db.getProductByName(key)
    if rows:
        r = rows[0]
        return {
            'code': str(r.get('product_code') or ''),
            'name': str(r.get('variety_name') or key),
            'pinyin': str(r.get('variety_name_pinyin') or ''),
        }

    # 4) 拼音模糊（variety_name_pinyin LIKE）
    try:
        fuzzy = db.getProductsWithFilter(variety_name_pinyin=key, page=1, page_size=1)
        if fuzzy:
            r = fuzzy[0]
            return {
                'code': str(r.get('product_code') or ''),
                'name': str(r.get('variety_name') or ''),
                'pinyin': str(r.get('variety_name_pinyin') or key),
            }
    except Exception:
        pass

    return None


def resolve_goods_from_data(data: list) -> list[dict[str, Any]]:
    """AI data[] → 去重后的货物列表（按 score 降序，同品保留最高分）。"""
    from app.db.product import DBProduct

    db = DBProduct()
    best: dict[str, dict[str, Any]] = {}  # dedupe key → entry

    items = [x for x in data if isinstance(x, dict)]
    items.sort(key=_score_of, reverse=True)

    for item in items:
        resolved = None
        for cand in _label_candidates(item):
            resolved = _resolve_one_label(cand, db)
            if resolved:
                break
        if not resolved:
            continue
        code = resolved.get('code') or ''
        name = resolved.get('name') or ''
        dedupe = code or name
        if not dedupe or dedupe in best:
            continue
        entry = {
            'code': code,
            'name': name,
            'pinyin': resolved.get('pinyin') or '',
            'score': _score_of(item),
            'label': str(item.get('label') or ''),
        }
        best[dedupe] = entry

    return list(best.values())


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
    """货物实物图识别 task=product_real。"""
    if not image_bytes:
        raise ValueError('缺少货物实物图')
    payload = _call_ai(
        image_bytes,
        task='product_real',
        score_threshold='0.6',
        filename=filename or 'goods.jpg',
    )
    data = payload.get('data') if isinstance(payload.get('data'), list) else []
    goods = resolve_goods_from_data(data)
    logger.info('product_real goods=%s mixed=%s', [g.get('name') for g in goods], payload.get('mixed_load'))
    return _pack_result(payload, goods, 'product_real')


def detect_product_xray(image_bytes: bytes, filename: str = 'xray.jpg') -> dict[str, Any]:
    """货物透视图识别 task=product_xray（建议用拼接图）。"""
    if not image_bytes:
        raise ValueError('缺少透视拼接图')
    payload = _call_ai(
        image_bytes,
        task='product_xray',
        score_threshold='0.1',
        filename=filename or 'xray.jpg',
    )
    data = payload.get('data') if isinstance(payload.get('data'), list) else []
    goods = resolve_goods_from_data(data)
    logger.info('product_xray goods=%s mixed=%s', [g.get('name') for g in goods], payload.get('mixed_load'))
    return _pack_result(payload, goods, 'product_xray')
