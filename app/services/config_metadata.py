"""系统参数的中文说明；只描述字段，不参与设备命令或业务判断。"""
from __future__ import annotations

import copy

TABLE_COMMENT = '系统参数配置：中间件地址、组播、检测调度、语音及命令模板；参数值保存后重启后端生效。'
COLUMN_COMMENTS = {
    'key': '参数唯一键。点号前为分类，点号后为参数名；供程序读取，请勿改名。',
    'value': '参数值，使用JSON类型：字符串须带双引号，布尔值为true/false；对象内字段含义见本行description。',
    'category': '参数分类：middleware中间件，transport组播通信，scheduler检测调度，voice语音，commands命令，led显示屏，capture图像采集。',
    'description': '参数的中文用途、单位、取值及嵌套字段说明；由程序自动维护，不是设备指令。',
    'revision': '参数值的修订号，从0开始；通过配置接口保存时递增，用于防止多人修改互相覆盖。更新说明不递增。',
    'restart_required': '是否需要重启后端生效，true表示需要；当前配置均在后端启动时整批加载。',
    'updated_by': '最近修改参数值的账号；initial表示首次建档，migration开头表示数据迁移。更新说明不改动此列。',
}

# 兼容字段明确标注用途，避免把“库中存在”误解成“当前流程正在使用”。
PARAMETER_DESCRIPTIONS = {
    'middleware.IMAGE_FETCH_BASE': '兼容统一出图接口的基础地址，程序可追加type参数；当前车身、预约雷达、点云、200透视优先使用各自独立地址。',
    'middleware.PLACEHOLDER_IMAGE_ID': '旧版占位图编号，兼容保留；当前检测流程不使用此编号拉图。',
    'middleware.PLACEHOLDER_IMAGE_URL': '旧版占位图地址，兼容保留；当前车身图使用BODY_CAMERA_IMAGE_URL。',
    'middleware.USE_PLACEHOLDER_IMAGES': '中间件出图拉取开关：true允许透视等现有拉图入口，false跳过对应入口。名称为历史沿用，不表示启用LAZ演示数据。',
    'middleware.RADAR_HEAD_URL': '预约弹窗的二维雷达图接口地址，通常为5001端口的/appt/image；不是三维点云接口。',
    'middleware.RADAR_LAZ_AXES': 'LAZ二维投影使用的坐标轴及先后顺序，如xz表示横轴X、纵轴Z；两个轴必须不同且取自x/y/z，不改变点云原始坐标。',
    'middleware.RADAR_LAZ_METERS_PER_UNIT': '点云坐标换算成米的比例：原始坐标乘以此值；1表示原始单位为米，0.001表示原始单位为毫米。影响尺寸测量。',
    'middleware.RADAR_LAZ_RESOLUTION': 'LAZ二维投影的分辨率，单位米/像素；0.01表示每像素1厘米。不是点云采集间隔。',
    'middleware.RADAR_POINT_CLOUD_URL': '真实LAZ点云文件接口地址，通常为5001端口的/geom/laz；开启几何采集后轮询，Capture-Finish:true表示结束。',
    'transport.RADAR_TRANSPORT': '雷达接收方式：multicast表示UDP组播，unicast表示UDP单播。',
    'transport.MCAST_GROUP': 'UDP组播组IPv4地址，同时用于现有控制发令与组播接收；现场通常为224.0.0.100。',
    'transport.MCAST_PORT': 'UDP通信端口，整数1～65535；现场通常为9000。',
    'transport.MCAST_IFACE_IP': '发送组播命令所用的本机网卡IPv4地址；不是中间件IP。空字符串表示自动选择网卡。',
    'transport.MCAST_RECV_IFACE_IP': '加入组播接收组所用的本机网卡IPv4地址；可与发送网卡不同，空字符串时沿用发送网卡配置。',
    'middleware.XRAY_BASE_URL': '透视服务基础地址，兼容保留；当前透视图拉取优先使用TRANSPARENT_IMAGE_URL，启扫使用组播命令。',
    'middleware.XRAY_START_TRANSPARENT_URL': '旧200透视HTTP启扫地址，兼容保留；当前检测启扫使用commands.templates中的xraycap.start。',
    'middleware.XRAY_START_YR_TRANSPARENT_URL': '旧160透视HTTP启扫地址，兼容保留；当前不通过此地址单独启动160透视。',
    'middleware.BODY_CAMERA_CAPTURE_URL': '旧车身相机HTTP启停采集地址，兼容保留；当前车身采集通过camcap组播命令启停。',
    'middleware.BODY_CAMERA_IMAGE_URL': '当前车身图连续拉取地址，通常为5002端口的/camera/body/image；结束读取Capture-Finish响应头。',
    'middleware.BODY_IMAGE_URL': '兼容单帧车身图入口的地址；通常与BODY_CAMERA_IMAGE_URL保持一致，当前连续拉图以BODY_CAMERA_IMAGE_URL为准。',
    'middleware.BODY_TOP_IMAGE_URL': '旧二维车顶投影图接口，供兼容拉图入口保留；当前3D视图使用RADAR_POINT_CLOUD_URL。',
    'middleware.BODY_SIDE_IMAGE_URL': '旧二维车侧投影图接口，供兼容拉图入口保留；当前3D视图使用RADAR_POINT_CLOUD_URL。',
    'middleware.HEAD_CAPTURE_DEVICE_ID': '自动拍摄车头照片的设备编号，须对应devices表中的相机；不是相机IP。',
    'middleware.TAIL_CAPTURE_DEVICE_ID': '自动拍摄车尾照片的设备编号，须对应devices表中的相机；不是相机IP。',
    'middleware.TOP_CAPTURE_DEVICE_ID': '自动拍摄车顶照片的设备编号，须对应devices表中的相机；不是LAZ点云来源。',
    'middleware.XRAY_IMAGE_URL': '200透视图的基础拉取地址；当前连续拉取实际使用TRANSPARENT_IMAGE_URL，配置时通常保持两者一致。',
    'middleware.TRANSPARENT_IMAGE_URL': '当前200透视图连续拉取地址，通常为5003端口的/xray/image；Capture-Finish:true表示正常结束。',
    'middleware.YR_TRANSPARENT_IMAGE_URL': '160透视图兼容拉取地址；空字符串表示不启用该兼容拉图入口。',
    'middleware.STITCH_IMAGE_URL': '透视拼接图地址，兼容保留；当前流程未接入，通常留空。',
    'middleware.AI_DETECT_URL': '货车、货箱等AI检测服务地址，通常以/detect结尾；不是摄像头视频地址。',
    'middleware.AI_LICENSE_CROP_URL': '行驶证图片AI裁剪接口地址；首次建档未单配时沿用AI_DETECT_URL，建档后两项独立维护。',
    'middleware.AI_LOAD_RATE_URL': '货物装载率AI分割接口地址；首次建档未单配时沿用AI_DETECT_URL，建档后两项独立维护。',
    'middleware.OCR_SERVICE_URL': '行驶证文字识别服务基础地址，用于读取号牌、核定载质量、总质量等信息。',
    'middleware.TTS_CONTROL_URL': '旧HTTP语音控制地址，兼容保留；当前预置语音通过UDP组播发送，不使用此地址。',
    'scheduler.workflow': '检测距离调度及PLC兼容规则。距离单位均为米；业务空闲只由提交或重置恢复，光幕关闭只恢复语音1资格。',
    'voice.announce': '前后语音播报方向、业务语音编号及循环间隔；通过UDP组播交给中间件执行，不使用海康对讲。',
    'voice.playbackDurationSec': '前后共用的内部播放保护时长，单位秒；任一方向成功发送后重新计时。保护期间语音1不抢播，其他业务语音可抢占。不是设备播放完成回执。',
    'voice.varmcFreshSec': 'VARMC播报信号有效期，单位秒；超过该时长未收到合法VARMC时暂停语音1。与VMRMC小车距离显示独立。',
    'commands.templates': '组播命令体模板：键为程序内部业务名，值为实际发送给中间件的命令体；占位符见各字段。模板不含报文头和命令数量。',
    'commands.prefix': '组播控制报文头，通常为$MDRMC；发送器自动追加命令数量与各命令体。',
    'led.steps': '业务播报步骤到LED预置画面编号的映射，前后屏使用相同编号；编号为DISPLAY1～DISPLAY9，不可直接照搬所有语音编号。',
    'capture.pollIntervalSec': '车身、透视及LAZ轮询每轮处理后的等待时长，单位秒；实际请求周期还包含网络和解析耗时，不是雷达收包或小车刷新周期。',
    'capture.maxPollSec': '每轮图像或LAZ拉取的最长轮询时长，单位秒；正常结束以接口结束标志为准。实际退出还受在途请求超时影响。',
    'capture.connectTimeoutSec': '单次中间件拉图请求的建立连接超时，单位秒；与整轮最长轮询时长不同。',
    'capture.readTimeoutSec': '单次中间件拉图请求的读取超时，单位秒；不是整轮检测时长。',
}

