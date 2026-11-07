"""几何采集随本轮放行启动；取消/重置停止，正常结束由中间件光幕处理。"""
import threading

from app.devices.mcast_cmd import send_geomcap

_lock = threading.Lock()
_generation = 0
_armed = False


def prepare_geometry_capture() -> int:
    global _generation, _armed
    with _lock:
        _generation += 1
        _armed = True
        return _generation


def start_geometry_capture(generation: int | None) -> bool:
    with _lock:
        # 拒绝取消后的延迟启动，以及上一轮遗留的放行任务。
        if not _armed or generation != _generation:
            return False
        return send_geomcap(True)


def stop_geometry_capture() -> bool:
    global _generation, _armed
    with _lock:
        _armed = False
        _generation += 1
        return send_geomcap(False)
