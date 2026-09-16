from __future__ import annotations

import threading
import unittest
from concurrent.futures import TimeoutError as FutureTimeoutError
from unittest import mock

from wechat_shop_scraper.desktop_keyword import (
    DesktopKeywordListener, DesktopWechatBackend, DesktopWechatError,
    _is_transient_desktop_error,
)
from wechat_shop_scraper.desktop_proto import WmpfCdpBridge


class DesktopResumeFailureTests(unittest.TestCase):
    def backend_with_expired_page(self):
        backend = DesktopWechatBackend()
        backend.call = mock.Mock(side_effect=DesktopWechatError("后台脚本超时"))
        backend.cdp_bridge.wait_runtime = mock.Mock(return_value=True)
        backend.cdp_bridge.call = mock.Mock(side_effect=FutureTimeoutError())
        return backend

    def test_empty_future_timeout_survives_xweb_fallback_and_is_recoverable(self):
        backend = self.backend_with_expired_page()
        with self.assertRaises(DesktopWechatError) as failure:
            backend.evaluate("readSearchState()", timeout=0.1)
        message = str(failure.exception)
        self.assertIn("超时", message)
        self.assertTrue(_is_transient_desktop_error(failure.exception))

    def test_real_evaluation_timeout_rebuilds_before_retrying_current_keyword(self):
        backend = self.backend_with_expired_page()
        recovered = False

        def recover():
            nonlocal recovered
            recovered = True

        def collect(*_):
            if not recovered:
                backend.evaluate("readSearchState()", timeout=0.1)
            return "已完成"

        backend.collect_keyword = collect
        backend.recover_search_runtime = mock.Mock(side_effect=recover)
        finished = mock.Mock()
        errors = []
        listener = DesktopKeywordListener(
            keyword="", keywords=["小清新保暖四件套"], backend=backend,
            on_products=lambda _: None, on_keyword_finished=finished,
            on_error=errors.append, retry_seconds=0,
        )
        listener._automation_loop()
        self.assertFalse(errors, errors)
        backend.recover_search_runtime.assert_called_once()
        finished.assert_called_once_with("小清新保暖四件套", 1, 1, "已完成")

    def test_restart_interval_counts_completed_keywords_and_never_truncates_word(self):
        backend = mock.Mock()
        order = []
        backend.collect_keyword.side_effect = lambda word, *_: order.append(("all-pages", word)) or "已完成"
        backend.restart_search_runtime.side_effect = lambda: order.append(("restart", None))
        listener = DesktopKeywordListener(
            keyword="", keywords=["甲", "乙", "丙", "丁", "戊"], backend=backend,
            on_products=lambda _: None, restart_keyword_interval=2,
        )
        listener._automation_loop()
        self.assertEqual(order, [
            ("all-pages", "甲"), ("all-pages", "乙"), ("restart", None),
            ("all-pages", "丙"), ("all-pages", "丁"), ("restart", None),
            ("all-pages", "戊"),
        ])

    def test_cdp_future_timeout_has_method_and_remains_timeout_type(self):
        bridge = WmpfCdpBridge()
        bridge._send_raw = mock.Mock()
        with self.assertRaises(TimeoutError) as failure:
            bridge.call("Runtime.evaluate", timeout=0.001, jscontext_id="search-page")
        self.assertIn("超时", str(failure.exception))
        self.assertIn("Runtime.evaluate", str(failure.exception))
        self.assertIn("search-page", str(failure.exception))
        self.assertFalse(bridge._pending)

    def test_relaunch_invalidates_page_without_breaking_socket_handover(self):
        bridge = WmpfCdpBridge()
        client = mock.Mock()
        bridge._clients = [client]
        bridge._contexts = ["old"]
        bridge._connection_generation = 3
        bridge._setup_generation = 3
        bridge._runtime_ready.set()
        bridge._setup_complete.set()
        self.assertTrue(bridge.runtime_ready)
        bridge.prepare_relaunch()
        self.assertFalse(bridge.runtime_ready)
        self.assertFalse(bridge._contexts)
        self.assertFalse(bridge._stop.is_set())
        self.assertEqual(bridge.connection_generation, 3)
        client.close.assert_not_called()
        self.assertEqual(bridge._clients, [client])

    def test_stop_interrupts_fresh_connection_wait(self):
        bridge = WmpfCdpBridge()
        stopped = threading.Event()
        stopped.set()
        self.assertFalse(bridge.wait_runtime_refresh(0, 30, cancel_event=stopped))

    def test_stop_in_first_context_never_dispatches_to_second_context(self):
        bridge = WmpfCdpBridge()
        bridge.wait_runtime = mock.Mock(return_value=True)
        bridge._contexts = ["old-page", "second-page"]
        sent = []
        entered = threading.Event()
        errors = []

        def send(frame):
            sent.append(frame)
            if len(sent) == 1:
                entered.set()
            else:
                # Bound the old bug: second dispatch would otherwise wait again.
                bridge.cancel_pending()

        bridge._send_raw = send

        def evaluate():
            try:
                bridge.evaluate("readSearchState()", timeout=1)
            except RuntimeError as exc:
                errors.append(str(exc))

        worker = threading.Thread(target=evaluate)
        worker.start()
        self.assertTrue(entered.wait(1))
        bridge.cancel_pending()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(sent), 1, "停止后旧评估仍然向下一个页面发命令")
        self.assertTrue(errors)

    def test_resume_closes_old_applet_even_if_bridge_still_claims_ready(self):
        backend = DesktopWechatBackend()
        backend.cdp_bridge = mock.Mock()
        backend.cdp_bridge.runtime_ready = True
        backend._launch_applet = mock.Mock()
        backend.evaluate = mock.Mock(side_effect=[
            {"ok": True}, {"route": "globalSearch"},
        ])
        backend.pause()
        backend.cancel_event = threading.Event()
        backend.open_search_page()
        backend._launch_applet.assert_called_once()

    def test_old_socket_cannot_reintroduce_contexts_after_new_connection(self):
        bridge = WmpfCdpBridge()
        old, current = mock.Mock(), mock.Mock()
        bridge._clients = [current]
        bridge._contexts = ["current-page"]
        with mock.patch("wechat_shop_scraper.desktop_proto.decode_debug_message",
                        return_value={"category": "removeJsContext",
                                      "data": {"jscontext_id": "current-page"}}):
            bridge._on_message(b"late-old-socket-message", old)
        self.assertEqual(bridge._contexts, ["current-page"])

    def test_transient_recovery_uses_short_delay_not_60_second_keyword_wait(self):
        backend = mock.Mock()
        backend.collect_keyword.side_effect = [DesktopWechatError("页面调试连接超时"), "已完成"]
        listener = DesktopKeywordListener(
            keyword="", keywords=["当前词"], backend=backend,
            on_products=lambda _: None, retry_seconds=60,
        )
        listener._stop = mock.Mock()
        listener._stop.is_set.return_value = False
        listener._stop.wait.return_value = False
        listener._automation_loop()
        self.assertLessEqual(listener._stop.wait.call_args.args[0], 2)
        backend.recover_search_runtime.assert_called_once()


if __name__ == "__main__":
    unittest.main()
