"""仅合并相同输入的AI推理；不参与业务主流程，不缓存失败。"""
import copy
import json
import threading
import time
from collections import OrderedDict

_lock = threading.RLock()
_pending = {}
_cache = OrderedDict()
_bytes = 0
TTL = 120
MAX_BYTES = 32 * 1024 * 1024


def cached_inference(key, work):
    global _bytes
    now = time.monotonic()
    with _lock:
        for old_key, (expires, size, _) in list(_cache.items()):
            if expires < now:
                _cache.pop(old_key); _bytes -= size
        if key in _cache:
            _cache.move_to_end(key)
            return copy.deepcopy(_cache[key][2])
        leader = key not in _pending
        state = _pending.setdefault(key, {'event': threading.Event()})
    if not leader:
        if not state['event'].wait(150):
            raise TimeoutError('等待相同AI请求超时')
        if 'error' in state:
            raise state['error']
        return copy.deepcopy(state['result'])
    try:
        result = work()
        size = len(json.dumps(result, ensure_ascii=False).encode())
        with _lock:
            state['result'] = result
            if size <= MAX_BYTES:
                while _cache and (_bytes + size > MAX_BYTES or len(_cache) >= 32):
                    _, (_, old_size, _) = _cache.popitem(last=False); _bytes -= old_size
                _cache[key] = (time.monotonic() + TTL, size, copy.deepcopy(result)); _bytes += size
        return copy.deepcopy(result)
    except Exception as error:
        state['error'] = error
        raise
    finally:
        with _lock:
            _pending.pop(key, None)
            state['event'].set()
