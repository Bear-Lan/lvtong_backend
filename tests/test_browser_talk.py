"""独立音频会话测试：鉴权、互斥、断线/失败回收、海康提前响应兼容。"""
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from flask import Flask
from flask_socketio import SocketIO
from app.extensions.auth import create_token
from app.services import browser_talk as talk
from app.services.voice_channel import VoiceChannelLock


class FakeCamera:
    codec = 'alaw'
    def __init__(self):
        self.closed = threading.Event()
        self.sent = []
    def open(self): pass
    def close(self): self.closed.set(); return True
    def send(self, data): self.sent.append(data)
    def receive(self):
        while not self.closed.wait(.01):
            yield b'\xd5' * 320


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.voice = VoiceChannelLock()
        p = patch.object(talk, 'get_voice_channel', return_value=self.voice)
        p.start(); self.addCleanup(p.stop)

    def test_distinct_talk_sessions_cannot_reenter_same_owner(self):
        first = self.voice.acquire('talk', reentrant=False)
        self.assertTrue(first['ok'])
        self.assertFalse(self.voice.acquire('talk', reentrant=False)['ok'])
        self.assertFalse(self.voice.release(owner='talk', token='wrong')['ok'])
        self.assertTrue(self.voice.release(token=first['token'])['ok'])

    def test_bounded_input_and_reject_malformed_frames(self):
        s = talk.TalkSession('a', time.time()+60, lambda *a, **k: None, lambda s: None)
        with self.assertRaises(talk.TalkError): s.feed(b'a'*320)
        s.ready = True
        for _ in range(30): s.feed(b'a'*320)
        self.assertEqual(s.input.qsize(), 5)
        self.assertEqual(s.dropped, 25)
        for value in ('bad', b'a', b'a'*640, []):
            with self.assertRaises(talk.TalkError): s.feed(value)
        s.stop()
        with self.assertRaises(talk.TalkError): s.feed(b'a'*320)

    def test_audio_roundtrip_stop_closes_camera_and_releases_lock(self):
        camera = FakeCamera(); events = []; done = threading.Event()
        def emit(event, data, **kw):
            events.append((event, data))
            if event == 'ready': session.feed(b'\xab'*320)
            if kw.get('callback'): kw['callback']()
        session = talk.TalkSession('a', time.time()+60, emit, lambda s: done.set(), lambda: camera)
        worker = threading.Thread(target=session.run); worker.start()
        time.sleep(.15); session.stop(); worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(done.is_set()); self.assertTrue(camera.closed.is_set())
        self.assertIn(b'\xab'*320, camera.sent)
        self.assertGreater(session.rx_bytes, 0)
        self.assertFalse(self.voice.status()['busy'])
        self.assertTrue(any(e == 'ended' and d['cameraClosed'] for e, d in events))

    def test_failure_during_open_still_releases_reserved_session(self):
        camera = FakeCamera(); camera.open = lambda: (_ for _ in ()).throw(OSError())
        done = []; events = []
        s = talk.TalkSession('a', time.time()+60, lambda *a, **k: events.append(a), done.append, lambda: camera)
        s.run()
        self.assertEqual(done, [s]); self.assertTrue(camera.closed.is_set())
        self.assertFalse(self.voice.status()['busy'])

    def test_unacknowledged_audio_window_stays_bounded(self):
        camera = FakeCamera()
        camera.receive = lambda: iter([b'\xd5'*320] * 100)
        events = []
        s = talk.TalkSession('a', time.time()+60, lambda event, data, **kw: events.append(event), lambda s: None)
        s.camera = camera
        s._receive()
        self.assertEqual(events.count('audio'), 24)
        self.assertEqual(s.pending_down, 24)
        self.assertEqual(s.dropped, 76)

    def test_disconnect_while_opening_does_not_start_audio(self):
        camera = FakeCamera(); s = talk.TalkSession('a', time.time()+60, lambda *a, **k: None, lambda s: None, lambda: camera)
        camera.open = s.stop
        s.run()
        self.assertEqual(camera.sent, []); self.assertTrue(camera.closed.is_set())

    def test_expired_session_releases_camera(self):
        camera = FakeCamera(); s = talk.TalkSession('a', time.time()-1, lambda *a, **k: None, lambda s: None, lambda: camera)
        s.run()
        self.assertTrue(camera.closed.is_set()); self.assertFalse(self.voice.status()['busy'])

    def test_manager_reserves_before_background_start_and_cannot_stop_other_client(self):
        tasks = []
        m = talk.TalkManager(lambda *a, **k: None, tasks.append)
        m.start('a', time.time()+60)
        with self.assertRaises(talk.TalkError): m.start('b', time.time()+60)
        m.stop('b'); self.assertFalse(m.active.stop_event.is_set())
        m.stop('a'); self.assertTrue(m.active.stop_event.is_set())
        tasks[0](); self.assertIsNone(m.active)

    def test_socket_namespace_requires_valid_login(self):
        app = Flask(__name__); app.secret_key = 'test'
        io = SocketIO(app, async_mode='threading')
        manager = talk.register_talk_handlers(io)
        for auth in (None, {}, {'token': 'invalid'}):
            c = io.test_client(app, namespace='/talk', auth=auth)
            self.assertFalse(c.is_connected('/talk'))
        token = create_token({'username':'test'})
        a = io.test_client(app, namespace='/talk', auth={'token':token})
        b = io.test_client(app, namespace='/talk', auth={'token':token})
        with patch.object(manager, 'spawn'):
            self.assertTrue(a.emit('start', namespace='/talk', callback=True)['ok'])
            self.assertFalse(b.emit('start', namespace='/talk', callback=True)['ok'])
            self.assertFalse(b.emit('audio', b'\xd5'*320, namespace='/talk', callback=True)['ok'])
            a.disconnect(namespace='/talk')
            self.assertTrue(manager.active.stop_event.is_set())
        b.disconnect(namespace='/talk')


