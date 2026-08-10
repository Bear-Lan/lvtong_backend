"""设备动作执行：真实硬件 or 转发 mock_agent

flask_backend 的业务代码（如 accept_booking / reject_booking / urgent_stop）
调用 execute_device_action()：

- action=capture（调度器车头/车尾/车顶）：走 capture_service（RTSP 抽帧推前端，提交再落盘）
- 其它动作：MOCK_DEVICE_AGENT_URL 可达则转 mock，否则 DeviceManager

海康预览摄像头不在这里走：前端插件直连。
"""
import logging
import os
import threading
from typing import Any

import requests

logger = logging.getLogger('device_action')
if logger.level == logging.NOTSET or logger.level > logging.INFO:
    logger.setLevel(logging.INFO)

MOCK_DEVICE_AGENT_URL = os.environ.get('MOCK_DEVICE_AGENT_URL', 'http://127.0.0.1:8081')

_lock = threading.Lock()
_mock_enabled: bool = False
_mock_probed: bool = False


def _probe_mock_agent() -> bool:
    if not MOCK_DEVICE_AGENT_URL:
        return False
    try:
        r = requests.get(f'{MOCK_DEVICE_AGENT_URL}/', timeout=1)
        return r.status_code == 200
    except Exception:
        return False


def _is_mock_enabled() -> bool:
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

    device_id: 'plc_001' / 'gate_001' / 'led_001' / 'camera_001' / ...
    action: 'setPLC' / 'capture' / 'open' / 'close' 等
    """
    params = params or {}
    print(f'[DEVICE-ACTION] {device_id}.{action} params={params}', flush=True)

    # 调度器自动拍照：不走 camera 设备驱动 / mock，直接调采集服务
    if action == 'capture':
        return _execute_capture(params)

    if _is_mock_enabled():
        print(f'[DEVICE-ACTION] → mock agent ({MOCK_DEVICE_AGENT_URL})', flush=True)
        return _execute_via_mock_agent(device_id, action, params)
    print('[DEVICE-ACTION] → 真实硬件', flush=True)
    return _execute_via_hardware(device_id, action, params)


def _execute_capture(params: dict) -> bool:
    """车头/车尾/车顶等：HTTP 抓图并入库"""
    category = str(params.get('category') or 'head').strip() or 'head'
    print(f'[DEVICE-ACTION] → capture_service category={category}', flush=True)
    try:
        from app.services.capture_service import capture_category
        capture_category(category)
        return True
    except Exception as e:
        logger.warning(f'[CAPTURE] {category} 失败: {e}')
        print(f'[DEVICE-ACTION] capture 失败: {e}', flush=True)
        return False


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
    global _mock_enabled, _mock_probed
    with _lock:
        _mock_enabled = False
        _mock_probed = False
