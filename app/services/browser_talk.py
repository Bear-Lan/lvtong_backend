"""浏览器实时音频与预约海康 TwoWayAudio 的桥接；不涉及业务/射线控制。"""
from __future__ import annotations

import http.client
import queue
import socket
import threading
import time
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth

from app.db.device import DBDevice
from app.services.voice_channel import get_voice_channel

FRAME_BYTES = 320  # G.711 8kHz mono，40ms；传输与相机写入均有界
MAX_SECONDS = 600


class TalkError(RuntimeError):
    pass


def xml_value(data, name, default=''):
    root = ET.fromstring(data)
    return next((e.text or default for e in root.iter()
                 if e.tag.rsplit('}', 1)[-1] == name), default)


def check_response(response):
    if response.status_code != 200:
        raise TalkError(f'预约相机音频接口返回 HTTP {response.status_code}')
    if 'xml' in response.headers.get('Content-Type', '') or response.content.startswith(b'<?xml'):
        code = xml_value(response.content, 'statusCode', '1')
        if code != '1':
            raise TalkError('预约相机拒绝音频操作：' + xml_value(response.content, 'subStatusCode', code))


class CameraAudio:
    """凭据只在服务端使用；仅支持预约相机、G.711、通道1。"""
    def __init__(self):
        row = DBDevice().getDevice('cam_booking')
        if not row or not row.get('ip_address'):
            raise TalkError('预约相机未配置')
        self.host = row['ip_address']
        self.port = int(row.get('port') or 80)
        self.base = f'http://{self.host}:{self.port}'
        self.path = '/ISAPI/System/TwoWayAudio/channels/1'
        self.auth = HTTPDigestAuth(row.get('username') or 'admin', row.get('password') or '')
        self.control = requests.Session()
        self.control.trust_env = False
        self.control.auth = self.auth
        self.opened = False
        self.uplink = None
        self.downlink = None
        self.up_response = None
        self.uri = ''
        self.codec = ''

    def open(self):
        config = self.control.get(self.base + self.path, timeout=(3, 4))
        check_response(config)
        codec = xml_value(config.content, 'audioCompressionType').lower()
        if codec not in ('g.711alaw', 'g.711ulaw'):
            raise TalkError('预约相机对讲编码须为 G.711 A-law 或 μ-law')
        self.codec = 'alaw' if codec == 'g.711alaw' else 'ulaw'
        response = self.control.put(self.base + self.path + '/open', timeout=(3, 4))
        check_response(response)
        self.opened = True
        session_id = xml_value(response.content, 'sessionId')
        self.uri = self.path + '/audioData'
        if session_id:
            self.uri += '?' + urlencode({'sessionId': session_id})
        # Digest 状态为线程局部：在 open 所在线程构造发送鉴权头。
        header = self.auth.build_digest_header('PUT', self.base + self.uri)
        self.uplink = http.client.HTTPConnection(self.host, self.port, timeout=4)
        self.uplink.putrequest('PUT', self.uri)
        self.uplink.putheader('Authorization', header)
        self.uplink.putheader('Content-Type', 'application/octet-stream')
        self.uplink.putheader('Content-Length', str(8000 * (MAX_SECONDS + 10)))
        self.uplink.endheaders()
        self.uplink.send(bytes([0xd5 if self.codec == 'alaw' else 0xff]) * FRAME_BYTES)
        # 海康提前返回无长度的 HTTP 200，随后继续收上传体。getresponse() 会
        # 将这种响应视为 will_close 并关闭上传连接，故只解析响应头，保留原套接字。
        self.up_response = http.client.HTTPResponse(self.uplink.sock)
        self.up_response.begin()
        if self.up_response.status != 200:
            raise TalkError(f'相机音频发送失败 HTTP {self.up_response.status}')

    def receive(self):
        # 新线程使用独立 Digest；GET 与 PUT 必须保持到会话结束。
        auth = HTTPDigestAuth(self.auth.username, self.auth.password)
        session = requests.Session()
        session.trust_env = False
        try:
            self.downlink = session.get(self.base + self.uri, auth=auth,
                                        timeout=(3, 5), stream=True)
            if self.downlink.status_code != 200:
                raise TalkError(f'相机声音接收失败 HTTP {self.downlink.status_code}')
            if not self.downlink.headers.get('Content-Type', '').startswith(('audio/', 'application/octet-stream')):
                raise TalkError('相机未返回音频流')
            first = True
            for data in self.downlink.iter_content(FRAME_BYTES):
                if first and data.startswith(b'<?xml'):
                    raise TalkError('相机返回音频会话错误，请重试')
                first = False
                yield data
        finally:
            if self.downlink is not None:
                self.downlink.close()
            session.close()

    def send(self, data):
        self.uplink.send(data)

    def close(self):
        # 先 close 设备使阻塞中的 GET 退出，再关闭本机流套接字。
        closed = True
        if self.opened:
            try:
                r = self.control.put(self.base + self.path + '/close', timeout=(2, 3))
                check_response(r)
            except Exception:
                closed = False
            self.opened = False
        if self.uplink is not None:
            try:
                if self.uplink.sock:
                    self.uplink.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.uplink.close()
        if self.up_response is not None:
            self.up_response.close()
        self.control.close()
        return closed


