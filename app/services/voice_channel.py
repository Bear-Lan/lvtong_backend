"""预约机双工语音通道独占锁。

摄像头 TwoWayAudio 同一时刻只能有一个占用方：
- announce：后端 TTS / 招揽播报
- talk：前端海康插件对讲

硬件无法排队，必须在业务层互斥。
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from typing import Any


def _ts() -> str:
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]


class VoiceChannelLock:
    """进程内独占锁（单后端实例假设）。"""

    OWNERS = frozenset({'announce', 'talk'})

    def __init__(self) -> None:
        self._cv = threading.Condition(threading.Lock())
        self._owner: str | None = None
        self._token: str | None = None
        self._since_mono: float | None = None
        self._since_wall: float | None = None

    def status(self) -> dict[str, Any]:
        with self._cv:
            return self._status_unlocked()

    def _status_unlocked(self) -> dict[str, Any]:
        busy = self._owner is not None
        held_ms = 0
        if busy and self._since_mono is not None:
            held_ms = int((time.monotonic() - self._since_mono) * 1000)
        return {
            'busy': busy,
            'owner': self._owner,
            'token': self._token,
            'heldMs': held_ms,
            'since': self._since_wall,
        }

    def acquire(
        self,
        owner: str,
        *,
        wait: bool = False,
        timeout: float = 30.0,
        steal: bool = False,
    ) -> dict[str, Any]:
        owner = (owner or '').strip()
        if owner not in self.OWNERS:
            return {'ok': False, 'error': f'invalid owner: {owner}'}

        token = uuid.uuid4().hex
        deadline = time.monotonic() + max(0.0, float(timeout))
        stolen_from: str | None = None

        with self._cv:
            while True:
                if self._owner is None:
                    self._owner = owner
                    self._token = token
                    self._since_mono = time.monotonic()
                    self._since_wall = time.time()
                    print(
                        f'[{_ts()}] [VOICE-LOCK] acquire ok owner={owner} '
                        f'token={token[:8]}…',
                        flush=True,
                    )
                    out = self._status_unlocked()
                    out['ok'] = True
                    out['token'] = token
                    if stolen_from:
                        out['stolenFrom'] = stolen_from
                    return out

                if self._owner == owner:
                    # 同占用方重入：复用现有 token
                    out = self._status_unlocked()
                    out['ok'] = True
                    out['reentrant'] = True
                    return out

                if steal:
                    stolen_from = self._owner
                    print(
                        f'[{_ts()}] [VOICE-LOCK] steal {stolen_from} → {owner}',
                        flush=True,
                    )
                    self._owner = owner
                    self._token = token
                    self._since_mono = time.monotonic()
                    self._since_wall = time.time()
                    self._cv.notify_all()
                    out = self._status_unlocked()
                    out['ok'] = True
                    out['token'] = token
                    out['stolenFrom'] = stolen_from
                    return out

                if not wait:
                    out = self._status_unlocked()
                    out['ok'] = False
                    out['error'] = f'busy:{self._owner}'
                    return out

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    out = self._status_unlocked()
                    out['ok'] = False
                    out['error'] = 'timeout'
                    return out
                self._cv.wait(timeout=min(remaining, 0.4))

    def release(
        self,
        *,
        owner: str | None = None,
        token: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        owner = (owner or '').strip() or None
        token = (token or '').strip() or None

        with self._cv:
            if self._owner is None:
                out = self._status_unlocked()
                out['ok'] = True
                out['idle'] = True
                return out

            allowed = force
            if not allowed and token and token == self._token:
                allowed = True
            if not allowed and owner and owner == self._owner:
                allowed = True

            if not allowed:
                out = self._status_unlocked()
                out['ok'] = False
                out['error'] = 'not_holder'
                return out

            prev = self._owner
            self._owner = None
            self._token = None
            self._since_mono = None
            self._since_wall = None
            self._cv.notify_all()
            print(
                f'[{_ts()}] [VOICE-LOCK] release ok was={prev}'
                f'{" force" if force else ""}',
                flush=True,
            )
            out = self._status_unlocked()
            out['ok'] = True
            out['released'] = prev
            return out


_lock = VoiceChannelLock()


def get_voice_channel() -> VoiceChannelLock:
    return _lock
