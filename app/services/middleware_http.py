"""现场中间件 HTTP（图像 / 启扫等）

Flask 使用 gevent monkey.patch_all() 后，greenlet 里 requests 偶发卡读超时。
请求放到 gevent.threadpool；线程内异常先包装，避免 ThreadPool 刷整段 Traceback。

中间件（:5003/:5006）并发弱：启扫与拉图并行会整路 Read timeout，故全局串行。
"""
from __future__ import annotations

import threading
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
