"""设备动作执行：真实硬件 or 转发 mock_agent

flask_backend 的业务代码（如 accept_booking / reject_booking / urgent_stop）
调用 execute_device_action()：

- 如果设置了 MOCK_DEVICE_AGENT_URL（或探测到 mock_back），把动作 HTTP 转发给 mock agent
- 否则走真实 DeviceManager（调硬件）

摄像头不在这里走：camera1/2 由前端 useHikvisionPlayer 直连海康插件，
flask_backend 不参与。
"""
import logging
import os
import threading
from typing import Any

import requests

# 强制 INFO 级别（默认 WARNING 会让 logger.info 不打印）
logger = logging.getLogger('device_action')
if logger.level == logging.NOTSET or logger.level > logging.INFO:
    logger.setLevel(logging.INFO)

MOCK_DEVICE_AGENT_URL = os.environ.get('MOCK_DEVICE_AGENT_URL', 'http://127.0.0.1:8081')

_lock = threading.Lock()
_mock_enabled: bool = False
_mock_probed: bool = False


def _probe_mock_agent() -> bool:
    """探测 mock agent 是否就绪"""
    if not MOCK_DEVICE_AGENT_URL:
        return False
    try:
        r = requests.get(f'{MOCK_DEVICE_AGENT_URL}/', timeout=1)
        return r.status_code == 200
    except Exception:
        return False


def _is_mock_enabled() -> bool:
    """是否启用 mock agent 模式（探测一次后缓存结果）"""
    global _mock_enabled, _mock_probed
    if not _mock_probed:
        with _lock:
            if not _mock_probed:
                _mock_enabled = _probe_mock_agent()
                _mock_probed = True
                if _mock_enabled:
                    logger.info(f'[DEVICE] 启用 mock_agent 模式 ({MOCK_DEVICE_AGENT_URL})')
                else:
                    logger.info('[DEVICE] 使用真实硬件模式（mock_agent 不可达）')
    return _mock_enabled


def execute_device_action(device_id: str, action: str, params: dict[str, Any] | None = None) -> bool:
    """执行设备动作

    device_id: 'plc_001' / 'gate_001' / 'led_001' / 'udpradar_001'
    action: 'setPLC' / 'set_step4' / 'open' / 'close' 等
    params: 动作参数

    Returns: 是否成功
    """
    print(f'[DEVICE-ACTION] {device_id}.{action} params={params or {}}', flush=True)
    if _is_mock_enabled():
        print(f'[DEVICE-ACTION] → mock agent ({MOCK_DEVICE_AGENT_URL})', flush=True)
        return _execute_via_mock_agent(device_id, action, params or {})
    print('[DEVICE-ACTION] → 真实硬件', flush=True)
    return _execute_via_hardware(device_id, action, params or {})


def _execute_via_mock_agent(device_id: str, action: str, params: dict) -> bool:
    try:
        r = requests.post(
            f'{MOCK_DEVICE_AGENT_URL}/api/mock/device-action',
            json={'deviceId': device_id, 'action': action, 'params': params},
            timeout=2,
        )
        if r.status_code == 200:
            logger.info(f'[MOCK] {device_id}.{action}({params}) OK')
            return True
        logger.warning(f'[MOCK] {device_id}.{action} 失败: HTTP {r.status_code}')
        return False
    except Exception as e:
        logger.warning(f'[MOCK] {device_id}.{action} 异常: {e}')
        return False


def _execute_via_hardware(device_id: str, action: str, params: dict) -> bool:
    """通过真实 DeviceManager 执行（保留原 Qt 风格）"""
    from app.devices.manager import DeviceManager
    mgr = DeviceManager()
    ctrl = mgr.get_device(device_id)
    if not ctrl:
        logger.warning(f'[HW] 设备 {device_id} 未找到')
        return False
    try:
        result = ctrl.execute_action(action, params)
        logger.info(f'[HW] {device_id}.{action}({params}) → {result}')
        return bool(result) if result is not None else True
    except Exception as e:
        logger.warning(f'[HW] {device_id}.{action} 失败: {e}')
        return False


def reset_mock_cache() -> None:
    """重置 mock 探测缓存（测试用）"""
    global _mock_enabled, _mock_probed
    with _lock:
        _mock_enabled = False
        _mock_probed = False