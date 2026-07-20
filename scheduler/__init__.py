"""距离调度器模块

从 Qt DistanceBasedScheduler 移植。
基于雷达距离触发设备动作的核心业务引擎。
"""
from scheduler.distance_scheduler import DistanceScheduler
from scheduler.threshold import DistanceThreshold, PlcStateRule, DeviceActionConfig
