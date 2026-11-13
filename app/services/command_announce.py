"""自动播报通过 MDRMC 组播交给中间件的串口设备；不占用海康对讲。"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.devices.mcast_cmd import send_mdrmc

CONFIG_PATH = Path(__file__).resolve().parents[2] / 'config' / 'voice_announce.json'
ANNOUNCE_TEXT = {
    1: '绿通车辆，按键检测',
    2: '申请已接收，请原地等待',
    3: '申请已受理，请等待放行',
    4: '待检车辆，请通行',
    5: '检测中，请勿跟车',
    6: '请打开车门',
    7: '请勿遮挡车厢',
    8: '拍摄完成，请关好车门',
    9: '前方超限车辆倒车，请注意避让',
}
_TARGET_KEYS = {'front': 'frontvoiceannounce', 'back': 'backvoiceannounce'}
_send_lock = threading.RLock()
_state_lock = threading.Lock()
_generation = 0
_wake_idle = threading.Event()


@dataclass
class _PlaybackState:
    # 状态由 _state_lock 保护；取消空闲线程不能清除正在播放的保护。
    playing: dict[str, bool] = field(default_factory=dict)
    pending: dict[str, tuple[float, bool]] = field(default_factory=dict)
    quiet_until: float = 0.0
    revision: int = 0
    prompt_active: bool = False

    def idle_blocked(self, now: float, *, include_prompt: bool = True) -> bool:
        # 无反馈不能靠超时假定播完；已知仍在播放也不限时避让。
        return (include_prompt and self.prompt_active) or any(self.playing.values()) or bool(self.pending) or now < self.quiet_until


_playback = _PlaybackState()


def update_playback_status(parsed: dict) -> None:
    """DEVICE 前字段 bit16/17；两侧各自完成后，才允许恢复空闲提示。"""
    with _state_lock:
        for side in _TARGET_KEYS:
            value = parsed.get(f'{side}VoiceAnnounceStatus')
            if not isinstance(value, bool):
                continue
            was_playing = _playback.playing.get(side, False)
            _playback.playing[side] = value
            pending = _playback.pending.get(side)
            if pending is not None:
                deadline, started = pending
                if value and not was_playing:
                    _playback.pending[side] = (deadline, True)
                elif not value and (started or time.monotonic() >= deadline):
                    # 漏掉起始沿时，也要保护期后的新空闲反馈；反馈断流不解锁。
                    del _playback.pending[side]


def load_config() -> dict:
    """配置错误时拒绝发送，避免把错误编号播到现场；允许现场修正映射后直接生效。"""
    config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict):
        raise ValueError('语音配置必须为 JSON 对象')
    targets = config.get('targets')
    if not isinstance(targets, list) or any(t not in _TARGET_KEYS for t in targets):
        raise ValueError('targets 必须为 front/back 列表')
    if len(targets) != len(set(targets)):
        raise ValueError('targets 不可重复')
    steps = config.get('steps')
    if not isinstance(steps, dict):
        raise ValueError('steps 必须为业务步骤到 VOICE1…VOICE9 的映射')
    for step, code in steps.items():
        if step not in {str(n) for n in ANNOUNCE_TEXT}:
            raise ValueError('未知播报业务步骤')
        if not isinstance(code, str) or not re.fullmatch(r'VOICE[1-9]', code):
            raise ValueError('播报编号只能为 VOICE1…VOICE9')
    interval = config.get('repeat_interval_sec', 10)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        raise ValueError('repeat_interval_sec 必须为数字')
    if not math.isfinite(interval) or not 1 <= interval <= 3600:
        raise ValueError('repeat_interval_sec 必须在 1…3600 秒之间')
    guard = config.get('idle_resume_guard_sec', 10)
    if (isinstance(guard, bool) or not isinstance(guard, (int, float))
            or not math.isfinite(guard) or not 1 <= guard <= 120):
        raise ValueError('idle_resume_guard_sec 必须在 1…120 秒之间')
    return {**config, 'repeat_interval_sec': float(interval), 'idle_resume_guard_sec': float(guard)}


def speak_step(step: int, *, interrupt: bool = True, blocking: bool = False) -> bool:
    """发送预置语音命令。成功仅表示 UDP 已提交，不代表设备确认或播放结束。

    兼容业务调用的 interrupt/blocking 参数；协议没有停止或播放完成回执，
    不模拟海康播放完成，也不发送未确认的停止指令。
    """
    if not isinstance(step, int) or isinstance(step, bool) or step not in ANNOUNCE_TEXT:
        return False
    if interrupt:
        stop_speak()
    return bool(_dispatch_step(step))


def _dispatch_step(step: int, expected_generation: int | None = None) -> bool | None:
    """None 表示空闲避让，不是发送失败；业务语音不受播放状态限制。"""
    if not isinstance(step, int) or isinstance(step, bool) or step not in ANNOUNCE_TEXT:
        return False
    try:
        config = load_config()
    except (OSError, ValueError, TypeError) as error:
        print(f'[VOICE-CMD] 配置错误，未发送: {error}', flush=True)
        return False
    code = config['steps'].get(str(step))
    targets = config['targets']
    if not code or not targets:
        print(f'[VOICE-CMD] step{step} 编号或播报方向尚未配置，未发送', flush=True)
        return False
    # 与 stop_speak 串行交接；它返回后不再派发取消前的等待任务。
    with _state_lock:
        generation = _generation if expected_generation is None else expected_generation
    with _send_lock:
        with _state_lock:
            if generation != _generation:
                return False
            now = time.monotonic()
            if step == 1 and _playback.idle_blocked(now):
                return None
            old_quiet_until = _playback.quiet_until
        success = True
        any_sent = False
        for target in targets:
            with _state_lock:
                previous = _playback.pending.get(target)
                if step != 1:
                    # 发包前就保护，覆盖命令发送到播放状态反馈之间的窗口。
                    _playback.pending[target] = (now + config.get('idle_resume_guard_sec', 10), False)
                    _playback.quiet_until = max(_playback.quiet_until, now + 1)
            command = f'{_TARGET_KEYS[target]}={code}'
            sent = send_mdrmc(command)
            any_sent = sent or any_sent
            if not sent and step != 1:
                with _state_lock:
                    if previous is None:
                        _playback.pending.pop(target, None)
                    else:
                        _playback.pending[target] = previous
            success = sent and success
            print(f'[VOICE-CMD] step{step} {command} udp_sent={sent}', flush=True)
        if step != 1 and not any_sent:
            with _state_lock:
                _playback.quiet_until = old_quiet_until
        elif step != 1:
            with _state_lock:
                _playback.revision += 1
        return success


def prompt_playback_snapshot() -> tuple[int, bool]:
    with _state_lock:
        return _playback.revision, _playback.idle_blocked(time.monotonic(), include_prompt=False)


def set_prompt_loop_active(active: bool) -> None:
    with _state_lock:
        _playback.prompt_active = active


def dispatch_prompt(previous_revision: int | None = None) -> tuple[bool | None, int]:
    """首次人工提示可抢播；后续重复不能覆盖期间新到的业务语音。"""
    with _send_lock:
        revision, blocked = prompt_playback_snapshot()
        if previous_revision is not None and revision != previous_revision and blocked:
            return None, revision
        result = speak_step(9, interrupt=False)
        return result, prompt_playback_snapshot()[0]


def speak_text(text: str) -> bool:
    for step, expected in ANNOUNCE_TEXT.items():
        if (text or '').strip() == expected:
            return speak_step(step)
    return False


def stop_speak() -> None:
    """取消本机后续播报。已送达设备的语音无法由此停止。"""
    global _generation
    with _send_lock:
        with _state_lock:
            _generation += 1
        _wake_idle.set()


def run_idle_announcements(
    step: int,
    should_continue: Callable[[], bool],
    *,
    on_clip: Callable[[], None] | None = None,
) -> None:
    """空闲提示定时发命令；事件切换唤醒等待，不创建音频连接。"""
    with _state_lock:
        generation = _generation
    while should_continue():
        with _state_lock:
            if generation != _generation:
                return
        _wake_idle.clear()
        started = time.monotonic()
        result = _dispatch_step(step, expected_generation=generation)
        if result is None:
            # 暂停而非取消循环；业务条件仍满足时自动恢复，不补发积压提示。
            _wake_idle.wait(0.2)
            continue
        if not result:
            # 未配置/发送失败时结束本轮，由后续雷达事件重试，避免忙循环。
            return
        if on_clip is not None:
            on_clip()
        try:
            interval = load_config()['repeat_interval_sec']
        except (OSError, ValueError, TypeError):
            return
        while should_continue():
            with _state_lock:
                if generation != _generation:
                    return
            remaining = interval - (time.monotonic() - started)
            if remaining <= 0:
                break
            if _wake_idle.wait(min(remaining, 0.2)):
                return
