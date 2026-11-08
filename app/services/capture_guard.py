"""所有相机采集共用车道占用检查；阻止迟到图覆盖当前预览。"""


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
