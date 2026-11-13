"""正常结束不发停止；超时、异常和显式停止仍发送，禁止网络。"""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from requests import Response
from app.services import body_camera_fetch as body, progressive_image_fetch as xray, geometry_capture as geom


def response(raw=b'', finish=None):
    r=Response();r.status_code=200;r._content=raw
    if finish:r.headers['Capture-Finish']=finish
    return r


class CapturePollCompletionTest(unittest.TestCase):
    def setUp(self):
        for item in [patch('socket.socket',side_effect=AssertionError('network forbidden')),
                     patch.object(body,'ensure_capture_allowed',return_value=1)]:
            item.start();self.addCleanup(item.stop)

    def run_poll(self, kind, response_value=None, *, timeout=False, error=False):
        if kind=='body':
            fetcher=object.__new__(body.BodyCameraFetcher);fetcher._stop=threading.Event();fetcher._frame_count=0
            module=body;getter='app.services.middleware_http.middleware_get';sender='app.services.body_camera_fetch._send_camcap'
            push=patch.object(fetcher,'_push_frame');run=fetcher._poll_images
        else:
            fetcher=object.__new__(xray.ProgressivePanelFetcher);fetcher._stop_flag=threading.Event();fetcher._frame_count=0
            module=xray;getter='app.services.progressive_image_fetch.middleware_get';sender='app.devices.xray.send_xray_capture'
            push=patch.object(fetcher,'_push_x200_frame');run=lambda:fetcher._poll_x200('http://test/xray/image')
        clock=SimpleNamespace(monotonic=Mock(side_effect=[0,181] if timeout else [0,0]))
        with patch.object(module,'time',clock),patch(getter,return_value=response_value) as get,patch(sender,return_value=True) as send,push as deliver:
            if error:
                with patch.object(body,'_is_capture_finished',side_effect=RuntimeError('unexpected exit')):
                    with self.assertRaises(RuntimeError):run()
            else:run()
            if timeout or error:send.assert_called_once_with(False)
            else:
                send.assert_not_called();get.assert_called_once()
                if response_value.content.startswith(b'\xff\xd8'):deliver.assert_called_once()
            return fetcher

    def test_header_and_json_normal_completion_do_not_send_false(self):
        for kind in ('body','xray'):
            for r in [response(b'\xff\xd8'+b'x'*40,'true'),response(b'{"status":false,"message":"no image"}'),response(b'[]')]:
                with self.subTest(kind=kind,raw=r.content):self.run_poll(kind,r)

    def test_timeout_keeps_stop_for_body_and_xray(self):
        for kind in ('body','xray'):
            with self.subTest(kind=kind):self.run_poll(kind,timeout=True)

    def test_unexpected_exit_keeps_stop_for_body_and_xray(self):
        for kind in ('body','xray'):
            with self.subTest(kind=kind):self.run_poll(kind,response(),error=True)

    def test_explicit_body_and_xray_stop_still_send_false(self):
        b=object.__new__(body.BodyCameraFetcher);b._guard=threading.Lock();b._stop=threading.Event()
        x=object.__new__(xray.ProgressivePanelFetcher);x._guard=threading.Lock();x._stop_flag=threading.Event()
        with patch.object(body,'_send_camcap',return_value=True) as cam,patch.object(xray,'ProgressivePanelFetcher',return_value=x),patch('app.devices.xray.send_xray_capture',return_value=True) as xr:
            b.stop();xray.stop_progressive_panel_fetch()
            cam.assert_called_once_with(False);xr.assert_called_once_with(False)
            self.assertTrue(b._stop.is_set());self.assertTrue(x._stop_flag.is_set())

    def test_geometry_unexpected_exit_still_sends_false(self):
        with patch.object(geom,'_generation',5),patch.object(geom,'_started',True),patch.object(geom,'_poll_laz_frames',side_effect=RuntimeError('unexpected exit')),patch.object(geom,'send_geomcap',return_value=True) as send:
            with self.assertRaises(RuntimeError):geom._poll_laz(5,threading.Event())
            send.assert_called_once_with(False)


if __name__=='__main__':unittest.main()
