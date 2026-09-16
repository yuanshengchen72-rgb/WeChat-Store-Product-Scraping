import json
import threading
import unittest
from unittest import mock

from wechat_shop_scraper.desktop_keyword import (
    DesktopWechatBackend, DesktopWechatError, _build_weixin_script,
    DesktopRecoveryExhausted, _is_transient_desktop_error,
)
from wechat_shop_scraper.desktop_proto import WmpfCdpBridge


def native_reply(**state):
    return {"success": True, "data": json.dumps(state)}


READY = native_reply(pages="function", modules="function", wx="object")


class CollectionRuntimeTests(unittest.TestCase):
    def backend(self):
        backend = DesktopWechatBackend()
        backend.cdp_bridge = mock.Mock(runtime_ready=True)
        backend._launch_applet = mock.Mock()
        backend.call = mock.Mock()
        return backend

    def test_unsupported_version_does_not_disable_native_evaluation(self):
        backend = self.backend()
        backend.profile = mock.Mock(source="微信 4.1.13.65 / WMPF 25560")
        backend.call.side_effect = DesktopWechatError("不支持的method: XWeb.version")
        backend._probe_xweb_version()
        self.assertIsNone(backend._xweb_replies_available)

    def test_registers_unbound_native_session_once_then_verifies_capabilities(self):
        backend = self.backend()
        backend.call.side_effect = [
            DesktopWechatError("session未绑定或CDP客户端不可用"),
            native_reply(pages="undefined", modules="undefined", wx="undefined"),
            READY,
        ]
        backend._ensure_collection_runtime(1)
        backend._launch_applet.assert_called_once_with(1)
        self.assertIs(backend._xweb_replies_available, True)
        self.assertEqual(backend.call.call_count, 3)
        backend.cdp_bridge.evaluate.assert_not_called()

    def test_usable_native_session_is_reused_without_relaunch(self):
        backend = self.backend()
        backend.call.return_value = READY
        backend._ensure_collection_runtime(1)
        backend._launch_applet.assert_not_called()

    def test_rendering_target_is_not_a_usable_collector(self):
        backend = self.backend()
        backend.call.return_value = native_reply(pages="undefined", modules="undefined", wx="undefined")
        with self.assertRaises(DesktopWechatError) as raised:
            backend._ensure_collection_runtime(0.01)
        self.assertTrue(_is_transient_desktop_error(raised.exception))
        backend._launch_applet.assert_called_once()
        self.assertIsNot(backend._xweb_replies_available, True)

    def test_uninitialized_host_is_not_sent_native_launch(self):
        backend = self.backend()
        backend.cdp_bridge.runtime_ready = False
        backend.cdp_bridge.wait_runtime.return_value = False
        backend.call.side_effect = DesktopWechatError("session未绑定或CDP客户端不可用")
        with self.assertRaises(DesktopWechatError):
            backend._ensure_collection_runtime(0.01)
        backend._launch_applet.assert_not_called()

    def test_failed_probe_reuses_verified_normal_entry_before_native_launch(self):
        backend = self.backend()
        backend.normal_shop_opener = mock.Mock()
        backend._bootstrap_normal_shop = mock.Mock()
        backend._normal_entry_opened_search = False
        backend.call.side_effect = [
            DesktopWechatError("session未绑定或CDP客户端不可用"),
            READY,
        ]
        backend._ensure_collection_runtime(1)
        backend._bootstrap_normal_shop.assert_called_once()
        backend._launch_applet.assert_not_called()

    def test_search_state_transient_error_is_retried_until_stable(self):
        backend = self.backend()
        backend.evaluate = mock.Mock(side_effect=[
            {"keyword": "", "error": "页面仍在初始化"},
            {"keyword": "测试词", "loading": False},
            {"keyword": "测试词", "loading": False},
            {"keyword": "测试词", "loading": False},
        ])
        state = backend._wait_search_stable("测试词", timeout=2, stage="搜索")
        self.assertEqual(state["keyword"], "测试词")

    def test_native_dispatch_script_holds_task_until_explicit_release(self):
        script = _build_weixin_script(123)
        self.assertIn("const held = new Map()", script)
        self.assertIn("releaseDispatch", script)
        self.assertIn("encodeURIComponent", script)
        self.assertNotIn("event:'on-command'", script)

    def test_call_batch_releases_native_dispatch_after_reply(self):
        backend = DesktopWechatBackend()
        backend.weixin_session = mock.Mock(is_detached=False)
        backend.wmpf_session = mock.Mock(is_detached=False)
        backend.wmpf_script = mock.Mock()
        released: list[str] = []

        class Exports:
            def dispatch(_self, payload, _thread_id):
                call_id = json.loads(payload)["call_id"]
                backend._on_wmpf_message({
                    "type": "send",
                    "payload": {"event": "reply", "call_id": call_id,
                                "error_code": 0, "responses": []},
                }, None)

            def release_dispatch(_self, call_id):
                released.append(call_id)

        script = mock.Mock()
        script.exports_sync = Exports()
        backend.weixin_script = script
        backend.thread_id = 123

        self.assertEqual(backend.call_batch([{"method": "XWeb.version"}], timeout=1), [])
        self.assertEqual(len(released), 1)
        self.assertTrue(released[0].startswith("desktop-keyword-"))

    def test_recovery_has_a_safety_cap_instead_of_restarting_forever(self):
        backend = DesktopWechatBackend()
        backend._restore_normal_shop = mock.Mock(side_effect=DesktopWechatError("连接超时"))
        backend._try_rebind_existing_shop = mock.Mock(return_value=False)
        backend._mark_native_lost = mock.Mock()
        backend.stop = mock.Mock()
        backend.cancel_event.wait = mock.Mock(return_value=False)
        with mock.patch("wechat_shop_scraper.desktop_keyword.HOST_RECOVERY_BACKOFF_SECONDS", (0,)):
            with self.assertRaises(DesktopRecoveryExhausted):
                backend._recover_normal_runtime(1)
        self.assertEqual(backend._restore_normal_shop.call_count, 5)

    def test_cancel_does_not_probe_or_launch(self):
        backend = self.backend()
        backend.cancel_event.set()
        with self.assertRaisesRegex(DesktopWechatError, "已停止"):
            backend._ensure_collection_runtime(1)
        backend.call.assert_not_called()
        backend._launch_applet.assert_not_called()

    def test_pause_invalidates_cached_readiness_before_continue(self):
        backend = self.backend()
        backend._search_page_ready = True
        backend.pause()
        self.assertFalse(backend._search_page_ready)

    def test_module_error_is_preserved_as_recoverable_context_failure(self):
        backend = self.backend()
        backend.evaluate = mock.Mock(return_value={"keyword": "", "error": "ReferenceError: require is not defined"})
        with self.assertRaisesRegex(DesktopWechatError, "require is not defined") as raised:
            backend._wait_search_stable("test", timeout=0.01, stage="搜索")
        self.assertTrue(_is_transient_desktop_error(raised.exception))
        backend.evaluate.assert_called_once()

    def test_runtime_wait_allows_socket_to_arrive(self):
        bridge = WmpfCdpBridge()
        def arrive():
            bridge._clients.append(object())
            bridge._runtime_ready.set()
            bridge._setup_complete.set()
        timer = threading.Timer(0.02, arrive)
        timer.start()
        try:
            self.assertTrue(bridge.wait_runtime(0.5))
        finally:
            timer.join()

    def test_module_failure_keeps_recovery_type_through_page_wrapper(self):
        backend = self.backend()
        backend._evaluate_page_async = mock.Mock(return_value={"ok": False, "error": "search-module-unavailable"})
        with mock.patch("wechat_shop_scraper.desktop_keyword.PAGE_READ_RETRY_SECONDS", 0):
            with self.assertRaises(DesktopWechatError) as raised:
                backend._read_search_page("readPage()", 1)
        self.assertTrue(_is_transient_desktop_error(raised.exception))

    def test_visible_window_with_failed_state_probe_is_not_declared_ready(self):
        backend = self.backend()
        backend._normal_entry_opened_search = True
        backend._ensure_collection_runtime = mock.Mock()
        backend.evaluate = mock.Mock(side_effect=DesktopWechatError("脚本超时"))
        with self.assertRaises(DesktopWechatError):
            backend._open_search_page_once(timeout=0.01)
        self.assertFalse(backend._search_page_ready)

    def test_runtime_wait_can_be_cancelled_without_socket(self):
        bridge = WmpfCdpBridge()
        cancelled = threading.Event()
        cancelled.set()
        self.assertFalse(bridge.wait_runtime(1, cancel_event=cancelled))


if __name__ == "__main__":
    unittest.main()