class EarlyCameraResponseTest(unittest.TestCase):
    def test_early_200_without_length_does_not_close_upload_socket(self):
        received = bytearray()
        class CameraHandler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *a): pass
            def reply(self, body):
                self.send_response(200); self.send_header('Content-Type','application/xml')
                self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_GET(self):
                if not self.headers.get('Authorization'):
                    self.send_response(401)
                    self.send_header('WWW-Authenticate','Digest realm="test",nonce="nonce",qop="auth"')
                    self.send_header('Content-Length','0'); self.end_headers(); return
                self.reply(b'<Channel><audioCompressionType>G.711alaw</audioCompressionType></Channel>')
            def do_PUT(self):
                if '/audioData' in self.path:
                    self.wfile.write(b'HTTP/1.1 200 OK\r\n\r\n'); self.wfile.flush()
                    while len(received) < 960:
                        data = self.rfile.read(320)
                        if not data: break
                        received.extend(data)
                    self.close_connection = True
                elif self.path.endswith('/open'):
                    self.reply(b'<Session><sessionId>test</sessionId></Session>')
                else: self.reply(b'<Response><statusCode>1</statusCode></Response>')
        server = ThreadingHTTPServer(('127.0.0.1',0),CameraHandler)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever,daemon=True); worker.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        row = {'ip_address':'127.0.0.1','port':server.server_port,'username':'u','password':'p'}
        with patch.object(talk.DBDevice,'getDevice',return_value=row):
            camera = talk.CameraAudio()
            try:
                camera.open(); camera.send(b'\xaa'*320); camera.send(b'\xbb'*320)
                until = time.monotonic()+1
                while len(received)<960 and time.monotonic()<until: time.sleep(.01)
                self.assertEqual(bytes(received), b'\xd5'*320+b'\xaa'*320+b'\xbb'*320)
            finally: self.assertTrue(camera.close())


if __name__ == '__main__': unittest.main()
