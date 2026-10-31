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


# ---- 双工语音通道锁（TTS / 对讲互斥）----

@device_api.route('/voice/status', methods=['GET'])
@login_required
def voice_channel_status():
    """查询预约机 TwoWayAudio 占用状态。"""
    from app.services.voice_channel import get_voice_channel

    return ok(get_voice_channel().status())


@device_api.route('/voice/acquire', methods=['POST'])
@login_required
def voice_channel_acquire():
    """申请独占双工通道。

    Body: { owner: 'talk'|'announce', wait?: bool, timeout?: number, steal?: bool }
    """
    from app.services.voice_channel import get_voice_channel

    body = request.get_json(silent=True) or {}
    owner = str(body.get('owner') or '').strip()
    wait = bool(body.get('wait', False))
    steal = bool(body.get('steal', False))
    try:
        timeout = float(body.get('timeout', 30))
    except (TypeError, ValueError):
        timeout = 30.0

    # 前端对讲禁止 steal，避免误抢后台播报
    if owner == 'talk':
        steal = False

    result = get_voice_channel().acquire(
        owner, wait=wait, timeout=timeout, steal=steal
    )
    if not result.get('ok'):
        return fail(409, result.get('error') or 'voice channel busy', result)
    return ok(result, 'acquired')


@device_api.route('/voice/release', methods=['POST'])
@login_required
def voice_channel_release():
    """释放双工通道。

    Body: { owner?: 'talk'|'announce', token?: string, force?: bool }
    """
    from app.services.voice_channel import get_voice_channel

    body = request.get_json(silent=True) or {}
    owner = str(body.get('owner') or '').strip() or None
    token = str(body.get('token') or '').strip() or None
    force = bool(body.get('force', False))

    result = get_voice_channel().release(owner=owner, token=token, force=force)
    if not result.get('ok'):
        return fail(409, result.get('error') or 'release denied', result)
    return ok(result, 'released')


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


@device_api.route('/<device_id>/preview-config', methods=['GET'])
@login_required
def device_preview_config(device_id):
    """读取摄像头 Web 预览登录配置（只读 devices 表，不改库）

    供前端海康 WebSDK 直连设备使用。
    channelId / streamType 来自 config JSON，缺省均为 1。
    """
    from app.db.device import DBDevice

    row = DBDevice().getDevice(device_id)
    if not row:
        return fail(404, f'设备不存在: {device_id}')

    cfg = row.get('config') or {}
    if not isinstance(cfg, dict):
        cfg = {}

    ip = (row.get('ip_address') or '').strip()
    if not ip:
        return fail(400, f'设备 {device_id} 未配置 IP')

    try:
        port = int(row.get('port') or 80)
    except (TypeError, ValueError):
        port = 80

    try:
        channel_id = int(cfg.get('channelId', cfg.get('channel_id', 1)))
    except (TypeError, ValueError):
        channel_id = 1

    try:
        stream_type = int(cfg.get('streamType', cfg.get('stream_type', 1)))
    except (TypeError, ValueError):
        stream_type = 1

    try:
        protocol = int(cfg.get('protocol', 1))
    except (TypeError, ValueError):
        protocol = 1

    return ok({
        'deviceId': row.get('device_id'),
        'deviceName': row.get('device_name'),
        'deviceType': row.get('device_type'),
        'ip': ip,
        'port': port,
        'username': row.get('username') or 'admin',
        'password': row.get('password') or '',
        'channelId': channel_id,
        'streamType': stream_type,
        'protocol': protocol,
        'zeroChannel': bool(cfg.get('zeroChannel', cfg.get('zero_channel', False))),
    })


# ---- 设备控制 ----

@device_api.route('/<device_id>/ptz/continuous', methods=['POST'])
@login_required
def device_ptz_continuous(device_id):
    """球机连续云台控制（后端直连 ISAPI，不依赖浏览器插件）。"""
    from app.services.hik_ptz import PtzError, get_hik_ptz_service

    body = request.get_json(silent=True) or {}
    action = str(body.get('action') or '').strip().lower()
    direction = str(body.get('direction') or '').strip().lower()
    try:
        speed = int(body.get('speed', 45))
    except (TypeError, ValueError):
        return fail(400, 'speed 须为整数')
    try:
        data = get_hik_ptz_service().continuous(device_id, action, direction, speed)
    except PtzError as exc:
        return fail(502, str(exc))
    return ok(data, 'PTZ 指令已执行')


