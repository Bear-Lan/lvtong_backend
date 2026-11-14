"""海康同一视频通道的主/子码流选择，预览与拍照互不影响。"""
import json


def rtsp_channel(row: dict, stream_type: int, fallback=None) -> int:
    cfg = row.get('config') or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except (TypeError, ValueError):
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    # 兼容旧的完整通道号（101/102）及短写法（1/2），只保留视频通道。
    raw = cfg.get('rtsp_channel') or cfg.get('rtspChannel') or fallback
    try:
        full = int(raw)
        if full > 0:
            return max(1, full // 100) * 100 + stream_type
    except (TypeError, ValueError):
        pass
    try:
        channel = max(1, int(cfg.get('channelId', cfg.get('channel_id', 1))))
    except (TypeError, ValueError):
        channel = 1
    return channel * 100 + stream_type
