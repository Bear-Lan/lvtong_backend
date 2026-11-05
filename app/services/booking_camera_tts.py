"""旧导入路径兼容层：自动播报已改为 MDRMC，不再访问海康音频接口。"""
from app.services.command_announce import ANNOUNCE_TEXT, speak_step, speak_text, stop_speak


def abort_keepalive(*, timeout: float = 3.0) -> None:
    stop_speak()


def release_voice_channel(*, settle_sec: float = 0.0) -> None:
    # 命令播报不占用 TwoWayAudio；不能关闭或释放正在进行的人工对讲。
    pass


def run_idle_keepalive(step, gap_sec, should_continue, *, on_clip=None):
    from app.services.command_announce import run_idle_announcements
    run_idle_announcements(step, should_continue, on_clip=on_clip)