_COMMAND_FIELDS = {
    'urgentstop': '整机急停复位兼容键；现有业务急停另走xraycap停止入口。',
    'redlight': '红灯开关。', 'yellowlight': '黄灯开关。', 'greenlight': '绿灯开关。',
    'filllight': '补光灯开关。', 'soundalarm': '声音报警开关，区别于前后预置语音。',
    'boombarrier': '栏杆开关：true抬杆，false落杆。',
    'interlock200': '200内锁兼容命令名，按现场协议核对实际模板。',
    'interlock160': '160内锁兼容命令名，按现场协议核对实际模板。',
    'xraygate200': '200光闸兼容命令名，按现场协议核对实际模板。',
    'xraygate160': '160光闸兼容命令名，按现场协议核对实际模板。',
    'xraymotorreset200': '200光机电机复位兼容命令名。',
    'xraymotorreset160': '160光机电机复位兼容命令名。',
    'xraysource200': '200光源兼容命令名，按现场协议核对实际模板。',
    'xraysource160': '160光源兼容命令名，按现场协议核对实际模板。',
    'camcap': '线扫车身相机采集开关：true开启，false停止。',
    'geomcap': '车辆几何点云采集开关：true开启，false停止。',
    'frontvoiceannounce': '前方预置语音编号，取VOICE1～VOICE9。',
    'backvoiceannounce': '后方预置语音编号，取VOICE1～VOICE9。',
    'frontleddisplay': '前方LED预置画面编号，取DISPLAY1～DISPLAY9。',
    'backleddisplay': '后方LED预置画面编号，取DISPLAY1～DISPLAY9。',
    'leddisplay': '旧单屏显示兼容命令名；当前业务分别发送前后LED命令。',
}
_STEP_TEXT = {
    '1': '绿通车辆，按键检测', '2': '申请已接收，请原地等待',
    '3': '申请已受理，请等待放行', '4': '待检车辆，请通行',
    '5': '检测中，请勿跟车', '6': '请打开车门',
    '7': '请勿遮挡车厢', '8': '拍摄完成，请关好车门',
    '9': '前方超限车辆倒车，请注意避让',
}
FIELD_DESCRIPTIONS = {
    'scheduler.workflow': {
        'gateRelease': '受理后的首次净空放行条件，每轮检测只自动放行一次。',
        'gateRelease.minClearDistance': '最小净空距离，单位米；距离大于等于此值或等于0视为净空，0表示无限远。',
        'gateRelease.requiredFrames': '净空条件连续满足的雷达帧数；任一近距、无效或明确侵入帧清零重新累计，不是秒数。',
        'gateRelease.description': '净空规则的中文备注，仅说明用途，不参与判断。',
        'tailCaptureOffsetMeters': '光幕下降沿记录距离x后，雷达距离小于x减此值时触发车尾拍照，单位米。',
        'thresholds': '距离区间触发规则数组；受理放行后处理，每条规则每轮执行一次，可有重叠区间。',
        'thresholds[].id': '规则唯一标识，程序用它识别落杆、拍照、拉图等特定行为；不要改名或重复。',
        'thresholds[].minDistance': '触发区间下限，单位米，包含边界。',
        'thresholds[].maxDistance': '触发区间上限，单位米，包含边界，不能小于下限。',
        'thresholds[].description': '规则中文说明；具体执行行为以id和deviceActions为准。',
        'thresholds[].isActive': '是否启用本条距离规则：true启用，false停用。',
        'thresholds[].deviceActions': '按设备编号组织的动作字典；空对象表示没有通用设备动作，特定id仍可能触发拉图等内置逻辑。',
        'thresholds[].deviceActions.<设备编号>': '动作目标设备，例如gate_001栏杆、plc_001控制器、camera_001抓拍入口。',
        'thresholds[].deviceActions.<设备编号>.action': '动作名，例如open抬杆、close落杆、capture抓拍、setPLC设置开关。',
        'thresholds[].deviceActions.<设备编号>.params': '动作参数对象；没有参数时保留空对象。',
        'thresholds[].deviceActions.<设备编号>.params.category': '抓拍类别：head车头、tail车尾、top车顶，实际相机由对应设备编号参数决定。',
        'plcStateRules': 'PLC兼容规则数组；当前加载器读取条件和关联设备，但不加载该数组中的deviceActions，不能靠此配置新增安全联锁或设备动作。',
        'plcStateRules[].ruleId': 'PLC规则唯一标识。',
        'plcStateRules[].description': 'PLC规则中文备注；备注不代表已实现相应联动。',
        'plcStateRules[].plcDeviceId': 'PLC设备编号，通常为plc_001。',
        'plcStateRules[].isActive': '是否启用该兼容规则。',
        'plcStateRules[].ruleType': '兼容规则类型：activation激活、trigger触发；不是硬件安全联锁开关。',
        'plcStateRules[].deviceIds': '关联设备编号列表；仅填写设备列表不会产生动作。',
        'plcStateRules[].conditions': 'PLC状态条件数组，条件全部满足才匹配。',
        'plcStateRules[].conditions[].stateKey': '程序内PLC状态字段名，例如urgentStopStatus、lightGate200Status、lightGate160Status。',
        'plcStateRules[].conditions[].expectedValue': '期望状态，JSON布尔值true或false。',
    },
    'voice.announce': {
        'targets': '播报方向数组：front前方、back后方；两边都播填["front","back"]，空数组不发送语音。',
        'steps': '业务步骤到中间件预置语音编号的映射，值为VOICE1～VOICE9。',
        **{'steps.' + step: '业务语音' + step + '：' + message + '。' for step, message in _STEP_TEXT.items()},
        'repeat_interval_sec': '语音1循环发送间隔，单位秒；需满足VARMC新鲜度及播报资格，且不能打断其他语音。',
        'prompt_clip_sec': '9号提示循环中每条播放时长的计时依据，单位秒，不是设备完成回执。',
        'prompt_gap_sec': '9号提示循环在每条计时结束后的停顿时长，单位秒。',
    },
    'commands.templates': {
        **{key: text + ' 模板中的{value}由程序替换为该次动作的值。' for key, text in _COMMAND_FIELDS.items()},
        'xraycap.start': '整体X光采集启动模板；{length}是预约车头避让距离，单位米，{vehicle_type}为small小型车或large大型车。不是独立光源命令。',
        'xraycap.stop': '整体X光采集停止命令体，不含占位符；供急停、取消、重置、异常停止等入口使用。',
    },
    'led.steps': {
        '1': '按键检测提示，通常为DISPLAY1。', '2': '已接收、原地等待，通常为DISPLAY2。',
        '3': '已受理、等待放行，通常为DISPLAY3。', '4': '待检车辆通行，通常为DISPLAY4。',
        '5': '检测中请勿跟车，通常为DISPLAY5。',
        '6': '兼容旧检测中步骤，通常仍为DISPLAY5，不是语音6的开门文案。',
        '7': '兼容旧完成通行步骤，通常为DISPLAY4，不是语音7的勿遮挡文案。',
    },
}


def field_descriptions(key: str) -> dict[str, str]:
    """接口返回独立注释字典，不向运行参数JSON塞入注释字段。"""
    return copy.deepcopy(FIELD_DESCRIPTIONS.get(key, {}))


def description(key: str) -> str:
    """数据库中也保存嵌套说明，直接查看表时无需另调接口。"""
    title = PARAMETER_DESCRIPTIONS[key]
    notes = FIELD_DESCRIPTIONS.get(key, {})
    return title + ''.join(f'\n{path}：{note}' for path, note in notes.items())
