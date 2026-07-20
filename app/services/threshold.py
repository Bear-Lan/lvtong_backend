"""调度器数据结构

参考 Qt DistanceBasedScheduler 中的结构体定义。
"""
from dataclasses import dataclass, field


@dataclass
class DeviceActionConfig:
    """设备动作配置"""
    action: str = ''
    params: dict = field(default_factory=dict)


@dataclass
class DistanceThreshold:
    """距离阈值配置

    参考 Qt DistanceThreshold 结构体
    """
    id: str = ''
    min_distance: float = 0.0           # 最小距离（米）
    max_distance: float = 0.0           # 最大距离（米）
    description: str = ''               # 描述
    is_active: bool = True              # 是否激活
    status: bool = False                # 是否已执行
    device_actions: dict[str, DeviceActionConfig] = field(default_factory=dict)
    # deviceId -> DeviceActionConfig


@dataclass
class PlcStateCondition:
    """PLC 状态条件"""
    state_key: str = ''
    expected_value: bool = True


@dataclass
class PlcStateRule:
    """PLC 状态规则

    参考 Qt PlcStateRule 结构体
    """
    rule_id: str = ''
    description: str = ''
    plc_device_id: str = ''
    conditions: list[PlcStateCondition] = field(default_factory=list)
    device_ids: list[str] = field(default_factory=list)
    device_actions: dict[str, DeviceActionConfig] = field(default_factory=dict)
    is_active: bool = True
    match_all: bool = True
    trigger_once: bool = False
    last_satisfied: bool = False
    rule_type: str = 'activation'  # "activation" | "deactivation"
