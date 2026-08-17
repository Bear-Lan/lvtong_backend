"""货车实物图识别 — 对齐 gongsi_LvTong truck_real（车轴/货箱）

一次调用同时得到：
  - wheel_count → 货车类型（一~六型，按轴数映射）
  - cratetype   → 货箱类型（与字典 type_code 一致）

输入：车身影像「车身图」。
"""
from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

import requests

from config import AI_DETECT_URL

logger = logging.getLogger('truck_real_detect')

# AI cratetype → 中文（与 VehicleConstants / 本地字典一致）
CRATETYPE_TEXT = {
    '1': '罐式货车',
    '2.1': '敞篷货车(平板式)',
    '2.2': '敞篷货车(栅栏式)',
    '3.1': '普通货车(篷布包裹式)',
    '4.1': '厢式货车(封闭货车)',
    '5.1': '特殊结构货车(水箱式)',
}


# 车牌颜色码（对齐前端 LicensePlateEdit）→ 是否按一型处理两轮车
# 0=蓝色 4=渐变绿色；兼容中文色名
_TYPE1_PLATE_COLORS = frozenset({
    '0', '4',
    '蓝', '蓝色', '蓝牌',
    '渐变绿', '渐变绿色',
})


def _normalize_plate_color(plate_color: Any) -> str:
    if plate_color is None:
        return ''
    return str(plate_color).strip()


def is_type1_plate_color(plate_color: Any) -> bool:
    """两轮车：蓝牌 / 渐变绿 → 一型；其余 → 二型。"""
    c = _normalize_plate_color(plate_color)
    return c in _TYPE1_PLATE_COLORS


def wheel_count_to_truck_type(
    wheel_count: int,
    plate_color: Any = '',
    plate_number: Any = '',
) -> tuple[str, str]:
    """侧视轮胎/车轴数 → 货车类型 type_code / type_name。

    AI 按车轮数识别车型。两轮（≤2）需结合车牌颜色：
      蓝牌 / 渐变绿 → 一型(11)；有色且非上述 → 二型(12)；
      未选色 / 车牌为空无法判断 → 不赋值（空）。
    3 轴→二型 … 6 轴→五型，≥7→六型。
    """
    axles = int(wheel_count or 0)
    if axles <= 0:
        return '', ''
    if axles <= 2:
        # 两轮：必须有车牌号 + 颜色才能判定
        color = _normalize_plate_color(plate_color)
        plate = str(plate_number or '').strip()
        if not color or not plate or plate in ('--', '-', '无'):
            return '', ''
        code = '11' if is_type1_plate_color(color) else '12'
    elif axles >= 7:
        code = '16'
    else:
        # 3→12 … 6→15（与原先轴数阶梯一致）
        code = str(9 + axles)
    name_map = {
        '11': '一型货车',
        '12': '二型货车',
        '13': '三型货车',
        '14': '四型货车',
        '15': '五型货车',
        '16': '六型货车',
    }
    return code, name_map.get(code, '')


def cratetype_to_container(cratetype: Any) -> tuple[str, str]:
    """AI cratetype → 货箱类型 type_code / type_name。"""
    if cratetype is None:
        return '', ''
    code = str(cratetype).strip()
    if not code:
        return '', ''
    text = CRATETYPE_TEXT.get(code, '')
    return code, text


def _count_wheels_in_data(data: list) -> int:
    n = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        label = str(item.get('label') or item.get('base_label') or '').lower()
        if label == 'wheel' or label.startswith('wheel'):
            n += 1
    return n


def call_ai_truck_real(image_bytes: bytes, filename: str = 'body.jpg') -> dict:
    files = {
        'image': (filename or 'body.jpg', BytesIO(image_bytes), 'application/octet-stream'),
    }
    data = {
        'task': 'truck_real',
        'return_image': '0',
        'return_mask': '0',
        'score_threshold': '0.95',
    }
    resp = requests.post(AI_DETECT_URL, files=files, data=data, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f'AI HTTP {resp.status_code}: {(resp.text or "")[:300]}')
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError('AI 返回非 JSON object')
    return payload


def detect_truck_and_container(
    image_bytes: bytes,
    filename: str = 'body.jpg',
    plate_color: Any = '',
    plate_number: Any = '',
) -> dict[str, Any]:
    """端到端：车身图 → 货车类型 + 货箱类型。

    plate_color / plate_number：两轮车判定一/二型时使用；缺任一则不赋货车类型。
    """
    if not image_bytes:
        raise ValueError('缺少车身图')

    payload = call_ai_truck_real(image_bytes, filename)

    wheel_count = payload.get('wheel_count')
    try:
        wheel_count_i = int(float(wheel_count)) if wheel_count is not None else 0
    except (TypeError, ValueError):
        wheel_count_i = 0

    data = payload.get('data') if isinstance(payload.get('data'), list) else []
    if wheel_count_i <= 0:
        wheel_count_i = _count_wheels_in_data(data)

    truck_code, truck_name = wheel_count_to_truck_type(
        wheel_count_i, plate_color, plate_number,
    )
    crate = payload.get('cratetype')
    if crate is None or str(crate).strip() == '':
        label_to_crate = {
            'guanshi_truck': '1',
            'changpeng_truck_board': '2.1',
            'changpeng_truck_bar': '2.2',
            'normal_truck_cover': '3.1',
            'box_truck_block': '4.1',
            'special_truck_water': '5.1',
        }
        for item in data:
            if not isinstance(item, dict):
                continue
            label = str(item.get('label') or '')
            if label in label_to_crate:
                crate = label_to_crate[label]
                break

    container_code, container_name = cratetype_to_container(crate)
    if container_code and not container_name:
        container_name = CRATETYPE_TEXT.get(container_code, container_code)

    # 用本地字典名覆盖（若库中文案不同）
    try:
        from app.db.dictionary import DBDictionary
        db = DBDictionary()
        if truck_code:
            for row in db.getAllTruckTypes():
                if str(row.get('type_code')) == truck_code:
                    truck_name = str(row.get('type_name') or truck_name)
                    break
        if container_code:
            for row in db.getAllContainerTypes():
                if str(row.get('type_code')) == container_code:
                    container_name = str(row.get('type_name') or container_name)
                    break
    except Exception:
        pass

    logger.info(
        'truck_real wheel=%s plate=%s truck=%s/%s crate=%s container=%s/%s',
        wheel_count_i, _normalize_plate_color(plate_color),
        truck_code, truck_name, crate, container_code, container_name,
    )

    return {
        'wheel_count': wheel_count_i,
        'plate_color': _normalize_plate_color(plate_color),
        'cratetype': str(crate).strip() if crate is not None else '',
        'vehicle_type': truck_code,
        'vehicle_type_name': truck_name,
        'vehicle_container_type': container_code,
        'vehicle_container_name': container_name,
    }
