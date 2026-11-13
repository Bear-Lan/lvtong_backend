"""操作台提示循环：单个全局任务，停止仅撤销后续派发，不影响其他语音。"""
from __future__ import annotations

import math
import threading
import time
import uuid

from app.services import command_announce as voice


def _timing() -> tuple[float, float]:
    config = voice.load_config()
    values = (config.get('prompt_clip_sec', 3), config.get('prompt_gap_sec', 2))
    for value, lower, upper in zip(values, (1, 0.5), (120, 30)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not lower <= value <= upper):
            raise ValueError('提示语音时长或间隔配置无效')
    return float(values[0]), float(values[1])


class PromptAnnounceService:
    def __init__(self):
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._version = 0
        self._active = False
        self._phase = 'stopped'
        self._error = ''
        self._instance = uuid.uuid4().hex

    def status(self) -> dict:
        with self._lock:
            return self._status_unlocked()

    def _status_unlocked(self) -> dict:
        return {'active': self._active, 'step': 9 if self._active else None,
                'phase': self._phase, 'error': self._error, 'version': self._version,
                'instance': self._instance, 'options': [{'step': 9, 'text': voice.ANNOUNCE_TEXT[9]}]}

    def start(self) -> tuple[bool, dict]:
        with self._lock:
            if self._active:
                return True, self._status_unlocked()
            self._version += 1
            token = self._version
            self._cancel.set()
            cancel = self._cancel = threading.Event()
            try:
                clip_sec, gap_sec = _timing()
                voice.set_prompt_loop_active(True)
                sent, revision = voice.dispatch_prompt()
                if not sent:
                    raise RuntimeError('提示语音命令发送失败，请重试')
            except Exception as error:
                self._active = False
                self._phase = 'stopped'
                self._error = str(error)
                voice.set_prompt_loop_active(False)
                return False, self._status_unlocked()
            self._active = True
            self._phase = 'playing'
            self._error = ''
            self._thread = threading.Thread(target=self._run,
                args=(token, cancel, revision, time.monotonic(), clip_sec, gap_sec),
                daemon=True, name='prompt-voice9')
            self._thread.start()
            return True, self._status_unlocked()

    def stop(self) -> dict:
        # 与最终发包共用同一把锁；返回之后，旧任务不会再发送。
        with self._lock:
            self._version += 1
            self._active = False
            self._phase = 'stopped'
            self._error = ''
            self._cancel.set()
            voice.set_prompt_loop_active(False)
            return self._status_unlocked()

    def _run(self, token, cancel, revision, sent_at, clip_sec, gap_sec):
        try:
            while not cancel.is_set():
                current_revision, blocked = voice.prompt_playback_snapshot()
                # 状态位没有命令编号且现场会滞留为1；本轮按配置时长兜底。
                # revision变化表示被新业务语音抢占，必须等其保护释放。
                ready = not blocked or (current_revision == revision
                    and time.monotonic() - sent_at >= clip_sec)
                if not ready:
                    cancel.wait(0.1)
                    continue
                with self._lock:
                    if token != self._version or not self._active:
                        return
                    self._phase = 'gap'
                if cancel.wait(gap_sec):
                    return
                with self._lock:
                    if token != self._version or not self._active:
                        return
                    sent, new_revision = voice.dispatch_prompt(revision)
                    if sent is None:
                        self._phase = 'waiting'
                        continue
                    if not sent:
                        raise RuntimeError('提示语音命令发送失败，循环已停止，请重试')
                    revision = new_revision
                    sent_at = time.monotonic()
                    self._phase = 'playing'
        except Exception as error:
            with self._lock:
                if token == self._version:
                    self._active = False
                    self._phase = 'stopped'
                    self._error = str(error)
                    self._version += 1
                    cancel.set()
                    voice.set_prompt_loop_active(False)


_service = PromptAnnounceService()


def get_prompt_announce() -> PromptAnnounceService:
    return _service
