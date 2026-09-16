from __future__ import annotations

import re
import threading
import unittest
from unittest import mock

from wechat_shop_scraper.desktop_keyword import (
    DesktopKeywordListener,
    DesktopWechatBackend,
    DesktopWechatError,
)


TIMEOUT = {
    "code": "TIMEOUT_ERROR",
    "message": "JavaScript execution timeout after 5000ms",
    "stack": "",
}


class DesktopRecoveryTests(unittest.TestCase):
    def test_detached_host_is_recovered_before_next_keyword_is_started(self):
        class DetachedBackend:
            needs_recovery = True

            def __init__(self):
                self.recoveries = 0
                self.keywords = []

            def recover_search_runtime(self):
                self.recoveries += 1
                self.needs_recovery = False

            def collect_keyword(self, keyword, _minimum, _maximum, on_page):
                self.keywords.append(keyword)
                on_page([])
                return "已完成"

            def pause(self):
                pass

        backend = DetachedBackend()
        finished = []
        listener = DesktopKeywordListener(
            keyword="", keywords=["第一词"], on_products=lambda _: None,
            on_keyword_finished=lambda *args: finished.append(args),
            backend=backend, retry_seconds=0,
        )
        listener._automation_loop()
        self.assertEqual(backend.recoveries, 1)
        self.assertEqual(backend.keywords, ["第一词"])
        self.assertEqual(finished[0][0], "第一词")

    def test_keyword_reads_beyond_300_pages_until_server_reports_last_page(self):
        backend = DesktopWechatBackend()
        backend.open_search_page = mock.Mock()
        backend._wait_search_stable = mock.Mock(return_value={})
        backend.evaluate = mock.Mock(return_value={"ok": True})
        pages = []
        def page(script, **_kwargs):
            offset = int(re.search(r"const offset=(\d+);", script).group(1))
            pages.append(offset)
            return {"ok": True, "products": [], "hasMore": offset < 304,
                    "nextOffset": offset + 1, "searchCommInfo": {}}
        backend._evaluate_page_async = page
        self.assertEqual(backend.collect_keyword("超过三百页", None, None, lambda _: None), "已完成")
        self.assertEqual(pages, list(range(305)))

    def test_stop_finishes_applet_session_before_detaching(self):
        backend = DesktopWechatBackend()
        backend.cancel_event.set()
        backend.weixin_session = mock.Mock()
        backend.wmpf_session = mock.Mock()
        backend.weixin_script = mock.Mock()
        backend.wmpf_script = mock.Mock()
        backend.thread_id = 123
        backend.call_batch = mock.Mock()
        backend.cdp_bridge = mock.Mock()
        backend.stop()
        backend.call_batch.assert_called_once_with(
            [{"method": "XWeb.CloseApplet", "params": {"app_id": "wxca8d4b8e8feedc2a"}}],
            timeout=2.0, _allow_stopped=True,
        )
        self.assertTrue(backend.cancel_event.is_set())
        self.assertFalse(backend.active)

    def test_cancelled_evaluation_cannot_reopen_connection(self):
        backend = DesktopWechatBackend()
        backend.cancel_event.set()
        backend.call = mock.Mock(return_value={"ok": True})
        backend._evaluate_with_cdp = mock.Mock(return_value={"ok": True})
        with self.assertRaisesRegex(DesktopWechatError, "已停止"):
            backend.evaluate("readOnly()")
        backend.call.assert_not_called()
        backend._evaluate_with_cdp.assert_not_called()

    def test_stop_during_last_page_never_completes_keyword(self):
        backend = mock.Mock()
        finished = mock.Mock()
        completed = mock.Mock()
        listener = DesktopKeywordListener(
            keyword="", keywords=["床垫"], on_products=lambda _: None,
            on_keyword_finished=finished, on_complete=completed, backend=backend,
        )
        def stop_at_return(*_args):
            listener._stop.set()
            return "已完成"
        backend.collect_keyword.side_effect = stop_at_return
        listener._automation_loop()
        finished.assert_not_called()
        completed.assert_not_called()

    def test_startup_recovers_transient_timeout_before_starting_worker(self):
        backend = mock.Mock()
        backend.open_search_page.side_effect = [DesktopWechatError(str(TIMEOUT)), None]
        backend.collect_keyword.return_value = "已完成"
        completed = threading.Event()
        listener = DesktopKeywordListener(
            keyword="", keywords=["床垫"], on_products=lambda _: None,
            on_complete=completed.set, backend=backend,
        )
        with mock.patch("wechat_shop_scraper.desktop_keyword.PAGE_REENTRY_RETRY_SECONDS", 0):
            listener.start()
            self.assertTrue(completed.wait(1))
        self.assertEqual(backend.start.call_count, 2)
        self.assertEqual(backend.open_search_page.call_count, 2)
        backend.stop.assert_not_called()
        listener.stop()

    def test_startup_recovers_runtime_not_ready_after_stop(self):
        backend = mock.Mock()
        backend.open_search_page.side_effect = [
            DesktopWechatError("电脑版微信小店已打开，但内部采集通道未就绪"), None,
        ]
        backend.collect_keyword.return_value = "已完成"
        completed = threading.Event()
        listener = DesktopKeywordListener(
            keyword="", keywords=["床垫"], on_products=lambda _: None,
            on_complete=completed.set, backend=backend,
        )
        with mock.patch("wechat_shop_scraper.desktop_keyword.PAGE_REENTRY_RETRY_SECONDS", 0):
            listener.start()
            self.assertTrue(completed.wait(1))
        listener.stop()
        self.assertEqual(backend.start.call_count, 2)

    def test_builtin_entry_reuses_visible_search_page_before_launching_again(self):
        backend = DesktopWechatBackend()
        backend._ensure_collection_runtime = mock.Mock()
        backend._normal_entry_opened_search = True
        bridge = mock.Mock()
        bridge.wait_runtime.return_value = True
        bridge.runtime_ready = True
        backend.cdp_bridge = bridge
        backend._navigate_search_page = mock.Mock()
        backend._launch_applet = mock.Mock()
        backend.evaluate = mock.Mock(return_value={"route": "pages/globalSearch/index"})

        backend._open_search_page_once(timeout=1)

        backend._launch_applet.assert_not_called()
        backend._navigate_search_page.assert_not_called()
        backend.evaluate.assert_called_once()
        self.assertFalse(backend._normal_entry_opened_search)

    def test_force_start_reuses_existing_search_page_instead_of_reissuing_navigation(self):
        backend = DesktopWechatBackend()
        backend._ensure_collection_runtime = mock.Mock()
        bridge = mock.Mock()
        bridge.runtime_ready = True
        backend.cdp_bridge = bridge
        backend.evaluate = mock.Mock(return_value={"route": "pages/globalSearch/index"})
        backend._launch_applet = mock.Mock()
        backend._navigate_search_page = mock.Mock()

        backend._open_search_page_once(force=True, timeout=1)

        backend._launch_applet.assert_not_called()
        backend._navigate_search_page.assert_not_called()
        backend.evaluate.assert_called_once()

    def test_builtin_entry_falls_back_to_launch_when_page_connection_never_arrives(self):
        backend = DesktopWechatBackend()
        backend._ensure_collection_runtime = mock.Mock()
        backend._normal_entry_opened_search = True
        bridge = mock.Mock()
        bridge.wait_runtime.return_value = False
        bridge.runtime_ready = False
        backend.cdp_bridge = bridge
        backend._launch_applet = mock.Mock()
        backend._navigate_search_page = mock.Mock()

        backend._open_search_page_once(timeout=1)

        backend._launch_applet.assert_called_once_with(1)
        backend._navigate_search_page.assert_called_once_with(1)
        self.assertFalse(backend._normal_entry_opened_search)

    def test_async_page_read_uses_short_start_and_poll_calls(self):
        backend = DesktopWechatBackend()
        backend.evaluate = mock.Mock(side_effect=[
            {"jobStarted": True}, {"done": False},
            {"done": True, "value": '{"ok":true,"products":[]}'},
        ])
        self.assertEqual(backend._read_search_page("(async()=>{})()", 1),
                         {"ok": True, "products": []})
        calls = backend.evaluate.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertIn("Promise.resolve().then", calls[0].args[0])
        self.assertIn("const box=globalThis", calls[1].args[0])
        self.assertIn("clearTimeout", calls[-1].args[0])
        self.assertTrue(all(call.kwargs["timeout"] <= 5 for call in calls))

    def test_ready_page_needs_only_start_and_destructive_poll(self):
        backend = DesktopWechatBackend()
        backend.evaluate = mock.Mock(side_effect=[
            {"jobStarted": True}, {"done": True, "value": {"ok": True}},
        ])
        self.assertEqual(backend._evaluate_page_async("Promise.resolve({ok:true})", 30), {"ok": True})
        self.assertEqual(backend.evaluate.call_count, 2)
        self.assertIn("delete globalThis", backend.evaluate.call_args.args[0])

    def test_async_page_job_detects_lost_runtime_and_cleans_up(self):
        backend = DesktopWechatBackend()
        backend.evaluate = mock.Mock(side_effect=[{"jobStarted": True}, {"missing": True}, True])
        with self.assertRaisesRegex(DesktopWechatError, "运行环境已失效"):
            backend._evaluate_page_async("Promise.resolve(1)", 30)
        self.assertIn("clearTimeout", backend.evaluate.call_args.args[0])

    def test_page_retry_is_bounded_and_permanent_errors_are_not_retried(self):
        for message, expected in ((str(TIMEOUT), 3), ("请完成验证", 1)):
            with self.subTest(message=message):
                backend = DesktopWechatBackend()
                backend._evaluate_page_async = mock.Mock(side_effect=DesktopWechatError(message))
                with mock.patch("wechat_shop_scraper.desktop_keyword.PAGE_READ_RETRY_SECONDS", 0):
                    with self.assertRaisesRegex(DesktopWechatError, "第 4 页读取失败"):
                        backend._read_search_page("same cursor", 4)
                self.assertEqual(backend._evaluate_page_async.call_count, expected)

    def test_cancel_during_page_wait_does_not_commit_late_results(self):
        backend = DesktopWechatBackend()
        def late_result(*_args, **_kwargs):
            backend.cancel_event.set()
            return {"ok": True, "products": []}
        backend._evaluate_page_async = late_result
        with self.assertRaisesRegex(DesktopWechatError, "已停止"):
            backend._read_search_page("page", 1)

    def test_cancel_interrupts_page_backoff(self):
        backend = DesktopWechatBackend()
        backend._evaluate_page_async = mock.Mock(side_effect=DesktopWechatError(str(TIMEOUT)))
        backend.cancel_event = mock.Mock()
        backend.cancel_event.is_set.return_value = False
        backend.cancel_event.wait.return_value = True
        with self.assertRaisesRegex(DesktopWechatError, "已停止"):
            backend._read_search_page("page", 1)
        self.assertEqual(backend._evaluate_page_async.call_count, 1)

    def test_empty_page_is_a_success_and_does_not_trigger_recovery(self):
        backend = DesktopWechatBackend()
        backend.open_search_page = mock.Mock()
        backend._wait_search_stable = mock.Mock(return_value={})
        backend.evaluate = mock.Mock(return_value={"ok": True, "products": [], "hasMore": False})
        backend._evaluate_page_async = backend.evaluate
        on_page = mock.Mock()
        with mock.patch("wechat_shop_scraper.desktop_keyword.time.sleep"):
            self.assertEqual(backend.collect_keyword("无结果词", 50, 2000, on_page), "已完成")
        on_page.assert_not_called()

    def test_timeout_event_envelope_uses_cdp_fallback(self):
        for reply in (
            {"success": False, "data": None, "error": TIMEOUT},
            {"success": True, "data": {"ok": False, "error": TIMEOUT}},
        ):
            with self.subTest(reply=reply):
                backend = DesktopWechatBackend()
                backend.call = mock.Mock(return_value=reply)
                backend._evaluate_with_cdp = mock.Mock(return_value={"ok": True})
                self.assertEqual(backend.evaluate("readOnly()"), {"ok": True})
                backend._evaluate_with_cdp.assert_called_once()

    def test_fourth_page_timeout_retries_same_cursor_without_replaying_search(self):
        backend = DesktopWechatBackend()
        backend.open_search_page = mock.Mock()
        backend._wait_search_stable = mock.Mock(return_value={"searchCommInfo": {}})
        offsets = []
        delivered = []
        failed = False

        def evaluate(script, **_kwargs):
            nonlocal failed
            match = re.search(r"const offset=(\d+);", script)
            if not match:
                return {"ok": True}
            offset = int(match.group(1))
            offsets.append(offset)
            if offset == 3 and not failed:
                failed = True
                return {"ok": False, "error": TIMEOUT}
            return {
                "ok": True,
                "products": [{"productId": str(10000123456000 + offset),
                              "title": "分页商品", "sellingPrice": 9900}],
                "hasMore": offset < 4,
                "nextOffset": offset + 1,
                "searchCommInfo": {"cursor": offset + 1},
            }

        backend.evaluate = mock.Mock(return_value={"ok": True})
        backend._evaluate_page_async = evaluate
        with mock.patch("wechat_shop_scraper.desktop_keyword.time.sleep"), mock.patch(
            "wechat_shop_scraper.desktop_keyword.PAGE_READ_RETRY_SECONDS", 0, create=True
        ):
            outcome = backend.collect_keyword("床垫", 50, 2000, delivered.extend)
        self.assertEqual(outcome, "已完成")
        self.assertEqual(offsets, [0, 1, 2, 3, 3, 4])
        self.assertEqual(len({card.product_id for card in delivered}), 5)
        self.assertEqual(len(delivered), 5)
        backend.open_search_page.assert_called_once()

    def test_failed_keyword_is_never_committed_or_skipped(self):
        backend = mock.Mock()
        backend.collect_keyword.side_effect = DesktopWechatError(str(TIMEOUT))
        finished = mock.Mock()
        errors = []
        listener = DesktopKeywordListener(
            keyword="", keywords=["失败词", "不能跳到这里"],
            on_products=lambda _: None, on_keyword_finished=finished,
            on_error=errors.append, backend=backend, retry_seconds=0,
        )
        listener._automation_loop()
        finished.assert_not_called()
        self.assertTrue(errors)
        self.assertTrue(all(call.args[0] == "失败词"
                            for call in backend.collect_keyword.call_args_list))
        self.assertIn("断点", errors[0])

    def test_recovery_retries_current_keyword_after_rebuilding_connection(self):
        backend = mock.Mock()
        backend.collect_keyword.side_effect = [DesktopWechatError(str(TIMEOUT)), "已完成"]
        finished = mock.Mock()
        errors = []
        listener = DesktopKeywordListener(
            keyword="", keywords=["床垫"], on_products=lambda _: None,
            on_keyword_finished=finished, on_error=errors.append,
            backend=backend, retry_seconds=0,
        )
        listener._automation_loop()
        backend.recover_search_runtime.assert_called_once()
        self.assertEqual(backend.collect_keyword.call_count, 2)
        self.assertFalse(errors)
        finished.assert_called_once_with("床垫", 1, 1, "已完成")

    def test_safety_verification_does_not_trigger_reconnection(self):
        backend = mock.Mock()
        backend.collect_keyword.side_effect = DesktopWechatError("请完成验证")
        listener = DesktopKeywordListener(
            keyword="", keywords=["床垫"], on_products=lambda _: None,
            backend=backend, retry_seconds=0,
        )
        listener._automation_loop()
        backend.recover_search_runtime.assert_not_called()
        self.assertEqual(backend.collect_keyword.call_count, 1)


if __name__ == "__main__":
    unittest.main()