class TalkSession:
    def __init__(self, sid, expires, emit, finished, camera_factory=None):
        self.sid, self.expires = sid, expires
        self.emit, self.finished = emit, finished
        self.camera_factory = camera_factory or CameraAudio
        self.stop_event = threading.Event()
        self.input = queue.Queue(maxsize=5)
        self.lock = threading.RLock()
        self.last_seen = time.monotonic()
        self.tx_bytes = self.rx_bytes = self.dropped = 0
        self.microphone_bytes = 0
        self.ready = False
        self.pending_down = 0
        self.reason = ''
        self.camera = None
        self.reader = None

    def feed(self, data):
        if not isinstance(data, bytes) or len(data) != FRAME_BYTES:
            raise TalkError('音频帧格式错误')
        with self.lock:
            if not self.ready or self.stop_event.is_set():
                raise TalkError('对讲通道未就绪')
            self.last_seen = time.monotonic()
            self.microphone_bytes += len(data)
            if self.input.full():
                self.input.get_nowait()
                self.dropped += 1
            self.input.put_nowait(data)

    def stop(self, reason=''):
        with self.lock:
            if reason and not self.reason:
                self.reason = reason
            self.stop_event.set()

    def _receive(self):
        try:
            for data in self.camera.receive():
                if self.stop_event.is_set():
                    break
                if not data:
                    continue
                self.rx_bytes += len(data)
                if not self.ready:
                    self.ready = True
                    self.emit('ready', {'codec': self.camera.codec, 'sampleRate': 8000,
                                        'frameBytes': FRAME_BYTES, 'deviceId': 'cam_booking'})
                # 窗口覆盖中继 RTT，最多24帧(960ms)，避免逐帧等ACK造成断续。
                with self.lock:
                    if self.pending_down >= 24:
                        self.dropped += 1
                        continue
                    self.pending_down += 1
                def ack(*_):
                    with self.lock:
                        self.pending_down = max(0, self.pending_down - 1)
                self.emit('audio', data, callback=ack)
            if not self.stop_event.is_set():
                self.stop('相机音频流已结束')
        except Exception as exc:
            if not self.stop_event.is_set():
                self.stop(str(exc) if isinstance(exc, TalkError) else '相机音频接收中断')

    def run(self):
        token = None
        started = time.monotonic()
        close_ok = True
        try:
            result = get_voice_channel().acquire('talk', reentrant=False)
            if not result.get('ok'):
                raise TalkError('预约相机对讲正在被占用')
            token = result['token']
            if self.stop_event.is_set():
                return
            self.camera = self.camera_factory()
            self.camera.open()
            if self.stop_event.is_set():
                return
            self.reader = threading.Thread(target=self._receive, daemon=True, name='talk-receive')
            self.reader.start()
            silence = bytes([0xd5 if self.camera.codec == 'alaw' else 0xff]) * FRAME_BYTES
            next_frame = time.monotonic()
            stats_at = next_frame
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now - self.last_seen > 8:
                    raise TalkError('对讲连接超时，请重新连接')
                if now - started > MAX_SECONDS or time.time() >= self.expires:
                    raise TalkError('本次对讲已结束，请重新连接')
                try:
                    data = self.input.get_nowait()
                except queue.Empty:
                    data = silence
                self.camera.send(data)
                self.tx_bytes += len(data)
                if now - stats_at >= 1:
                    self.emit('stats', {'sentBytes': self.tx_bytes, 'receivedBytes': self.rx_bytes,
                                        'microphoneBytes': self.microphone_bytes,
                                        'droppedFrames': self.dropped})
                    stats_at = now
                next_frame = max(next_frame + .04, time.monotonic())
                self.stop_event.wait(max(0, next_frame - time.monotonic()))
        except Exception as exc:
            self.stop(str(exc) if isinstance(exc, TalkError) else '预约相机对讲连接失败，请重试')
        finally:
            self.stop_event.set()
            try:
                if self.camera is not None:
                    close_ok = self.camera.close()
                if self.reader:
                    self.reader.join(timeout=6)
            except Exception:
                close_ok = False
            finally:
                if token:
                    get_voice_channel().release(token=token)
                self.finished(self)
            self.emit('ended', {'message': self.reason or ('对讲已结束' if close_ok else '连接已结束，相机通道关闭未确认'),
                                'sentBytes': self.tx_bytes, 'receivedBytes': self.rx_bytes,
                                'microphoneBytes': self.microphone_bytes,
                                'cameraClosed': close_ok})


