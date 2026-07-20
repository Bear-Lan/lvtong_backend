"""设备管理 API 蓝图

提供设备状态查询、设备控制、重连等接口。
参考 Qt DeviceManager。
"""
from flask import Blueprint, request, jsonify

from dbm.dbdevice import DBDevice
from util.auth import login_required

device_api = Blueprint('device', __name__, url_prefix='/api/device')


def ok(data=None, message='success'):
    return jsonify({'code': 0, 'message': message, 'data': data})


def fail(code=400, message='error', data=None):
    return jsonify({'code': code, 'message': message, 'data': data}), code


@device_api.route('/status', methods=['GET'])
@login_required
def all_device_status():
    """获取所有设备状态

    参考 Qt DeviceManager::getAllDeviceStatus()
    """
    db = DBDevice()
    devices = db.getAllDevices()

    # 格式化设备状态信息
    status_list = []
    for d in devices:
        status_text = {0: '离线', 1: '在线', 2: '忙碌', 3: '错误'}.get(
            d.get('status', 0), '未知'
        )
        status_list.append({
            'deviceId': d['device_id'],
            'deviceName': d['device_name'],
            'deviceType': d['device_type'],
            'status': status_text,
            'statusCode': d['status'],
            'connected': d['status'] == 1,
            'ipAddress': d.get('ip_address', ''),
        })

    online_count = sum(1 for s in status_list if s['connected'])
    return ok({
        'devices': status_list,
        'total': len(status_list),
        'onlineCount': online_count,
        'offlineCount': len(status_list) - online_count,
    })


@device_api.route('/<device_id>/status', methods=['GET'])
@login_required
def device_status(device_id):
    """获取单个设备状态"""
    db = DBDevice()
    device = db.getDevice(device_id)
    if not device:
        return fail(404, '设备不存在')

    status_text = {0: '离线', 1: '在线', 2: '忙碌', 3: '错误'}.get(
        device.get('status', 0), '未知'
    )
    return ok({
        'deviceId': device['device_id'],
        'deviceName': device['device_name'],
        'deviceType': device['device_type'],
        'status': status_text,
        'statusCode': device['status'],
        'connected': device['status'] == 1,
    })


@device_api.route('/<device_id>/control', methods=['POST'])
@login_required
def device_control(device_id):
    """设备控制

    POST /api/device/<device_id>/control
    Body: { action: "setPLC"|"openGate"|"closeGate"|..., params: {...} }

    参考 Qt DeviceManager::executeDeviceAction() / PLCModbus::executeAction()
    """
    body = request.get_json(silent=True) or {}
    action = body.get('action', '')
    params = body.get('params', {})

    # 设备控制通过中间层转发到硬件设备服务
    # TODO: 对接实际的硬件中间层
    print(f'[DEVICE] 控制设备 {device_id}, action={action}, params={params}')

    return ok(message=f'设备 {device_id} 控制指令已发送: {action}')


@device_api.route('/reconnect', methods=['POST'])
@login_required
def reconnect_devices():
    """设备重连

    参考 Qt DeviceManager::attemptReconnect()
    """
    # TODO: 对接实际的设备重连逻辑
    print('[DEVICE] 执行设备重连')
    return ok(message='设备重连请求已处理')


@device_api.route('/health', methods=['GET'])
@login_required
def health_check():
    """设备健康检查

    参考 Qt DeviceManager::HealthCheck()
    """
    db = DBDevice()
    devices = db.getAllDevices()
    offline = [d for d in devices if d.get('status') != 1]

    healthy = len(offline) == 0
    return ok({
        'healthy': healthy,
        'total': len(devices),
        'offlineCount': len(offline),
        'offlineDevices': [d['device_name'] for d in offline],
    })
