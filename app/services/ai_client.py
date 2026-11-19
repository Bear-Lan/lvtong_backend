"""151 AI业务客户端。凭据仅从环境或本机忽略文件读取，不进入业务参数表。"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


class AIError(RuntimeError):
    pass


def settings():
    path = Path(os.environ.get('AI_CONFIG_FILE', str(ROOT / 'config/ai.local.json')))
    try:
        local = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    except (ValueError, OSError):
        raise AIError('AI本机连接配置无法读取') from None
    result = {key: os.environ.get(env, local.get(key, default)) for key, env, default in (
        ('base_url', 'AI_BASE_URL', 'https://192.168.188.151:8891'),
        ('username', 'AI_USERNAME', ''), ('password', 'AI_PASSWORD', ''),
        ('ca_bundle', 'AI_CA_BUNDLE', str(ROOT / 'certs/ai-server-public.pem')),
        ('proxy', 'AI_PROXY', ''),
    )}
    parsed = urlsplit(result['base_url'])
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise AIError('AI服务地址必须是无内嵌口令的HTTPS地址')
    if not result['username'] or not result['password']:
        raise AIError('尚未配置AI业务凭据，请配置config/ai.local.json或AI_USERNAME/AI_PASSWORD')
    if not Path(result['ca_bundle']).is_file():
        raise AIError('AI受信证书文件不存在')
    return result


def request_json(path, *, files=None, data=None, json_body=None, read_timeout=60):
    cfg = settings()
    # 使用文件内容而非临时URL/文件名去重，PC/Pad同时请求也只推理一次。
    if files:
        digest = hashlib.sha256(json.dumps([cfg, path, data, json_body], sort_keys=True).encode())
        for name, entry in sorted(files.items()):
            raw = entry[1]
            if hasattr(raw, 'getvalue'): raw = raw.getvalue()
            if not isinstance(raw, bytes): break
            digest.update(name.encode()); digest.update(hashlib.sha256(raw).digest())
        else:
            from app.services.ai_request_cache import cached_inference
            return cached_inference(digest.hexdigest(), lambda: _request_json(cfg, path, files=files, data=data, json_body=json_body, read_timeout=read_timeout))
    return _request_json(cfg, path, files=files, data=data, json_body=json_body, read_timeout=read_timeout)


def _request_json(cfg, path, *, files=None, data=None, json_body=None, read_timeout=60):
    rid = uuid.uuid4().hex
    started = time.monotonic()
    try:
        with requests.Session() as session:
            session.trust_env = False
            if cfg['proxy']:
                session.proxies = {'https': cfg['proxy']}
            response = session.request(
                'POST' if files is not None or json_body is not None else 'GET',
                cfg['base_url'].rstrip('/') + path,
                files=files, data=data, json=json_body,
                auth=(cfg['username'], cfg['password']), verify=cfg['ca_bundle'],
                headers={'X-Request-ID': rid}, timeout=(15, read_timeout), allow_redirects=False,
            )
        rid = response.headers.get('X-Request-ID') or rid
        if response.status_code != 200:
            raise AIError(f'AI返回HTTP {response.status_code}，请求号{rid}')
        try:
            payload = response.json()
        except ValueError:
            raise AIError(f'AI返回非JSON，请求号{rid}') from None
        if not isinstance(payload, dict):
            raise AIError(f'AI响应格式异常，请求号{rid}')
        # status接口不采用业务推理code约定。
        if not path.startswith('/status') and (type(payload.get('code')) is not int or payload['code'] != 1):
            raise AIError(f'AI未识别成功，请求号{rid}')
        return payload
    except requests.exceptions.SSLError:
        raise AIError(f'AI证书校验失败，请求号{rid}') from None
    except requests.Timeout:
        raise AIError(f'AI请求超时，请求号{rid}') from None
    except requests.RequestException:
        raise AIError(f'AI网络连接失败，请求号{rid}') from None
    finally:
        elapsed = time.monotonic() - started
        (log.warning if elapsed > 2 else log.info)(
            'AI path=%s request_id=%s elapsed=%.3fs over_2s=%s', path, rid, elapsed, elapsed > 2)


def detect(image_bytes, filename, task, **fields):
    return request_json('/detect', files={'image': (filename, image_bytes, 'application/octet-stream')},
                        data={'task': task, 'return_image': '0', 'return_mask': '0', **fields})