class TalkManager:
    def __init__(self, emit, spawn):
        self.emit, self.spawn = emit, spawn
        self.lock = threading.RLock()
        self.active = None

    def start(self, sid, expires):
        with self.lock:
            if self.active:
                raise TalkError('预约相机正在对讲或释放通道，请稍后重试')
            session = TalkSession(sid, expires,
                lambda event, data, **kw: self.emit(event, data, to=sid, namespace='/talk', **kw),
                self._finished)
            self.active = session
            self.spawn(session.run)

    def _finished(self, session):
        with self.lock:
            if self.active is session:
                self.active = None

    def owned(self, sid):
        with self.lock:
            return self.active if self.active and self.active.sid == sid else None

    def stop(self, sid):
        session = self.owned(sid)
        if session:
            session.stop()


def register_talk_handlers(socketio):
    from flask import request
    from app.extensions.auth import decode_token
    clients = {}
    manager = TalkManager(socketio.emit, socketio.start_background_task)

    @socketio.on('connect', namespace='/talk')
    def connect(auth=None):
        token = auth.get('token') if isinstance(auth, dict) else None
        claims = decode_token(token) if isinstance(token, str) else None
        if not claims or not claims.get('exp'):
            return False
        clients[request.sid] = claims

    @socketio.on('start', namespace='/talk')
    def start():
        claims = clients.get(request.sid)
        if not claims or claims['exp'] <= time.time():
            return {'ok': False, 'error': '登录已过期，请重新登录'}
        try:
            manager.start(request.sid, claims['exp'])
            return {'ok': True}
        except TalkError as exc:
            return {'ok': False, 'error': str(exc)}

    @socketio.on('audio', namespace='/talk')
    def audio(data):
        session = manager.owned(request.sid)
        if not session:
            return {'ok': False, 'error': '对讲已结束'}
        try:
            session.feed(data)
            return {'ok': True}
        except TalkError as exc:
            session.stop(str(exc))
            return {'ok': False, 'error': str(exc)}

    @socketio.on('heartbeat', namespace='/talk')
    def heartbeat():
        session = manager.owned(request.sid)
        if session:
            session.last_seen = time.monotonic()

    @socketio.on('stop', namespace='/talk')
    def stop():
        manager.stop(request.sid)
        return {'ok': True}

    @socketio.on('disconnect', namespace='/talk')
    def disconnect(reason=None):
        clients.pop(request.sid, None)
        manager.stop(request.sid)

    return manager
