"""真实Socket.IO认证房间：匿名连接不能收到草稿，重新认证后持续收到。"""
from contextlib import ExitStack
import unittest
from unittest.mock import patch
from flask import Flask
from flask_socketio import SocketIO
from app.extensions.auth import create_token
from app.websocket import handler
from app.services.inspection_session import broadcast


class SessionSocketAuthTests(unittest.TestCase):
    def test_anonymous_connection_then_authenticated_connection_receives_live_sessions(self):
        with ExitStack() as stack:
            stack.enter_context(patch('socket.socket', side_effect=AssertionError('禁止真实网络')))
            flask_app=Flask(__name__)
            server=SocketIO(flask_app,async_mode='threading')
            stack.enter_context(patch.object(handler,'socketio',server))
            stack.enter_context(patch.object(handler,'_ws_clients',0))
            handler.register_handlers()
            anonymous=server.test_client(flask_app)
            initial=anonymous.get_received()
            ack=next(e['args']['data'] for e in initial if e['name']=='message' and e['args']['type']=='connect_ack')
            self.assertFalse(ack['sessionSyncAuthenticated'])
            broadcast({'sessionId':'test','revision':1,'phase':'detecting','draft':{'form':{'phone':'13800000000'}}})
            self.assertEqual(anonymous.get_received(),[])
            anonymous.disconnect()
            authenticated=server.test_client(flask_app,auth={'token':create_token({'username':'operator'})})
            initial=authenticated.get_received()
            ack=next(e['args']['data'] for e in initial if e['name']=='message' and e['args']['type']=='connect_ack')
            self.assertTrue(ack['sessionSyncAuthenticated'])
            for revision in (2,3):
                broadcast({'sessionId':'test','revision':revision,'phase':'detecting','draft':{}})
                messages=authenticated.get_received()
                self.assertTrue(any(e['name']=='message' and e['args']['type']=='inspection_session' and e['args']['data']['revision']==revision for e in messages))
            authenticated.disconnect()


if __name__=='__main__':unittest.main()
