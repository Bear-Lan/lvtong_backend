"""现场中间件 HTTP（图像 / 启扫等）

Flask 使用 gevent monkey.patch_all() 后，greenlet 里 requests 偶发卡读超时。
请求放到 gevent.threadpool；线程内异常先包装，避免 ThreadPool 刷整段 Traceback。

中间件（:5003/:5006）并发弱：启扫与拉图并行会整路 Read timeout，故全局串行。
"""
from __future__ import annotations

import threading
import json
from typing import Any
from urllib.parse import urlparse

import requests

_pool = None
_middleware_lock = threading.Lock()


def _thread_pool():
    global _pool
    if _pool is None:
        from gevent.threadpool import ThreadPool

        _pool = ThreadPool(2)
    return _pool


def _is_middleware_host(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or '').lower()
        port = urlparse(url).port
    except Exception:
        return False
    if port in (5002, 5003, 5006):
        return True
    return host in ('192.168.88.201', '127.0.0.1', 'localhost')


def _run_in_pool(fn):
    """在线程池执行；worker 内不 raise，避免 gevent 打印失败堆栈。"""

    def _do():
        try:
            return ('ok', fn())
        except Exception as e:
            return ('err', e)

    kind, payload = _thread_pool().spawn(_do).get()
    if kind == 'ok':
        return payload
    raise payload


def middleware_get(
    url: str,
    *,
    timeout: float | tuple[float, float] = (3.0, 8.0),
    **kwargs: Any,
) -> requests.Response:
    """GET；默认短连接。中间件主机自动串行。"""
    headers = dict(kwargs.pop('headers', None) or {})
    headers.setdefault('Connection', 'close')
    headers.setdefault('Cache-Control', 'no-cache')

    def _call():
        return requests.get(url, timeout=timeout, headers=headers, **kwargs)

    if _is_middleware_host(url):
        with _middleware_lock:
            return _run_in_pool(_call)
    return _run_in_pool(_call)


def middleware_get_payload(
    url: str, *, max_bytes: int = 128 * 1024 * 1024,
    timeout: float | tuple[float, float] = (3.0, 10.0),
) -> requests.Response:
    """限量读取雷达文件；仅容忍现场已确认的完整无数据 JSON 长度错误。

    二进制文件少一字节也拒绝，不能把截断的 LAZ 作为成功数据交给解码器。
    读取和请求在同一线程中完成，避免 gevent 主线程解压/等待大文件。
    """
    def _call():
        with requests.get(url, timeout=timeout, stream=True, headers={
            'Connection': 'close', 'Cache-Control': 'no-cache',
            'Accept-Encoding': 'identity',
        }) as response:
            declared = response.headers.get('Content-Length')
            expected = int(declared) if declared is not None else None
            if expected is not None and (expected < 0 or expected > max_bytes):
                raise ValueError('雷达响应文件长度超出限制')
            # urllib3 默认会在拿到完整错误 JSON 后仍因现场多报14字节而抛异常。
            response.raw.enforce_content_length = False
            chunks, size = [], 0
            while True:
                chunk = response.raw.read(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError('雷达响应文件过大')
            data = b''.join(chunks)
            if expected is not None and len(data) != expected:
                try:
                    obj = json.loads(data)
                except (ValueError, UnicodeError):
                    obj = None
                known_empty_response = (
                    expected - len(data) == 14 and isinstance(obj, dict)
                    and obj.get('status') in (False, 'false')
                )
                if not known_empty_response:
                    raise ValueError(f'雷达文件不完整：应收 {expected} 字节，实收 {len(data)}')
            response._content = data
            response._content_consumed = True
            return response

    if _is_middleware_host(url):
        with _middleware_lock:
            return _run_in_pool(_call)
    return _run_in_pool(_call)


def middleware_request(
    method: str,
    url: str,
    *,
    timeout: float | tuple[float, float] = (3.0, 10.0),
    **kwargs: Any,
) -> requests.Response:
    """通用请求（启扫等）；中间件主机串行。"""
    headers = dict(kwargs.pop('headers', None) or {})
    headers.setdefault('Connection', 'close')
    method_u = (method or 'GET').upper()

    def _call():
        return requests.request(
            method_u, url, timeout=timeout, headers=headers, **kwargs,
        )

    if _is_middleware_host(url):
        with _middleware_lock:
            return _run_in_pool(_call)
    return _run_in_pool(_call)