@device_api.route('/<device_id>/ptz/preset', methods=['POST'])
@login_required
def device_ptz_preset(device_id):
    """设置或调用球机预置位 1-9。"""
    from app.services.hik_ptz import PtzError, get_hik_ptz_service

    body = request.get_json(silent=True) or {}
    action = str(body.get('action') or '').strip().lower()
    try:
        preset_id = int(body.get('preset'))
    except (TypeError, ValueError):
        return fail(400, 'preset 须为整数')
    try:
        data = get_hik_ptz_service().preset(device_id, action, preset_id)
    except PtzError as exc:
        return fail(502, str(exc))
    return ok(data, '预置位指令已执行')


@device_api.route('/<device_id>/control', methods=['POST'])
@login_required
def device_control(device_id):
    """设备控制

    POST /api/device/<device_id>/control
    Body: { action: "setPLC"|"open"|"close"|"toggle"|"set_step1", params: {...} }

    对齐 Qt DeviceManager::executeDeviceAction()
    闸机 gate_001：open/close/toggle → 组播 $MDRMC,1,boombarrier=…
    PLC plc_001：setPLC → 组播 $MDRMC,…,redlight=true|…（见 mcast_cmd）
    LED led_001：set_stepN → 组播 $MDRMC,1,leddisplay=…
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

    is_gate = device_id == 'gate_001' or getattr(ctrl, 'device_type', '') == 'gate'
    gate_will_raise = False
    if is_gate and action in ('open', 'close', 'toggle'):
        if action == 'open':
            gate_will_raise = True
        elif action == 'toggle':
            gate_will_raise = not bool(getattr(ctrl, 'is_opened', False))

    success = ctrl.execute_action(action, params)
    if success:
        if is_gate and gate_will_raise:
            try:
                from app.devices.xray import close_xray_capture
                close_xray_capture()
            except Exception as e:
                print(f'[GATE] 抬杆 xraycap=false 失败: {e}', flush=True)
        data = None
        if is_gate:
            boom = getattr(ctrl, 'last_boom_result', {}) or {}
            # 有组播确认则用反馈；否则用本机记忆
            if boom.get('confirmed') and isinstance(boom.get('stickdown'), bool):
                gate_open = bool(boom['stickdown'])
            else:
                gate_open = bool(getattr(ctrl, 'is_opened', False))
            data = {
                'gateOpen': gate_open,
                'sent': boom.get('sent', True),
                'confirmed': bool(boom.get('confirmed')),
                'stickdown': boom.get('stickdown'),
                'packet': boom.get('packet'),
                'cmdRaw': boom.get('cmdRaw'),
                'note': boom.get('note') or '',
            }
            if boom.get('sent'):
                msg = f'栏杆组播已发送（{"抬起" if gate_open else "落下"}，不等待 DEVICE 确认）'
            else:
                msg = f'设备 {device_id} 执行 {action} 成功'
            return ok(data, message=msg)
        return ok(data, message=f'设备 {device_id} 执行 {action} 成功')
    else:
        return fail(500, f'执行失败: {ctrl.last_error}')


@device_api.route('/gate-status', methods=['GET'])
@login_required
def gate_status():
    """主闸栏杆状态 — 供主页闸机图标同步

    gateOpen 优先取组播 DEVICE cmd bit6（stickdown / 栏杆位）：
      stickdown=true(1) → 抬起 → gateOpen=true
      stickdown=false(0) → 落下 → gateOpen=false
    尚无组播时回退到本进程最近一次 open/close 记忆值。
    """
    mgr = DeviceManager()
    ctrl = mgr.get_device('gate_001')
    connected = bool(ctrl and ctrl.is_online)
    last_error = (ctrl.last_error if ctrl else '') or ''

    gate_open = None
    stickdown = None
    try:
        from app.websocket.handler import get_last_plc_status

        plc = get_last_plc_status()
        if plc is not None and 'stickdown' in plc:
            stickdown = bool(plc.get('stickdown'))
            gate_open = stickdown
            # 有组播反馈则认为栏杆链路可用（中间件在推 DEVICE）
            if not connected and plc.get('raw') is not None:
                connected = True
    except Exception:
        pass

    if gate_open is None:
        gate_open = bool(getattr(ctrl, 'is_opened', False)) if ctrl else False

    return ok({
        'connected': connected,
        'gateOpen': bool(gate_open),
        'stickdown': stickdown,
        'lastError': last_error,
    })


# ---- 重连 ----

@device_api.route('/body-radar-image', methods=['POST'])
@login_required
def body_radar_image():
    """切换显示车顶/车侧雷达图时按需拉图。

    Body: { view: 'top' | 'side' }
    """
    body = request.get_json(silent=True) or {}
    view = str(body.get('view') or '').strip().lower()
    if view not in ('top', 'side'):
        return fail(400, 'view 须为 top 或 side')

    from app.services.progressive_image_fetch import fetch_body_radar_image

    image_url = fetch_body_radar_image(view)
    if not image_url:
        return fail(502, f'{view} 雷达图拉取失败')

    slot = '2' if view == 'top' else '3'
    return ok({'imageUrl': image_url, 'view': view, 'slot': slot})


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
    """设备健康检查 + AI 服务状态

    对齐 Qt DeviceManager::HealthCheck()
    AI 探活：与 AI_DETECT_URL 同主机的 /health（行驶证裁剪等业务走 /detect）
    """
    mgr = DeviceManager()
    healthy = mgr.health_check_all()
    devices = mgr.get_all_status()
    offline = [d for d in devices if not d['connected']]

    from config import AI_DETECT_URL
    ai_health = _ai_health_url(AI_DETECT_URL)
    ai_online = _probe_url(ai_health)
    # 行驶证裁剪与货车/货箱识别均在同一 AI 服务；UI 两路灯共用探活结果
    ocr_online = ai_online

    return ok({
        'healthy': healthy,
        'total': len(devices),
        'offlineCount': len(offline),
        'offlineDevices': [d['deviceName'] for d in offline],
        'aiDetectOnline': ai_online,
        'aiDetectUrl': AI_DETECT_URL,
        'aiHealthUrl': ai_health,
        'aiOnline': ai_online,
        'ocrOnline': ocr_online,
    })


@device_api.route('/plc-status', methods=['GET'])
@login_required
def plc_status():
    """最近一次组播 PLC/DEVICE 状态（面板打开时拉取，避免错过 WS 首推）"""
    from app.websocket.handler import get_last_plc_status

    data = get_last_plc_status()
    if data is None:
        return ok(None, message='尚无 PLC 状态')
    return ok(data)


@device_api.route('/plc-control', methods=['POST'])
@login_required
def plc_control():
    """PLC 开关控制

    POST /api/device/plc-control
    Body: { redlight: true, yellowlight: false, greenlight: false, ... }

    下发：$MDRMC,<n>,key=true|false（组播，键名与协议一致）。
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


def _ai_health_url(detect_url: str) -> str:
    """AI_DETECT_URL (.../detect) → 同主机 /health。"""
    from urllib.parse import urlsplit, urlunsplit
    u = (detect_url or '').strip()
    if not u:
        return ''
    parts = urlsplit(u)
    if not parts.scheme or not parts.netloc:
        return ''
    return urlunsplit((parts.scheme, parts.netloc, '/health', '', ''))


def _probe_url(url: str) -> bool:
    """探活外部服务 URL（不走系统代理，避免 Clash 等把内网/专线打到 127.0.0.1）"""
    import requests as req
    if not url:
        return False
    try:
        # trust_env=False：忽略 HTTP(S)_PROXY，否则探活常被代理劫持失败
        resp = req.get(url, timeout=3, proxies={'http': None, 'https': None})
        return resp.status_code < 500
    except Exception:
        return False


def _check_service_url(config_key: str, default_url: str) -> bool:
    """探活外部服务 URL"""
    from flask import current_app
    url = current_app.config.get(config_key, default_url)
    return _probe_url(url)
