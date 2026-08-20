"""车道语音/LED 播报编排（对齐现场 5 条规则）

语音：预置 wav→μ-law → 预约机 TwoWayAudio；LED 同步 set_step。

1. 系统空闲 + 雷达有目标 → 每 5 秒「绿通车辆，按键检测」；按预约键停止
2. 预约键上升沿 → 立刻「申请已接收，请原地等待」
3. 受理成功 → 立刻「申请已受理，请等待放行」
4. 受理后抬杆放行 → 「待检车辆，请通行」
5. 检测中光幕上升沿 → 「检测中，请勿跟车」
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Optional


def _ts() -> str:
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]


class LaneAnnounceService:
    IDLE_INTERVAL_SEC = 5.0

    _instance: Optional['LaneAnnounceService'] = None
    _inst_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._radar_target = False
        self._idle_looping = False
        self._idle_thread: threading.Thread | None = None
        self._prev_booking = False
        self._prev_light = False
        self._said_session: set[int] = set()
        self._release_pending = False

    @classmethod
    def instance(cls) -> 'LaneAnnounceService':
        with cls._inst_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ---------- 对外：雷达 / PLC ----------

    def on_radar(self, _distance: float, mode: int) -> None:
        """有车(mode>0 / 物体个数>0)且空闲 → 维持招揽线程；非空闲立刻停。"""
        target = int(mode) > 0
        with self._lock:
            self._radar_target = target
            idle = self._is_system_idle_unlocked()
        if target and idle:
            self._ensure_idle_loop()
        elif not idle:
            self._stop_idle_loop()

    def on_plc_status(self, parsed: dict) -> None:
        booking = bool(parsed.get('bookingStatus'))
        light = bool(parsed.get('lightScreenStatus'))

        with self._lock:
            booking_rise = booking and not self._prev_booking
            light_rise = light and not self._prev_light
            self._prev_booking = booking
            self._prev_light = light
            detecting = self._is_detecting_unlocked()

        if booking_rise:
            self.on_booking_button()

        if light_rise and detecting:
            self.on_light_curtain()

    # ---------- 对外：业务事件 ----------

    def on_booking_button(self, *, push_ws: bool = True, record: bool = True) -> None:
        self.stop_idle_loop()
        if record:
            try:
                from app.api.booking import mark_book_button

                btn_time = mark_book_button()
                print(f'[{_ts()}] [ANNOUNCE] 预约键 → step2, 记时 {btn_time}', flush=True)
            except Exception as e:
                print(f'[{_ts()}] [ANNOUNCE] 预约键记时失败: {e}', flush=True)

        self._play(2, interrupt=True)

        if push_ws:
            try:
                from app.websocket.handler import push_booking_event

                push_booking_event('book_button')
            except Exception as e:
                print(f'[{_ts()}] [ANNOUNCE] 推送 book_button 失败: {e}', flush=True)

    def on_accepted(self) -> None:
        self.stop_idle_loop()
        with self._lock:
            self._release_pending = True
            self._said_session.discard(4)
            self._said_session.discard(5)
        if self._announce_session_once(3):
            print(f'[{_ts()}] [ANNOUNCE] 受理 → step3', flush=True)

    def on_gate_open(self) -> None:
        with self._lock:
            pending = self._release_pending
            if pending:
                self._release_pending = False
        if not pending:
            return
        if self._announce_session_once(4):
            print(f'[{_ts()}] [ANNOUNCE] 抬杆放行 → step4', flush=True)

    def on_light_curtain(self) -> None:
        if self._announce_session_once(5):
            print(f'[{_ts()}] [ANNOUNCE] 光幕 → step5', flush=True)
            try:
                from app.api.booking import set_booking_state, _now_iso

                set_booking_state(openlightscreen_time=_now_iso())
            except Exception:
                pass

    def on_reset_to_idle(self) -> None:
        with self._lock:
            self._release_pending = False
            self._said_session.clear()
            target = self._radar_target
            idle = self._is_system_idle_unlocked()
        self._stop_idle_loop()
        if target and idle:
            self._ensure_idle_loop()
        print(f'[{_ts()}] [ANNOUNCE] 已复位，可恢复空闲播报', flush=True)

    def stop_idle_loop(self) -> None:
        self._stop_idle_loop()

    # ---------- 内部 ----------

    def _is_detecting_unlocked(self) -> bool:
        try:
            from app.api.booking import get_booking_state

            st = get_booking_state()
            return bool(st.get('is_detection')) or int(st.get('check_step') or 0) >= 1
        except Exception:
            return False

    def _is_system_idle_unlocked(self) -> bool:
        try:
            from app.api.booking import get_booking_state

            st = get_booking_state()
            if st.get('is_detection'):
                return False
            if int(st.get('check_step') or 0) >= 1:
                return False
            if st.get('booking_dialog_shown'):
                return False
            # 已按键待受理：不再招揽，等人工受理
            if st.get('btn_prebook_state'):
                return False
            return True
        except Exception:
            return True

    def _announce_session_once(self, step: int) -> bool:
        with self._lock:
            if step in self._said_session:
                return False
            self._said_session.add(step)
        self._play(step, interrupt=True)
        return True

    def _play(self, step: int, *, interrupt: bool = False, blocking: bool = False) -> None:
        try:
            from app.services.booking_camera_tts import speak_step

            speak_step(step, interrupt=interrupt, blocking=blocking)
        except Exception as e:
            print(f'[{_ts()}] [ANNOUNCE] 播报失败 step{step}: {e}', flush=True)
        try:
            from app.services.device_action import execute_device_action

            execute_device_action('led_001', f'set_step{step}')
        except Exception as e:
            print(f'[{_ts()}] [ANNOUNCE] LED step{step} 失败: {e}', flush=True)

    def _ensure_idle_loop(self) -> None:
        with self._lock:
            if self._idle_looping:
                return
            self._idle_looping = True
            t = threading.Thread(
                target=self._idle_worker,
                name='announce-idle',
                daemon=True,
            )
            self._idle_thread = t
            t.start()
            print(f'[{_ts()}] [ANNOUNCE] 空闲招揽线程已启动', flush=True)

    def _stop_idle_loop(self) -> None:
        with self._lock:
            was = self._idle_looping
            self._idle_looping = False
            self._idle_thread = None
        if was:
            print(f'[{_ts()}] [ANNOUNCE] 空闲招揽线程已停止', flush=True)

    def _idle_worker(self) -> None:
        """单线程：播完一整句，再等到满 5 秒，绝不叠播打断自己。"""
        while True:
            with self._lock:
                if not self._idle_looping:
                    return
                has_target = self._radar_target
                idle = self._is_system_idle_unlocked()

            if not idle:
                self._stop_idle_loop()
                return

            if not has_target:
                time.sleep(0.5)
                continue

            t0 = time.monotonic()
            print(
                f'[{_ts()}] [ANNOUNCE] 空闲招揽 → step1 开始推流（间隔目标 {self.IDLE_INTERVAL_SEC:.0f}s）',
                flush=True,
            )
            self._play(1, interrupt=False, blocking=True)

            with self._lock:
                if not self._idle_looping or not self._is_system_idle_unlocked():
                    print(
                        f'[{_ts()}] [ANNOUNCE] step1 结束（用时 {time.monotonic() - t0:.2f}s），招揽已停，不再等待',
                        flush=True,
                    )
                    return

            print(
                f'[{_ts()}] [ANNOUNCE] step1 本轮结束，已用 {time.monotonic() - t0:.2f}s，'
                f'再空等 {self.IDLE_INTERVAL_SEC:.0f}s',
                flush=True,
            )
            end = time.monotonic() + self.IDLE_INTERVAL_SEC
            while time.monotonic() < end:
                with self._lock:
                    if not self._idle_looping:
                        return
                    if not self._is_system_idle_unlocked():
                        self._stop_idle_loop()
                        return
                time.sleep(0.1)


def get_lane_announce() -> LaneAnnounceService:
    return LaneAnnounceService.instance()
