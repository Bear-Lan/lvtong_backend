"""车道语音/LED 播报编排（对齐现场规则）

语音：预置 wav→μ-law → 预约机 TwoWayAudio；LED 同步 set_step → $MDRMC leddisplay=。

1. 系统空闲 + 雷达有目标 → 「绿通车辆，按键检测」；长连接循环推流
2. 预约键（任意时刻）→ 红灯+落杆 + step2 语音/LED；空闲才通知前端弹窗
3. 受理成功 → 「申请已受理，请等待放行」
4. 受理后抬杆放行 → 「待检车辆，请通行」
5. 检测中光幕上升沿 → 「检测中，请勿跟车」
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional


def _ts() -> str:
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]


class LaneAnnounceService:
    IDLE_INTERVAL_SEC = 3.0

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
        else:
            self._stop_idle_loop()

    def on_plc_status(self, parsed: dict) -> None:
        """DEVICE 位：预约键 / 光幕上升沿。"""
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
        """预约按键（现场 / 操作台）：任意时刻都红灯+落杆+step2；空闲才让前端弹窗。"""
        self.stop_idle_loop()
        btn_time = ''
        if record:
            try:
                from app.api.booking import mark_book_button

                btn_time = mark_book_button()
                print(f'[{_ts()}] [ANNOUNCE] 预约键 → 记时 {btn_time}', flush=True)
            except Exception as e:
                print(f'[{_ts()}] [ANNOUNCE] 预约键记时失败: {e}', flush=True)

        # 红灯 + 落杆（与是否空闲无关）
        def _hold() -> None:
            try:
                from app.services.device_action import execute_device_action

                execute_device_action('plc_001', 'setPLC', {
                    'redlight': True, 'yellowlight': False, 'greenlight': False,
                })
                execute_device_action('gate_001', 'close')
                print(f'[{_ts()}] [ANNOUNCE] 预约键 → 红灯+落杆', flush=True)
            except Exception as e:
                print(f'[{_ts()}] [ANNOUNCE] 预约键红灯/落杆失败: {e}', flush=True)

        threading.Thread(target=_hold, daemon=True, name='book-hold').start()

        # step2 语音+LED：与 /open 共用同一轮，避免弹窗再播一遍
        def _step2() -> None:
            try:
                from app.api.booking import ensure_step2_announce_once

                ensure_step2_announce_once()
            except Exception as e:
                print(f'[{_ts()}] [ANNOUNCE] 预约键 step2 失败: {e}', flush=True)
                self._play(2, interrupt=True)

        threading.Thread(target=_step2, daemon=True, name='book-step2').start()

        with self._lock:
            open_dialog = not self._is_detecting_unlocked()

        if push_ws:
            try:
                from app.websocket.handler import push_booking_event

                push_booking_event(
                    'book_button',
                    openDialog=open_dialog,
                    btnPrebookState=True,
                    btnPressTime=btn_time or None,
                )
            except Exception as e:
                print(f'[{_ts()}] [ANNOUNCE] 推送 book_button 失败: {e}', flush=True)

    def on_accepted(self) -> None:
        """受理 → 异步播 step3；抬杆 step4 可用打断模式覆盖。"""
        self.stop_idle_loop()
        with self._lock:
            self._release_pending = True
            self._said_session.discard(4)
            self._said_session.discard(5)

        def _run() -> None:
            try:
                from app.services.booking_camera_tts import release_voice_channel

                # 前端刚停对讲时相机常仍 403，先 close+短暂冷却再播
                release_voice_channel(settle_sec=0.45)
            except Exception as e:
                print(f'[{_ts()}] [ANNOUNCE] 受理前释放通道失败: {e}', flush=True)
            if self._announce_session_once(3):
                print(f'[{_ts()}] [ANNOUNCE] 受理 → step3', flush=True)

        threading.Thread(target=_run, daemon=True, name='announce-step3').start()

    def on_gate_open(self) -> None:
        """抬杆放行 → step4（interrupt，可跳过未播完的 step3）。"""
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
        from app.services.booking_camera_tts import abort_keepalive

        with self._lock:
            self._release_pending = False
            self._said_session.clear()
            target = self._radar_target
            idle = self._is_system_idle_unlocked()
        abort_keepalive(timeout=3.0)
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

        def _led() -> None:
            try:
                from app.services.device_action import execute_device_action

                execute_device_action('led_001', f'set_step{step}')
            except Exception as e:
                print(f'[{_ts()}] [ANNOUNCE] LED step{step} 失败: {e}', flush=True)

        threading.Thread(target=_led, daemon=True, name=f'announce-led-{step}').start()

    def _ensure_idle_loop(self) -> None:
        with self._lock:
            if self._idle_looping:
                return
            t = self._idle_thread
            if t is not None and t.is_alive():
                # 预约键只是暂停招揽，长连接还在：恢复循环即可
                self._idle_looping = True
                print(f'[{_ts()}] [ANNOUNCE] 复用招揽长连接', flush=True)
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
        if was:
            print(f'[{_ts()}] [ANNOUNCE] 空闲招揽已暂停（长连接保持）', flush=True)

    def _idle_worker(self) -> None:
        """空闲招揽：一条 TwoWayAudio 长连接循环播 step1，句间写静音。"""
        def should_continue() -> bool:
            with self._lock:
                return (
                    self._idle_looping
                    and self._radar_target
                    and self._is_system_idle_unlocked()
                )

        def on_clip() -> None:
            def _led() -> None:
                try:
                    from app.services.device_action import execute_device_action

                    execute_device_action('led_001', 'set_step1')
                except Exception as e:
                    print(f'[{_ts()}] [ANNOUNCE] LED step1 失败: {e}', flush=True)

            threading.Thread(target=_led, daemon=True, name='announce-led-1').start()

        try:
            print(f'[{_ts()}] [ANNOUNCE] 空闲招揽长连接开始', flush=True)
            from app.services.booking_camera_tts import run_idle_keepalive

            run_idle_keepalive(
                1,
                self.IDLE_INTERVAL_SEC,
                should_continue,
                on_clip=on_clip,
            )
        except Exception as e:
            print(f'[{_ts()}] [ANNOUNCE] 招揽长连接失败: {e}', flush=True)
        finally:
            with self._lock:
                self._idle_looping = False
                if self._idle_thread is threading.current_thread():
                    self._idle_thread = None
            print(f'[{_ts()}] [ANNOUNCE] 空闲招揽线程结束', flush=True)


def get_lane_announce() -> LaneAnnounceService:
    return LaneAnnounceService.instance()
