"""设备管理 API 蓝图

提供设备状态查询、设备控制、重连等接口。
对齐 Qt DeviceManager。
"""
from flask import Blueprint, request, jsonify

from app.devices.manager import DeviceManager
from app.extensions.auth import login_required

device_api = Blueprint('device', __name__, url_prefix='/api/device')


def ok(data=None, message='success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    return jsonify({'code': code, 'message': message, 'data': data}), code


# ---- 状态查询 ----

@device_api.route('/status', methods=['GET'])
@login_required
def all_device_status():
    """获取所有设备实时状态

    对齐 Qt DeviceManager::getAllDeviceStatus()
    """
    mgr = DeviceManager()
    devices = mgr.get_all_status()
    return ok({
        'devices': devices,
        'total': len(devices),
        'onlineCount': mgr.online_count(),
        'offlineCount': mgr.offline_count(),
    })


@device_api.route('/<device_id>/status', methods=['GET'])
@login_required
def device_status(device_id):
    """获取单个设备状态"""
    mgr = DeviceManager()
    ctrl = mgr.get_device(device_id)
    if not ctrl:
        return fail(404, '设备不存在')
    return ok({
        'deviceId': ctrl.device_id,
        'deviceName': ctrl.device_name,
        'deviceType': ctrl.device_type,
        'status': {0: '离线', 1: '在线', 2: '忙碌', 3: '错误'}.get(int(ctrl.status), '未知'),
        'statusCode': int(ctrl.status),
        'connected': ctrl.is_online,
        'lastError': ctrl.last_error,
    })


# ---- 设备控制 ----

@device_api.route('/<device_id>/control', methods=['POST'])
@login_required
def device_control(device_id):
    """设备控制

    POST /api/device/<device_id>/control
    Body: { action: "setPLC"|"open"|"close"|"set_step1", params: {...} }

    对齐 Qt DeviceManager::executeDeviceAction()
    """
    body = request.get_json(silent=True) or {}
    action = body.get('action', '')
    params = body.get('params', {})

    mgr = DeviceManager()
    ctrl = mgr.get_device(device_id)
    if not ctrl:
        return fail(404, '设备不存在')

    if ctrl.status != 1:
        return fail(503, f'设备 {device_id} 当前离线，无法控制')

    success = ctrl.execute_action(action, params)
    if success:
        return ok(message=f'设备 {device_id} 执行 {action} 成功')
    else:
        return fail(500, f'执行失败: {ctrl.last_error}')


# ---- 重连 ----

@device_api.route('/reconnect', methods=['POST'])
@login_required
def reconnect_devices():
    """手动设备重连

    对齐 Qt DeviceManager::attemptReconnect()（onLinkClicked 触发）
    """
    mgr = DeviceManager()
    reconnected = mgr.manual_reconnect()
    return ok({
        'reconnected': reconnected,
        'count': len(reconnected),
    }, f'已重连 {len(reconnected)} 个设备')


@device_api.route('/health', methods=['GET'])
@login_required
def health_check():
    """设备健康检查 + AI/OCR 服务状态

    对齐 Qt DeviceManager::HealthCheck()
    """
    mgr = DeviceManager()
    healthy = mgr.health_check_all()
    devices = mgr.get_all_status()
    offline = [d for d in devices if not d['connected']]

    # 检测 AI 和 OCR 服务
    ai_online = _check_service_url('AI_MODEL_URL', 'http://192.168.88.245:8899')
    ocr_online = _check_service_url('OCR_SERVICE_URL', 'http://192.168.88.245:8890')

    return ok({
        'healthy': healthy,
        'total': len(devices),
        'offlineCount': len(offline),
        'offlineDevices': [d['deviceName'] for d in offline],
        'aiOnline': ai_online,
        'ocrOnline': ocr_online,
    })


@device_api.route('/plc-control', methods=['POST'])
@login_required
def plc_control():
    """PLC 开关控制

    POST /api/device/plc-control
    Body: { redlight: true, yellowlight: false, greenlight: false, ... }

    对齐 Qt 开关控制面板
    """
    body = request.get_json(silent=True) or {}
    if not body:
        return fail(400, '控制参数不能为空')

    mgr = DeviceManager()
    controllers = mgr.get_devices_by_type('controller')
    if not controllers:
        return fail(503, '未配置 PLC 控制器')

    success = False
    errors = []
    for plc in controllers:
        try:
            if plc.execute_action('setPLC', body):
                success = True
            else:
                errors.append(f'{plc.device_id}: {plc.last_error}')
        except Exception as e:
            errors.append(f'{plc.device_id}: {str(e)}')

    if not success:
        return fail(500, f'PLC 控制失败: {"; ".join(errors)}')

    return ok(message='PLC 控制指令已发送')


def _check_service_url(config_key: str, default_url: str) -> bool:
    """探活外部服务 URL"""
    import requests as req
    from flask import current_app
    url = current_app.config.get(config_key, default_url)
    try:
        resp = req.get(url, timeout=3)
        return resp.status_code < 500
    except Exception:
        return False
