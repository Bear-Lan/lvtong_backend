"""自动采集遵守车道占用，手动采集只检查当前检测会话。"""


class CaptureBlockedError(RuntimeError):
    pass


def ensure_capture_allowed(generation: int | None = None) -> int:
    from app.services.scheduler import DistanceScheduler

    scheduler = DistanceScheduler()
    if scheduler.capture_blocked:
        raise CaptureBlockedError('车道占用，已暂停拍照')
    current = scheduler.capture_generation
    if generation is not None and generation != current:
        raise CaptureBlockedError('采集期间车道状态已变化，已丢弃本次照片，请重新拍照')
    return current


def ensure_manual_capture_allowed(session_id=None):
    from app.services.inspection_session import get_store
    store = get_store()
    if store is None:
        return session_id
    snapshot = store.snapshot()
    if (session_id and snapshot['sessionId'] != session_id) or snapshot['phase'] == 'submitted':
        raise CaptureBlockedError('检测已结束或切换，请重新拍照')
    return snapshot['sessionId']
