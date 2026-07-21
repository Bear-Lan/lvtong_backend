"""基于距离的设备调度器

核心业务引擎，从 Qt DistanceBasedScheduler 移植。

功能：
- 接收雷达距离数据
- 根据距离阈值触发设备动作
- 评估 PLC 状态规则
- 控制检测流程步骤

参考 Qt DistanceBasedScheduler (device/distancebasedscheduler.h/.cpp)
"""
import json
import threading
from app.services.threshold import DistanceThreshold, PlcStateRule, DeviceActionConfig
from ws.handler import push_radar_distance, push_detection_step, push_lane_occupied


class DistanceScheduler:
    """基于距离的设备调度器（单例）"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._is_running = False
        self._is_detection = False
        self._current_distance = 0.0
        self._radar_device_id = ''

        # 阈值配置
        self._distance_thresholds: list[DistanceThreshold] = []
        self._plc_state_rules: list[PlcStateRule] = []

        # PLC 状态缓存
        self._plc_state_cache: dict[str, bool] = {}

        # 设备激活状态跟踪
        self._device_activation_status: dict[str, bool] = {}

    # ---- 控制 ----

    def start(self):
        self._is_running = True

    def stop(self):
        self._is_running = False

    def set_detection_state(self, is_detection: bool):
        self._is_detection = is_detection

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def current_distance(self) -> float:
        return self._current_distance

    # ---- 雷达数据处理 ----

    def on_radar_data(self, distance: float, mode: int = 1):
        """接收雷达距离数据

        参考 Qt DistanceBasedScheduler::onRadarActionCompleted()
        """
        if not self._is_running:
            return

        self._current_distance = distance

        # 推送 WebSocket
        push_radar_distance(distance, mode)

        # 距离处理
        self._process_distance_update(distance)

    def _process_distance_update(self, distance: float):
        """处理距离更新

        参考 Qt DistanceBasedScheduler::processDistanceUpdate()
        """
        # 查找匹配的阈值
        threshold = self._find_matching_threshold(distance)

        if threshold and self._should_activate(threshold, distance):
            self._activate_devices(threshold, distance)

        # 评估 PLC 规则
        self._evaluate_plc_rules()

    def _find_matching_threshold(self, distance: float) -> DistanceThreshold | None:
        """查找匹配的距离阈值

        参考 Qt DistanceBasedScheduler::findMatchingThreshold()
        """
        for t in self._distance_thresholds:
            if t.is_active and t.min_distance <= distance <= t.max_distance:
                return t
        return None

    def _should_activate(self, threshold: DistanceThreshold, distance: float) -> bool:
        """判断是否应激活设备

        参考 Qt DistanceBasedScheduler::shouldActivateDevices()
        """
        if threshold.status:
            return False
        return True

    def _activate_devices(self, threshold: DistanceThreshold, distance: float):
        """激活设备动作 — 通过 DeviceManager 真实调用硬件

        参考 Qt DistanceBasedScheduler::activateDevices()
        """
        threshold.status = True
        from app.services.device_manager import DeviceManager
        mgr = DeviceManager()

        for device_id, action_config in threshold.device_actions.items():
            ctrl = mgr.get_device(device_id)
            if ctrl:
                try:
                    ctrl.execute_action(action_config.action, action_config.params)
                    print(f'[SCHEDULER] {threshold.description}: {device_id}.{action_config.action} 成功')
                except Exception as e:
                    print(f'[SCHEDULER] {threshold.description}: {device_id}.{action_config.action} 失败: {e}')
            else:
                print(f'[SCHEDULER] {threshold.description}: 设备 {device_id} 未找到')
            self._device_activation_status[device_id] = True

        # 推送检测步骤更新
        self._push_step_update(threshold)

    def _trigger_plc_rule_actions(self, rule: PlcStateRule):
        """触发 PLC 规则关联的设备动作"""
        from app.services.device_manager import DeviceManager
        mgr = DeviceManager()
        for device_id, action_config in rule.device_actions.items():
            ctrl = mgr.get_device(device_id)
            if ctrl:
                try:
                    ctrl.execute_action(action_config.action, action_config.params)
                except Exception as e:
                    print(f'[SCHEDULER] PLC规则 {rule.rule_id}: {device_id} 执行失败: {e}')

    def _push_step_update(self, threshold):
        """根据阈值推送检测步骤"""
        step_map = {
            'initial_approach': (1, '车辆接近中'),
            'led_step5_plc_yellow': (2, '闸机关闭，黄灯亮'),
            'led_step6': (3, 'X光准备'),
            'xray_capture_trigger': (4, 'X光采集中'),
            'auto_capture_head': (5, '拍照中'),
            'auto_capture_tail': (5, '拍照中'),
            'arrived': (6, '车辆到达'),
        }
        step_info = step_map.get(threshold.id, (None, None))
        if step_info[0] is not None:
            try:
                from ws.handler import push_detection_step
                push_detection_step(step_info[0], step_info[1])
            except Exception:
                pass

    def _evaluate_plc_rules(self):
        """评估 PLC 状态规则

        参考 Qt DistanceBasedScheduler::evaluatePlcRules()
        """
        for rule in self._plc_state_rules:
            if not rule.is_active:
                continue
            satisfied = self._is_plc_rule_satisfied(rule)
            if satisfied and not rule.last_satisfied:
                rule.last_satisfied = True
                self._trigger_plc_rule_actions(rule)
            elif not satisfied:
                rule.last_satisfied = False

    def _is_plc_rule_satisfied(self, rule: PlcStateRule) -> bool:
        for cond in rule.conditions:
            actual = self._plc_state_cache.get(cond.state_key, False)
            if actual != cond.expected_value:
                return False
        return True

    def _trigger_plc_rule_actions(self, rule: PlcStateRule):
        for device_id, action_config in rule.device_actions.items():
            print(f'[SCHEDULER] PLC规则触发 {rule.rule_id}: '
                  f'{device_id} -> {action_config.action}')

    # ---- PLC 状态更新 ----

    def update_plc_state(self, state_key: str, value: bool):
        """更新 PLC 状态缓存"""
        self._plc_state_cache[state_key] = value

    # ---- 配置 ----

    def load_configuration_from_json(self, config: dict, car_head_length: float = 1.8):
        """从 JSON 加载阈值配置

        参考 Qt DistanceBasedScheduler::loadConfiguration()
        """
        thresholds_data = config.get('thresholds', [])
        self._distance_thresholds = []
        for item in thresholds_data:
            t = DistanceThreshold(
                id=item.get('id', ''),
                min_distance=item.get('minDistance', 0),
                max_distance=item.get('maxDistance', 0),
                description=item.get('description', ''),
                is_active=item.get('isActive', True),
            )
            for key, action_data in item.get('deviceActions', {}).items():
                t.device_actions[key] = DeviceActionConfig(
                    action=action_data.get('action', ''),
                    params=action_data.get('params', {}),
                )
            self._distance_thresholds.append(t)

        rules_data = config.get('plcStateRules', [])
        self._plc_state_rules = []
        for item in rules_data:
            from app.services.threshold import PlcStateCondition
            rule = PlcStateRule(
                rule_id=item.get('ruleId', ''),
                description=item.get('description', ''),
                plc_device_id=item.get('plcDeviceId', ''),
                is_active=item.get('isActive', True),
                rule_type=item.get('ruleType', 'activation'),
                device_ids=item.get('deviceIds', []),
            )
            for cond_data in item.get('conditions', []):
                rule.conditions.append(PlcStateCondition(
                    state_key=cond_data.get('stateKey', ''),
                    expected_value=cond_data.get('expectedValue', True),
                ))
            self._plc_state_rules.append(rule)

        print(f'[SCHEDULER] 加载 {len(self._distance_thresholds)} 个距离阈值, '
              f'{len(self._plc_state_rules)} 个PLC规则')

    def load_configuration_from_file(self, filepath: str, car_head_length: float = 1.8):
        """从文件加载配置

        参考 Qt DistanceBasedScheduler::loadConfigurationFromFile()
        """
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                config = json.load(f)
            self.load_configuration_from_json(config, car_head_length)
        except FileNotFoundError:
            print(f'[SCHEDULER] 配置文件不存在: {filepath}')
        except json.JSONDecodeError as e:
            print(f'[SCHEDULER] 配置文件解析失败: {e}')
