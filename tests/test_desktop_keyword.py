from __future__ import annotations

import base64
import ctypes
import json
import threading
import unittest
from concurrent.futures import TimeoutError as FutureTimeoutError
from unittest import mock

from wechat_shop_scraper.desktop_keyword import (
    DesktopWechatBackend,
    DesktopWechatError,
    DesktopKeywordListener,
    OPEN_SEARCH_SCRIPT,
    SEARCH_FILTER_SCRIPT,
    SEARCH_PAGE_SCRIPT,
    SEARCH_START_SCRIPT,
    _launch_wmpf_host,
    _decode_xweb_response,
    _is_transient_desktop_error,
    desktop_cards_from_rows,
)
from wechat_shop_scraper.desktop_proto import (
    WmpfCdpBridge,
    decode_debug_message,
    encode_action_command,
    wrap_cdp_context_payload,
    wrap_connect_js_context,
)
from wechat_shop_scraper.gui import (
    MODE_DESKTOP_KEYWORD,
    MODE_KEYWORD,
    MODE_STORE,
    is_keyword_mode,
)


class DesktopProtoTests(unittest.TestCase):
    def test_search_page_does_not_use_generic_thumbnail_or_profile_fields_as_main_image(self):
        self.assertIn("'mainImages'", SEARCH_PAGE_SCRIPT)
        self.assertIn("'imageList'", SEARCH_PAGE_SCRIPT)
        self.assertIn("pending:true", OPEN_SEARCH_SCRIPT)
        self.assertNotIn("(async()=>", OPEN_SEARCH_SCRIPT)
        for field in ("'coverImg'", "'headImgUrl'", "'thumbImg'", "'url'"):
            self.assertNotIn(field, SEARCH_PAGE_SCRIPT)

    def test_action_command_matches_verified_wmpf_envelope(self) -> None:
        payload = encode_action_command("s", "c", ["[]"])

        self.assertEqual(base64.b64encode(payload).decode(), "CgFzEgFjGgJbXQ==")

    def test_connect_context_frame_round_trips(self) -> None:
        frame = wrap_connect_js_context(1, "7")

        self.assertEqual(
            frame.hex(),
            "08011a10636f6e6e6563744a73436f6e7465787422030a013728003000",
        )
        self.assertEqual(
            decode_debug_message(frame),
            {
                "category": "connectJsContext",
                "data": {"jscontext_id": "7", "jscontext_name": ""},
            },
        )

    def test_cdp_context_frame_round_trips(self) -> None:
        frame = wrap_cdp_context_payload("abc", 3, 9, "7")

        self.assertEqual(
            frame.hex(),
            "08031a0e6368726f6d65446576746f6f6c73220a080912036162631a013728003000",
        )
        self.assertEqual(
            decode_debug_message(frame)["data"],
            {"op_id": 9, "payload": "abc", "jscontext_id": "7"},
        )

    def test_context_registration_alone_does_not_mark_runtime_ready(self) -> None:
        bridge = WmpfCdpBridge()
        bridge._clients.append(mock.Mock())

        with (
            mock.patch(
                "wechat_shop_scraper.desktop_proto.decode_debug_message",
                return_value={
                    "category": "addJsContext",
                    "data": {"jscontext_id": "7"},
                },
            ),
            mock.patch.object(bridge, "_send_raw"),
            mock.patch("wechat_shop_scraper.desktop_proto.threading.Thread"),
        ):
            bridge._on_message(b"context")

        self.assertFalse(bridge.runtime_ready)

    def test_connect_context_registration_is_accepted_like_add_context(self) -> None:
        bridge = WmpfCdpBridge()
        bridge._clients.append(mock.Mock())

        with (
            mock.patch(
                "wechat_shop_scraper.desktop_proto.decode_debug_message",
                return_value={
                    "category": "connectJsContext",
                    "data": {"jscontext_id": "25560-page"},
                },
            ),
            mock.patch.object(bridge, "_send_raw"),
            mock.patch("wechat_shop_scraper.desktop_proto.threading.Thread"),
        ):
            bridge._on_message(b"context")

        self.assertEqual(bridge._contexts, ["25560-page"])

    def test_runtime_wait_retries_future_timeouts_instead_of_aborting(self) -> None:
        bridge = WmpfCdpBridge()
        bridge._clients.append(mock.Mock())

        with mock.patch.object(bridge, "call", side_effect=FutureTimeoutError):
            self.assertFalse(bridge.wait_runtime(0.02))

    def test_runtime_wait_expires_without_a_debug_client(self) -> None:
        bridge = WmpfCdpBridge()
        self.assertFalse(bridge.wait_runtime(0.01))

    def test_25560_binds_the_store_appindex_target_session(self) -> None:
        bridge = WmpfCdpBridge()
        bridge._connection_generation = 1
        target = {
            "targetId": "store-page",
            "type": "page",
            "title": "AppIndex",
            "url": "https://servicewechat.com/wxca8d4b8e8feedc2a/4677/page-frame.html",
        }
        bridge.call = mock.Mock(side_effect=[
            {"id": 1, "result": {"targetInfos": [target]}},
            {"id": 2, "result": {"sessionId": "store-session"}},
            {"id": 3, "result": {}},
            {"id": 4, "result": {}},
        ])
        bridge._bind_appindex_target(1, 0, 0)
        self.assertEqual(bridge._target_session_id, "store-session")
        self.assertEqual(bridge.call.call_args_list[1].args[0], "Target.attachToTarget")


