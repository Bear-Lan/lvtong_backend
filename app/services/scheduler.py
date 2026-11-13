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
import math
import threading
from app.services.threshold import DistanceThreshold, PlcStateRule, DeviceActionConfig
from app.websocket.handler import push_radar_distance, push_detection_step, push_lane_occupied


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
        # 对齐 Qt：受理后 check_step=1，车道正常累计 >4 次再抬杆
        self._release_clear_count = 0
        self._gate_released = False
        # 放行仅按距离：连续5帧 >=27m 或 0（无限远）。
        self._gate_release_min_distance = 27.0
        self._gate_release_required_frames = 5
        self._tail_capture_offset_meters = 5.0
        self._lane_occupied_active = False
        self._capture_generation = 0
        # 车尾拍照：光幕下沿记距 x，待雷达 < x-5 再拍
        self._prev_light_screen = False
        self._tail_anchor_x: float | None = None
        self._tail_captured = False

    # ---- 控制 ----

    def start(self):
        """启动调度；新一轮检测前清掉阈值已执行标记。"""
        from app.services.system_config import value
        configured = value('scheduler.workflow')
        if configured is not None: self.load_configuration_from_json(configured)
        self._reset_threshold_status()
        self._radar_push_count = 0
        self._release_clear_count = 0
        self._gate_released = False
        self._reset_lane_occupied_state()
        if self._is_detection:
            self._update_lane_occupied_state(True)
        self._prev_light_screen = False
        self._tail_anchor_x = None
        self._tail_captured = False
        self._is_running = True

    def stop(self):
        self._is_running = False
        self._capture_generation += 1

    def _reset_threshold_status(self):
        for t in self._distance_thresholds:
            t.status = False
        for rule in self._plc_state_rules:
            rule.last_satisfied = False
        self._device_activation_status.clear()

    def set_detection_state(self, is_detection: bool):
        self._is_detection = is_detection
        if is_detection:
            self._release_clear_count = 0
            self._gate_released = False
            self._reset_lane_occupied_state()
            self._prev_light_screen = False
            self._tail_anchor_x = None
            self._tail_captured = False

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def current_distance(self) -> float:
        return self._current_distance

    @property
    def capture_blocked(self) -> bool:
        return self._is_running and self._lane_occupied_active

    @property
    def capture_generation(self) -> int:
        return self._capture_generation

    # ---- 雷达数据处理 ----

    def on_radar_data(
        self,
        distance: float,
        mode: int = 1,
        object_count: int | None = None,
        *,
        intrusion: bool | None = None,
    ):
        """接收雷达距离数据。

        所有阶段更新距离并推送小车位置；仅受理后 scheduler.start() 才做设备动作。
        intrusion：仅用于带明确占用标志的兼容协议；VMRMC 不由物体数推造占用。
        """
        if not math.isfinite(distance):
            if self._is_running:
                self._try_release_after_accept(0, distance, intrusion=intrusion)
            return

        self._current_distance = distance
        count = int(object_count) if object_count is not None else (1 if int(mode) > 0 else 0)

        # 实时显示独立于业务流程；空闲、预约、驳回/重置后同样跟随雷达。
        push_radar_distance(distance, mode, object_count=count)
        if not self._is_running:
            return
        # 低频日志，便于确认「受理后有距离 → 小车应移动」
        n = getattr(self, '_radar_push_count', 0) + 1
        self._radar_push_count = n
        if n == 1 or n % 20 == 0:
            print(
                f'[SCHEDULER] radar_distance → WS #{n} distance={distance:.2f} '
                f'mode={mode} objects={count} intrusion={intrusion}',
                flush=True,
            )

        # 现场 VMRMC 的物体数会在 0/1/2 间抖动，不能用 count != 1 判断占用。
        waiting_for_release = self._is_detection and not self._gate_released
        self._try_release_after_accept(count, distance, intrusion=intrusion)
        if waiting_for_release:
            # 净空确认帧只用于放行；避免第5帧同时命中27m落杆等后续阈值。
            return

        # 0 表示无限远，不能命中“车辆到达”或车尾拍照距离。
        if distance <= 0:
            return

        # 车尾拍照：光幕下沿记 x，雷达 < x-5 时触发
        self._try_tail_capture(distance)

        # 距离阈值 → 设备动作（仅受理后）
        self._process_distance_update(distance)

    def _try_release_after_accept(
        self,
        object_count: int,
        distance: float,
        *,
        intrusion: bool | None = None,
    ) -> None:
        """受理后连续5帧 >=27m 或 0（无限远）才清除占用并绿灯抬杆。

        目标数不参与判断；未满5帧或被近距离/无效帧打断时始终占用。
        兼容协议明确报告侵入时继续阻止放行。
        """
        if self._gate_released or not self._is_detection:
            return
        try:
            from app.api.booking import get_booking_state, set_booking_state, _now_iso

            st = get_booking_state()
            if int(st.get('check_step') or 0) != 1:
                return

            min_clear = float(self._gate_release_min_distance or 0)
            clear = math.isfinite(distance) and (distance == 0 or distance >= min_clear)
            occupied = bool(intrusion) or not clear

            if occupied:
                if self._release_clear_count:
                    reason = f'distance={distance:.2f}m intrusion={intrusion}'
                    print(
                        f'[SCHEDULER] 放行等待中断：{reason}，清零累计',
                        flush=True,
                    )
                self._release_clear_count = 0
                set_booking_state(radar_count=0)
                self._update_lane_occupied_state(True)
                return

            self._release_clear_count += 1
            set_booking_state(radar_count=self._release_clear_count)
            if self._release_clear_count < self._gate_release_required_frames:
                self._update_lane_occupied_state(True)
                if self._release_clear_count in (1, self._gate_release_required_frames - 1):
                    print(
                        f'[SCHEDULER] 放行确认中 {self._release_clear_count}/{self._gate_release_required_frames} '
                        f'(objects={object_count} distance={distance:.2f}m '
                        f'min={min_clear:.1f}m)',
                        flush=True,
                    )
                return

            self._update_lane_occupied_state(False)
            self._gate_released = True
            set_booking_state(
                check_step=2,
                radar_count=0,
                opengate_time=_now_iso(),
            )
            print('[SCHEDULER] 车道正常累计达标 → 绿灯抬杆放行', flush=True)
            geometry_generation = st.get('geometry_generation')

            def _do_release() -> None:
                from app.services.device_action import execute_device_action

                try:
                    execute_device_action('plc_001', 'setPLC', {
                        'redlight': False, 'yellowlight': False, 'greenlight': True,
                    })
                except Exception as e:
                    print(f'[SCHEDULER] 放行绿灯失败: {e}', flush=True)
                try:
                    execute_device_action('gate_001', 'open')
                except Exception as e:
                    print(f'[SCHEDULER] 放行开闸失败: {e}', flush=True)
                try:
                    from app.services.geometry_capture import start_geometry_capture
                    if not start_geometry_capture(geometry_generation):
                        print('[SCHEDULER] 几何采集未启动（已取消或发送失败）', flush=True)
                except Exception as error:
                    print(f'[SCHEDULER] 几何采集启动失败: {error}', flush=True)
                # 抬杆：geomcap、camcap，透视启用时再xraycap。
                try:
                    from app.services.body_camera_fetch import (
                        trigger_body_camera_capture,
                    )
                    trigger_body_camera_capture()
                except Exception as e:
                    print(f'[SCHEDULER] 车身相机 capture 失败: {e}', flush=True)
                try:
                    self._trigger_xray_start_scan()
                except Exception as e:
                    print(f'[SCHEDULER] 光机启扫失败: {e}', flush=True)
                try:
                    push_detection_step(2, '放行中', gate_released=True)
                except Exception:
                    pass

            threading.Thread(
                target=_do_release, daemon=True, name='gate-release',
            ).start()
        except Exception as e:
            print(f'[SCHEDULER] 放行判定异常: {e}', flush=True)

    def _reset_lane_occupied_state(self) -> None:
        self._capture_generation += 1
        self._lane_occupied_active = False

    def _update_lane_occupied_state(self, occupied: bool) -> None:
        """只在变化时推送；五帧确认由放行计数统一管理。"""
        if self._lane_occupied_active == occupied:
            return
        if occupied:
            self._capture_generation += 1
        self._lane_occupied_active = occupied
        push_lane_occupied(occupied)

    def _trigger_xray_start_scan(self) -> None:
        """抬杆时组播 $MDRMC,1,xraycap=true&{车头长}&{车型}（与 camcap 同时）。"""
        try:
            from app.api.booking import get_booking_state
            st = get_booking_state() or {}
            if not bool(st.get('is_check_xray', True)):
                print(
                    '[SCHEDULER] skip xray start_scan (透视关 is_check_xray=False)',
                    flush=True,
                )
                return
        except Exception:
            st = {}

        from app.services.device_action import execute_device_action
        scan_params = {
            'channel': '200',
            'vehicle_type': st.get('vehicle_type') or '',
            'car_length': st.get('car_length'),
        }
        execute_device_action('xray_200', 'start_scan', scan_params)

    def on_plc_status(self, parsed: dict) -> None:
        """组播 DEVICE：检测光幕下沿（lightScreen true→false），记当前距离为车尾拍照锚点。"""
        if not self._is_running or not self._is_detection or 'lightScreenStatus' not in parsed:
            return
        light = bool(parsed.get('lightScreenStatus'))
        falling = self._prev_light_screen and not light
        self._prev_light_screen = light
        if not falling or self._tail_captured:
            return
        if self._current_distance <= 0:
            return
        self._tail_anchor_x = self._current_distance
        print(
            f'[SCHEDULER] 光幕下沿 → 车尾锚点 x={self._tail_anchor_x:.2f}m',
            flush=True,
        )

    def _try_tail_capture(self, distance: float) -> None:
        """光幕下沿记 x 后，雷达距离 < x-5 时拍车尾（每轮检测一次）。"""
        if self.capture_blocked or self._tail_captured or self._tail_anchor_x is None:
            return
        trigger_at = self._tail_anchor_x - self._tail_capture_offset_meters
        if distance >= trigger_at:
            return
        self._tail_captured = True
        print(
            f'[SCHEDULER] 车尾拍照 distance={distance:.2f} < x-5={trigger_at:.2f} '
            f'(x={self._tail_anchor_x:.2f})',
            flush=True,
        )

        def _capture() -> None:
            from app.services.device_action import execute_device_action
            try:
                execute_device_action(
                    'camera_001', 'capture', {'category': 'tail'},
                )
            except Exception as e:
                print(f'[SCHEDULER] 车尾拍照失败: {e}', flush=True)

        threading.Thread(
            target=_capture, daemon=True, name='tail-capture',
        ).start()

    def _process_distance_update(self, distance: float):
        """处理距离更新

        同一距离可能落在多个重叠区间（如 initial_approach 10~100 与
        auto_capture_head 28~32）。必须把所有尚未执行的匹配阈值都激活，
        不能只取列表里第一个，否则宽区间会吞掉车头/车顶/X光等窄区间。
        """
        for threshold in self._matching_thresholds(distance):
            if self._should_activate(threshold, distance):
                self._activate_devices(threshold, distance)

        # 评估 PLC 规则
        self._evaluate_plc_rules()

    def _matching_thresholds(self, distance: float) -> list[DistanceThreshold]:
        """返回当前距离命中的全部有效阈值（可重叠）。"""
        return [
            t
            for t in self._distance_thresholds
            if t.is_active and t.min_distance <= distance <= t.max_distance
        ]

    def _find_matching_threshold(self, distance: float) -> DistanceThreshold | None:
        """兼容旧调用：返回第一个匹配项。新逻辑请用 _matching_thresholds。"""
        matched = self._matching_thresholds(distance)
        return matched[0] if matched else None

    def _should_activate(self, threshold: DistanceThreshold, distance: float) -> bool:
        """判断是否应激活设备（每条阈值整轮检测只执行一次）。"""
        captures = threshold.id == 'xray_capture_trigger' or any(
            config.action == 'capture' for config in threshold.device_actions.values()
        )
        if captures and self.capture_blocked:
            # 不消耗本轮拍照标记；占用解除且仍命中距离区间时可正常触发。
            return False
        if threshold.status:
            return False
        return True

    def _activate_devices(self, threshold: DistanceThreshold, distance: float):
        """激活设备动作 — 走 device_action（mock agent / 真实硬件自动切换）

        参考 Qt DistanceBasedScheduler::activateDevices()

        重要：本方法跑在雷达收包线程里。LED 串口 write_timeout、PLC/XRAY HTTP
        等同步调用会堵住 on_radar_data → 距离推送停顿 → 前端小车卡顿。
        因此设备动作一律丢到后台线程；时间戳与 detection_step 仍同步（轻量）。
        """
        threshold.status = True
        actions = [
            (device_id, action_config.action, dict(action_config.params or {}))
            for device_id, action_config in threshold.device_actions.items()
        ]
        for device_id, _, _ in actions:
            self._device_activation_status[device_id] = True

        # 雷达距离 25–28m：车身/200 透视 0.2s，LAZ 0.5s 轮询。
        if threshold.id == 'xray_capture_trigger':
            from app.api.booking import get_booking_state
            geometry_generation = get_booking_state().get('geometry_generation')
            def _start_25_28m_poll() -> None:
                try:
                    from app.services.geometry_capture import start_geometry_poll
                    start_geometry_poll(geometry_generation)
                except Exception as e:
                    print(f'[SCHEDULER] LAZ 轮询启动失败: {e}', flush=True)
                try:
                    from app.services.body_camera_fetch import start_body_camera_poll
                    start_body_camera_poll()
                except Exception as e:
                    print(f'[SCHEDULER] 车身相机轮询启动失败: {e}', flush=True)
                try:
                    from app.services.progressive_image_fetch import (
                        start_progressive_panel_fetch,
                    )
                    start_progressive_panel_fetch()
                except Exception as e:
                    print(f'[SCHEDULER] 200 透视轮询启动失败: {e}', flush=True)

            threading.Thread(
                target=_start_25_28m_poll,
                daemon=True,
                name='radar-25-28m-image-poll',
            ).start()
            return

        if actions:
            threshold_id = threshold.id

            def _run_actions() -> None:
                from app.services.device_action import execute_device_action
                for device_id, action, params in actions:
                    try:
                        execute_device_action(device_id, action, params)
                    except Exception as e:
                        print(
                            f'[SCHEDULER] async action failed '
                            f'{threshold_id}/{device_id}.{action}: {e}',
                            flush=True,
                        )

            threading.Thread(
                target=_run_actions,
                daemon=True,
                name=f'threshold-{threshold.id}',
            ).start()

        # 光幕时间由真实DEVICE沿记录，不能被距离阈值覆盖。
        _TIME_FIELD_MAP = {
            'auto_capture_top': 'cd_photo_time',
            'arrived': 'opengate_time',
        }
        field = _TIME_FIELD_MAP.get(threshold.id)
        if field:
            try:
                from app.api.booking import _booking_state as _bs
                from datetime import datetime
                _bs[field] = datetime.now().isoformat(timespec='seconds')
            except Exception:
                pass

        # 推送检测步骤更新
        self._push_step_update(threshold)

    def _trigger_plc_rule_actions(self, rule: PlcStateRule):
        """触发 PLC 规则关联的设备动作（异步，避免堵雷达线程）"""
        actions = [
            (device_id, action_config.action, dict(action_config.params or {}))
            for device_id, action_config in rule.device_actions.items()
        ]
        if not actions:
            return

        def _run() -> None:
            from app.devices.manager import DeviceManager
            mgr = DeviceManager()
            for device_id, action, params in actions:
                ctrl = mgr.get_device(device_id)
                if not ctrl:
                    continue
                try:
                    ctrl.execute_action(action, params)
                except Exception:
                    pass

        threading.Thread(
            target=_run, daemon=True, name=f'plc-rule-{rule.rule_id}',
        ).start()

    def _push_step_update(self, threshold):
        """根据阈值推送检测步骤"""
        step_map = {
            'initial_approach': (1, '车辆接近中'),
            'xray_capture_trigger': (4, 'X光采集中'),
            'auto_capture_head': (5, '拍照中'),
            'auto_capture_tail': (5, '拍照中'),
            'arrived': (6, '车辆到达'),
        }
        step_info = step_map.get(threshold.id, (None, None))
        if step_info[0] is not None:
            try:
                from app.websocket.handler import push_detection_step
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

    # ---- PLC 状态更新 ----

    def update_plc_state(self, state_key: str, value: bool):
        """更新 PLC 状态缓存"""
        self._plc_state_cache[state_key] = value

    # ---- 配置 ----

    def load_configuration_from_json(self, config: dict, car_head_length: float = 1.8):
        """从 JSON 加载阈值配置

        参考 Qt DistanceBasedScheduler::loadConfiguration()
        """
        from app.services.system_config import retire_legacy_distance_rules
        config = retire_legacy_distance_rules(config)
        thresholds_data = config.get('thresholds', [])
        self._distance_thresholds = []
        gate_release = config.get('gateRelease') or {}
        self._gate_release_required_frames = int(gate_release.get('requiredFrames', 5))
        self._tail_capture_offset_meters = float(config.get('tailCaptureOffsetMeters', 5.0))
        try:
            self._gate_release_min_distance = float(
                gate_release.get('minClearDistance', 27.0),
            )
        except (TypeError, ValueError):
            self._gate_release_min_distance = 27.0

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

        print(
            f'[SCHEDULER] 加载 {len(self._distance_thresholds)} 个距离阈值, '
            f'{len(self._plc_state_rules)} 个PLC规则, '
            f'抬杆最小净空距离={self._gate_release_min_distance:.1f}m',
        )

    def load_configuration_from_file(self, filepath: str, car_head_length: float = 1.8):
        """从文件加载配置

        参考 Qt DistanceBasedScheduler::loadConfigurationFromFile()
        """
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                config = json.load(f)
            from app.services.system_config import value
            config = value('scheduler.workflow', config)
            self.load_configuration_from_json(config, car_head_length)
        except FileNotFoundError:
            print(f'[SCHEDULER] 配置文件不存在: {filepath}')
        except json.JSONDecodeError as e:
            print(f'[SCHEDULER] 配置文件解析失败: {e}')
