from __future__ import annotations

import threading
import unittest
from concurrent.futures import Future
from unittest import mock

from wechat_shop_scraper.desktop_keyword import DesktopKeywordListener, DesktopWechatBackend, DesktopWechatError


class DesktopPauseTests(unittest.TestCase):
    def test_final_release_waits_for_inflight_native_dispatch(self):
        backend = DesktopWechatBackend()
        backend.weixin_session = mock.Mock()
        backend.wmpf_session = mock.Mock()
        weixin_script = backend.weixin_script = mock.Mock()
        wmpf_script = backend.wmpf_script = mock.Mock()
        entered = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()
        errors = []
        def dispatch(*_):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test dispatch not released")
        weixin_script.exports_sync.dispatch.side_effect = dispatch
        backend.cdp_bridge.cancel_pending = cancelled.set
        def call():
            try:
                backend.call("XWeb.version", timeout=1)
            except DesktopWechatError as exc:
                errors.append(str(exc))
        caller = threading.Thread(target=call)
        closer = threading.Thread(target=backend.stop)
        caller.start()
        try:
            self.assertTrue(entered.wait(1))
            closer.start()
            self.assertTrue(cancelled.wait(1))
            weixin_script.unload.assert_not_called()
            wmpf_script.unload.assert_not_called()
        finally:
            release.set()
            caller.join(2)
            if closer.ident is not None:
                closer.join(2)
        self.assertFalse(caller.is_alive())
        self.assertFalse(closer.is_alive())
        weixin_script.unload.assert_called_once()
        wmpf_script.unload.assert_called_once()
        self.assertTrue(errors)

    def test_stop_cancels_worker_without_unloading_native_sessions(self):
        backend = DesktopWechatBackend()
        backend.weixin_session = mock.Mock()
        backend.wmpf_session = mock.Mock()
        backend.weixin_script = mock.Mock()
        backend.wmpf_script = mock.Mock()
        backend.cdp_bridge = mock.Mock()
        backend.call_batch = mock.Mock()
        listener = DesktopKeywordListener(keyword="", keywords=["床垫"], on_products=lambda _: None, backend=backend)
        waiter = threading.Event()
        holder = {}
        backend._pending["old-call"] = (waiter, holder)
        listener.stop()
        self.assertTrue(waiter.is_set())
        self.assertIn("error", holder)
        self.assertTrue(backend.active)
        backend.weixin_script.unload.assert_not_called()
        backend.wmpf_script.unload.assert_not_called()
        backend.weixin_session.detach.assert_not_called()
        backend.wmpf_session.detach.assert_not_called()
        backend.call_batch.assert_not_called()
        backend.cdp_bridge.stop.assert_not_called()

    def test_pause_wakes_cdp_calls_without_closing_socket(self):
        backend = DesktopWechatBackend()
        bridge = backend.cdp_bridge
        client = mock.Mock()
        bridge._clients.append(client)
        future = Future()
        bridge._pending[1] = future
        backend.pause()
        self.assertTrue(future.done())
        self.assertIsInstance(future.exception(), RuntimeError)
        client.close.assert_not_called()
        self.assertFalse(bridge._stop.is_set())

    def test_runtime_recovery_reopens_page_without_detaching(self):
        backend = DesktopWechatBackend()
        backend.stop = mock.Mock()
        backend.start = mock.Mock()
        backend.open_search_page = mock.Mock()
        backend.recover_search_runtime()
        backend.stop.assert_not_called()
        backend.open_search_page.assert_called_once_with(force=True, timeout=30.0)

    def test_stopped_xweb_wait_raises_instead_of_returning_empty_success(self):
        backend = DesktopWechatBackend()
        backend.weixin_session = mock.Mock()
        backend.wmpf_session = mock.Mock()
        backend.weixin_script = mock.Mock()
        backend.wmpf_script = mock.Mock()
        backend.weixin_script.exports_sync.dispatch.side_effect = lambda *_: backend.pause()
        with self.assertRaisesRegex(DesktopWechatError, "停止"):
            backend.call("XWeb.version", timeout=0.5)


if __name__ == "__main__":
    unittest.main()