class DesktopKeywordTests(unittest.TestCase):
    def test_local_unbound_session_messages_are_recoverable(self) -> None:
        self.assertTrue(_is_transient_desktop_error(
            DesktopWechatError("内置入口已显示搜索页，但 CDP 客户端仍未绑定；将自动重试当前关键词")
        ))
        self.assertTrue(_is_transient_desktop_error(
            DesktopWechatError("小店窗口仍在但 CDP 客户端尚未绑定；保留现有页面重试")
        ))

    def test_rearms_normal_entry_after_hooks_attach_when_page_was_preopened(self) -> None:
        backend = DesktopWechatBackend(normal_shop_opener=mock.Mock())
        backend._normal_entry_opened_search = True
        backend.cdp_bridge = mock.Mock(runtime_ready=False)
        backend._bootstrap_normal_shop = mock.Mock()
        backend.cdp_bridge.wait_runtime = mock.Mock(return_value=True)

        backend._rearm_normal_entry_after_attach()

        backend._bootstrap_normal_shop.assert_called_once_with(force_relaunch=True)
        backend.cdp_bridge.wait_runtime.assert_called_once()

    def test_cold_host_launch_uses_hidden_shell_protocol(self) -> None:
        shell_execute = mock.Mock(return_value=33)
        with (
            mock.patch.object(
                ctypes.windll.shell32,
                "ShellExecuteW",
                shell_execute,
            ),
            mock.patch(
                "wechat_shop_scraper.desktop_keyword._wmpf_root_pid",
                return_value=4321,
            ),
            mock.patch(
                "wechat_shop_scraper.desktop_keyword._powershell_json",
                return_value={"MainWindowHandle": 12, "MainWindowTitle": "小店"},
            ),
            mock.patch("os.startfile") as startfile,
        ):
            pid = _launch_wmpf_host(9104, timeout=1.0)

        self.assertEqual(pid, 4321)
        startfile.assert_not_called()
        shell_execute.assert_called_once_with(
            None,
            "open",
            "weixin://launchapplet/?app_id=wxca8d4b8e8feedc2a",
            None,
            None,
            0,
        )

    def test_xweb_event_envelope_is_unwrapped_to_javascript_result(self) -> None:
        raw = json.dumps(
            {
                "code": 0,
                "data": {
                    "data": {
                        "error": "",
                        "result": {
                            "data": '{"ok":true}',
                            "executionTime": 0,
                            "success": True,
                        },
                        "task_id": "",
                    },
                    "event": "XWeb.OnEvaluateJavaScript",
                },
                "message": "success",
            }
        )

        self.assertEqual(
            _decode_xweb_response(raw),
            {"data": '{"ok":true}', "executionTime": 0, "success": True},
        )

    def test_cold_launch_is_blocked_until_the_runtime_is_ready(self) -> None:
        backend = DesktopWechatBackend()
        backend.weixin_session = mock.Mock(is_detached=False)
        backend.wmpf_session = mock.Mock(is_detached=False)
        backend.weixin_script = mock.Mock()
        backend.wmpf_script = mock.Mock()
        backend.cdp_bridge = mock.Mock()
        backend.cdp_bridge.runtime_ready = False
        backend.cdp_bridge.connection_generation = 7
        backend.call = mock.Mock()
        backend.dispatch_async = mock.Mock()
        with self.assertRaises(DesktopWechatError):
            backend._launch_applet()
        backend.call.assert_not_called()
        backend.dispatch_async.assert_not_called()

    def test_warm_launch_is_async_and_waits_for_a_new_runtime_generation(self) -> None:
        backend = DesktopWechatBackend()
        backend.weixin_session = mock.Mock(is_detached=False)
        backend.wmpf_session = mock.Mock(is_detached=False)
        backend.weixin_script = mock.Mock()
        backend.wmpf_script = mock.Mock()
        backend.cdp_bridge = mock.Mock()
        backend.cdp_bridge.runtime_ready = True
        backend.cdp_bridge.connection_generation = 7
        backend.cdp_bridge.wait_runtime_refresh.return_value = True
        backend.call = mock.Mock()
        backend.dispatch_async = mock.Mock()

        backend._launch_applet()

        backend.call.assert_called_once_with(
            "XWeb.CloseApplet", {"app_id": "wxca8d4b8e8feedc2a"}, timeout=5.0
        )
        backend.dispatch_async.assert_called_once()
        backend.cdp_bridge.prepare_relaunch.assert_called_once()
        backend.cdp_bridge.wait_runtime_refresh.assert_called_once_with(
            7, 25.0, cancel_event=backend.cancel_event, abort_event=backend._native_lost
        )

    def test_evaluate_falls_back_to_cdp_when_xweb_command_times_out(self) -> None:
        backend = DesktopWechatBackend()
        backend.log = mock.Mock()
        backend.call = mock.Mock(
            side_effect=DesktopWechatError(
                "电脑版微信后台命令超时：XWeb.EvaluateJavaScript"
            )
        )
        backend.cdp_bridge = mock.Mock()
        backend.cdp_bridge.wait_runtime.return_value = True
        backend.cdp_bridge.evaluate.return_value = '{"ok":true}'

        result = backend.evaluate("1 + 1", timeout=5.0)

        self.assertEqual(result, {"ok": True})
        backend.cdp_bridge.evaluate.assert_called_once_with("1 + 1", 5.0)

    def test_evaluate_falls_back_to_cdp_when_xweb_session_is_not_bound(self) -> None:
        """A visible .65 shop can report an unbound XWeb session during handover.

        The page debug socket is already usable in this state, so treating the
        result as a fatal startup error unnecessarily stops the keyword run.
        """
        backend = DesktopWechatBackend()
        backend.log = mock.Mock()
        backend.call = mock.Mock(
            return_value={
                "ok": False,
                "error": "session未绑定或CDP客户端不可用",
            }
        )
        backend.cdp_bridge = mock.Mock()
        backend.cdp_bridge.wait_runtime.return_value = True
        backend.cdp_bridge.evaluate.return_value = '{"ok":true}'

        result = backend.evaluate("1 + 1", timeout=5.0)

        self.assertEqual(result, {"ok": True})
        backend.cdp_bridge.evaluate.assert_called_once_with("1 + 1", 5.0)

    def test_search_navigation_retries_empty_xweb_result_via_cdp(self) -> None:
        backend = DesktopWechatBackend()
        backend.log = mock.Mock()
        backend.evaluate = mock.Mock(
            side_effect=[
                None,
                {"ok": True, "already": True},
                {"route": "subpackages/searchCenter/pages/globalSearch/globalSearch"},
            ]
        )

        backend._navigate_search_page(timeout=0.1)

        self.assertEqual(backend.evaluate.call_count, 3)
        self.assertNotIn("prefer_cdp", backend.evaluate.call_args_list[1].kwargs)

    def test_version_probe_timeout_is_non_fatal(self) -> None:
        messages: list[str] = []
        backend = DesktopWechatBackend()
        backend.log = messages.append
        backend._xweb_replies_available = None
        backend.call = mock.Mock(
            side_effect=DesktopWechatError("电脑版微信后台命令超时：XWeb.version")
        )

        backend._probe_xweb_version()

        self.assertIsNone(backend._xweb_replies_available)
        self.assertTrue(any("继续" in message for message in messages))

    def test_25560_unsupported_version_probe_defers_to_capability_probe(self) -> None:
        backend = DesktopWechatBackend()
        backend.profile = mock.Mock(source="微信 4.1.13.65 / WMPF 25560")
        backend.call = mock.Mock(side_effect=DesktopWechatError("不支持的method: XWeb.version"))

        backend._probe_xweb_version()

        self.assertIsNone(backend._xweb_replies_available)

    def test_evaluate_uses_cdp_immediately_after_version_probe_timeout(self) -> None:
        backend = DesktopWechatBackend()
        backend.log = mock.Mock()
        backend._xweb_replies_available = False
        backend.call = mock.Mock()
        backend.cdp_bridge = mock.Mock()
        backend.cdp_bridge.wait_runtime.return_value = True
        backend.cdp_bridge.evaluate.return_value = '{"ok":true}'

        result = backend.evaluate("1 + 1", timeout=5.0)

        self.assertEqual(result, {"ok": True})
        backend.call.assert_not_called()

    def test_desktop_mode_uses_the_shared_keyword_business_flow(self) -> None:
        self.assertTrue(is_keyword_mode(MODE_DESKTOP_KEYWORD))
        self.assertTrue(is_keyword_mode(MODE_KEYWORD))
        self.assertFalse(is_keyword_mode(MODE_STORE))

    def test_search_scripts_use_product_good_reviews_price_and_direct_paging(self) -> None:
        self.assertIn("searchType:1", SEARCH_START_SCRIPT)
        self.assertNotIn("await sleep", SEARCH_START_SCRIPT)
        self.assertIn("const keyword", SEARCH_FILTER_SCRIPT)
        self.assertIn("好评多", SEARCH_FILTER_SCRIPT)
        self.assertIn("priceRange.minPrice", SEARCH_FILTER_SCRIPT)
        self.assertIn("onFilterChange", SEARCH_FILTER_SCRIPT)
        self.assertNotIn("await sleep", SEARCH_FILTER_SCRIPT)
        self.assertIn("ecCardPackageSearch", SEARCH_PAGE_SCRIPT)

    def test_rows_are_converted_to_the_same_product_model(self) -> None:
        cards = desktop_cards_from_rows(
            [
                {
                    "productId": "10000123456789",
                    "title": "测试商品",
                    "mainImage": "https://example.test/image.jpg",
                    "sellingPrice": 5090,
                    "sales": 321,
                    "salesText": "已售321",
                    "shop": "测试小店",
                }
            ],
            "测试词",
        )

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].keyword, "测试词")
        self.assertEqual(cards[0].price_fen, 5090)
        self.assertEqual(cards[0].sales, 321)
        self.assertEqual(cards[0].source, "电脑版微信关键词搜索")

    def test_missing_sales_wording_stays_blank_instead_of_zero(self) -> None:
        cards = desktop_cards_from_rows(
            [
                {
                    "productId": "10000123456789",
                    "title": "没有销量文案的商品",
                    "sellingPrice": 5090,
                    "sales": 0,
                    "salesText": "",
                    "salesKnown": False,
                }
            ],
            "测试词",
        )

        self.assertIsNone(cards[0].sales)
        self.assertEqual(cards[0].display_sales, "")

    def test_listener_keeps_checkpoint_outcomes_and_restarts_by_interval(self) -> None:
        class FakeBackend:
            def __init__(self) -> None:
                self.restarts = 0
                self.stopped = False

            def start(self) -> None:
                pass

            def open_search_page(self, timeout: float = 0) -> None:
                pass

            def collect_keyword(self, keyword, _minimum, _maximum, on_page):
                if keyword == "无筛选":
                    return "无“好评多”，已跳过"
                on_page([])
                return "已完成"

            def restart_search_runtime(self) -> None:
                self.restarts += 1

            def stop(self) -> None:
                self.stopped = True

            pause = stop

        backend = FakeBackend()
        completed = threading.Event()
        finished: list[tuple[str, str]] = []
        listener = DesktopKeywordListener(
            keyword="",
            keywords=["第一词", "无筛选", "第三词"],
            on_products=lambda _cards: None,
            on_complete=completed.set,
            on_keyword_finished=lambda keyword, _index, _total, outcome: finished.append(
                (keyword, outcome)
            ),
            restart_keyword_interval=2,
            backend=backend,  # type: ignore[arg-type]
            retry_seconds=0,
        )

        listener.start()
        self.assertTrue(completed.wait(2.0))
        listener.stop()

        self.assertEqual(
            finished,
            [
                ("第一词", "已完成"),
                ("无筛选", "无“好评多”，已跳过"),
                ("第三词", "已完成"),
            ],
        )
        self.assertEqual(backend.restarts, 1)
        self.assertTrue(backend.stopped)

    def test_failed_keyword_pauses_without_advancing_checkpoint(self) -> None:
        class FailingBackend:
            def __init__(self) -> None:
                self.calls = 0

            def start(self) -> None:
                pass

            def open_search_page(self, timeout: float = 0) -> None:
                pass

            def collect_keyword(self, *_args, **_kwargs):
                self.calls += 1
                raise RuntimeError("测试失败")

            def restart_search_runtime(self) -> None:
                pass

            def stop(self) -> None:
                pass

            pause = stop

        backend = FailingBackend()
        paused = threading.Event()
        errors: list[str] = []
        listener = DesktopKeywordListener(
            keyword="",
            keywords=["第一词", "第二词"],
            on_products=lambda _cards: None,
            on_error=lambda message: (errors.append(message), paused.set()),
            backend=backend,  # type: ignore[arg-type]
            retry_seconds=0,
        )

        listener.start()
        self.assertTrue(paused.wait(2.0))
        listener.stop()

        self.assertEqual(backend.calls, 4)
        self.assertIn("保留断点", errors[0])
        self.assertIn("第一词", errors[0])

    def test_desktop_page_reentry_retries_three_times(self) -> None:
        class FlakyBackend:
            def __init__(self) -> None:
                self.calls = 0

            def restart_search_runtime(self) -> None:
                self.calls += 1
                if self.calls < 3:
                    raise RuntimeError("暂时未就绪")

        backend = FlakyBackend()
        listener = DesktopKeywordListener(
            keyword="",
            keywords=["测试词"],
            on_products=lambda _cards: None,
            backend=backend,  # type: ignore[arg-type]
        )

        with mock.patch(
            "wechat_shop_scraper.desktop_keyword.PAGE_REENTRY_RETRY_SECONDS",
            0.0,
        ):
            listener._reenter_search_page()

        self.assertEqual(backend.calls, 3)


if __name__ == "__main__":
    unittest.main()
