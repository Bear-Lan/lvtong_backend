"""AI 掩码装载率计算 — 对齐 gongsi_LvTong MaskLoadingRateCalculator

流程：
  AI task=truck_xray_box → data[0].mask.base64 → 分区白像素占比加权

方案选择（业务规则）：
  - 厢式货车 → 方案1：中部×0.8 + 底部×0.2
  - 其他货箱 → 方案2：中部×0.2 + 底部×0.8
"""
from __future__ import annotations

import base64
import logging
from io import BytesIO
from typing import Any

import cv2
import numpy as np
import requests

from config import AI_LOAD_RATE_URL

logger = logging.getLogger('mask_loading_rate')

WHITE_PIXEL_THRESHOLD = 250
TOP_ZONE_RATIO = 0.3
BOTTOM_ZONE_HEIGHT = 130


def is_box_container(container_type_name: str) -> bool:
    """是否厢式货车（用方案1）。"""
    name = (container_type_name or '').strip()
    return '厢式' in name


def _strip_b64(s: str) -> str:
    s = (s or '').strip()
    if s.startswith('data:') and ',' in s:
        s = s.split(',', 1)[1]
    return s


def decode_mask_gray(mask_b64: str) -> np.ndarray | None:
    try:
        raw = base64.b64decode(_strip_b64(mask_b64))
    except Exception:
        return None
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.ndim == 3:
        if img.shape[2] == 4:
            img = img[:, :, 0]
        else:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def calculate_from_mask(mask_b64: str) -> dict[str, Any]:
    """计算两套装载率及分区原始比率。返回值均为 [0,1]。"""
    empty = {
        'loading_rate': 0.0,
        'loading_rate_2': 0.0,
        'middle_ratio': 0.0,
        'bottom_ratio': 0.0,
        'mask_w': 0,
        'mask_h': 0,
    }
    gray = decode_mask_gray(mask_b64)
    if gray is None:
        return empty

    h, w = gray.shape[:2]
    if h <= BOTTOM_ZONE_HEIGHT:
        return {**empty, 'mask_w': w, 'mask_h': h}

    top_end = int(round(h * TOP_ZONE_RATIO))
    bottom_start = h - BOTTOM_ZONE_HEIGHT
    if top_end >= bottom_start:
        return {**empty, 'mask_w': w, 'mask_h': h}

    def white_ratio(y1: int, y2: int) -> float:
        zone = gray[y1:y2, :]
        if zone.size == 0:
            return 0.0
        return float(np.count_nonzero(zone >= WHITE_PIXEL_THRESHOLD)) / float(zone.size)

    middle = white_ratio(top_end, bottom_start)
    bottom = white_ratio(bottom_start, h)
    return {
        'loading_rate': round(middle * 0.8 + bottom * 0.2, 2),
        'loading_rate_2': round(middle * 0.2 + bottom * 0.8, 2),
        'middle_ratio': middle,
        'bottom_ratio': bottom,
        'mask_w': w,
        'mask_h': h,
    }


def pick_rate(rates: dict[str, Any], container_type_name: str) -> tuple[float, str]:
    """按货箱类型选方案，返回 (ratio 0-1, scheme 标签)。"""
    if is_box_container(container_type_name):
        return float(rates.get('loading_rate') or 0.0), 'scheme1_box'
    return float(rates.get('loading_rate_2') or 0.0), 'scheme2_other'


def extract_mask_b64(payload: dict) -> tuple[str, str, Any]:
    data = payload.get('data')
    if not isinstance(data, list) or not data:
        return '', '', None
    first = data[0]
    if not isinstance(first, dict):
        return '', '', None
    label = str(first.get('label') or '')
    score = first.get('score')
    mask = first.get('mask')
    if isinstance(mask, dict):
        return str(mask.get('base64') or ''), label, score
    if isinstance(mask, str):
        return mask, label, score
    return '', label, score


def call_ai_truck_xray_box(image_bytes: bytes, filename: str = 'xray.jpg') -> dict:
    """调用 AI，只要 mask，不要结果图。"""
    files = {
        'image': (filename or 'xray.jpg', BytesIO(image_bytes), 'application/octet-stream'),
    }
    data = {
        'task': 'truck_xray_box',
        'return_image': '0',
        'return_mask': '1',
        'score_threshold': '0.95',
    }
    resp = requests.post(AI_LOAD_RATE_URL, files=files, data=data, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f'AI HTTP {resp.status_code}: {(resp.text or "")[:300]}')
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError('AI 返回非 JSON object')
    return payload


def compute_ai_load_rate(
    image_bytes: bytes,
    container_type_name: str,
    filename: str = 'xray.jpg',
) -> dict[str, Any]:
    """端到端：AI mask → 分区加权 → 按货箱类型选方案。"""
    if not (container_type_name or '').strip():
        raise ValueError('缺少货箱类型')
    if not image_bytes:
        raise ValueError('缺少透视图')

    payload = call_ai_truck_xray_box(image_bytes, filename)
    mask_b64, label, score = extract_mask_b64(payload)
    if not mask_b64:
        raise RuntimeError('AI 未返回 data[0].mask.base64')

    rates = calculate_from_mask(mask_b64)
    selected, scheme = pick_rate(rates, container_type_name)
    pct = round(selected * 100, 2)

    logger.info(
        'AI load rate name=%s scheme=%s rate=%.2f%% mid=%.4f bot=%.4f label=%s',
        container_type_name, scheme, pct,
        rates.get('middle_ratio'), rates.get('bottom_ratio'), label,
    )

    return {
        'load_rate': selected,
        'load_rate_pct': pct,
        'scheme': scheme,
        'container_type_name': container_type_name,
        'loading_rate': rates['loading_rate'],
        'loading_rate_2': rates['loading_rate_2'],
        'middle_ratio': rates['middle_ratio'],
        'bottom_ratio': rates['bottom_ratio'],
        'mask_w': rates['mask_w'],
        'mask_h': rates['mask_h'],
        'label': label,
        'score': score,
    }
