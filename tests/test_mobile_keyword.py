from __future__ import annotations

import io
import socket
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from wechat_shop_scraper.mobile_keyword import (
    END_STABLE_ROUNDS,
    MobileKeywordListener,
    PAGE_STATE_EXPRESSION,
    WechatSearchUnavailable,
    _frida_server_asset_for_abi,
    _usb_serials,
    _wifi_ip_from_route,
    cards_from_dom,
    normalize_wifi_address,
    setup_frida_server_via_usb,
)
from wechat_shop_scraper.gui import (
    parse_keyword_list,
    parse_price_range,
    parse_restart_interval,
)
from wechat_shop_scraper.model import ProductCard


class MobileKeywordParserTests(unittest.TestCase):
    def test_parses_multiline_keyword_queue_and_deduplicates(self) -> None:
        self.assertEqual(
            parse_keyword_list("老爹鞋男\n男士开车专用鞋；老爹鞋男，羊毛阔腿裤"),
            ["老爹鞋男", "男士开车专用鞋", "羊毛阔腿裤"],
        )

    def test_parses_optional_integer_price_range(self) -> None:
        self.assertEqual(parse_price_range("", ""), (None, None))
        self.assertEqual(parse_price_range(" 50 ", "500"), (50, 500))
        self.assertEqual(parse_price_range("0", ""), (0, None))

    def test_rejects_invalid_price_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "最低价只能填写整数元"):
            parse_price_range("50.5", "500")
        with self.assertRaisesRegex(ValueError, "最低价不能大于最高价"):
            parse_price_range("501", "500")

    def test_parses_configurable_wechat_restart_interval(self) -> None:
        self.assertEqual(parse_restart_interval(" 30 "), 30)
        self.assertEqual(parse_restart_interval("0"), 0)
        self.assertEqual(parse_restart_interval(""), 0)
        with self.assertRaisesRegex(ValueError, "非负整数"):
            parse_restart_interval("3.5")

    def test_automation_bundle_and_filter_state_expression_are_present(self) -> None:
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            keywords=["老爹鞋男", "老爹鞋男", "羊毛阔腿裤"],
        )

        self.assertEqual(listener.keywords, ["老爹鞋男", "羊毛阔腿裤"])
        self.assertEqual(listener._bundle.name, "mobile_search_automation.bundle.js")
        self.assertTrue(listener._bundle.is_file())
        self.assertTrue(listener._launcher_bundle.is_file())
        self.assertTrue(listener._native_ui_bundle.is_file())
        bundle = listener._bundle.read_text(encoding="utf-8")
        self.assertIn("android.app.ActivityThread$ActivityClientRecord", bundle)
        self.assertIn("function findActivity", bundle)
        self.assertIn("FinderProductAdvanceSearchServiceRecommendFilter", PAGE_STATE_EXPRESSION)
        self.assertIn("good_review_available", PAGE_STATE_EXPRESSION)
        self.assertIn("data-fcn-input-range", PAGE_STATE_EXPRESSION)
        self.assertIn("keyboard_digits", PAGE_STATE_EXPRESSION)

    def test_rejects_reversed_listener_price_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "最低价不能大于最高价"):
            MobileKeywordListener(
                "",
                lambda _cards: None,
                min_price=501,
                max_price=500,
            )

    def test_selects_bundled_arm64_frida_server(self) -> None:
        asset = _frida_server_asset_for_abi(" ARM64-V8A ")

        self.assertEqual(asset.name, "frida-server-17.17.0-android-arm64")
        self.assertTrue(asset.is_file())

    def test_rejects_unsupported_frida_server_architecture(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "CPU 架构 x86_64 暂不支持"):
            _frida_server_asset_for_abi("x86_64")

    def test_one_click_setup_uploads_starts_and_verifies_server(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assets = Path(temporary)
            platform_tools = assets / "platform-tools"
            platform_tools.mkdir()
            (platform_tools / "adb.exe").write_bytes(b"adb")
            server = assets / "frida-server-17.17.0-android-arm64"
            server.write_bytes(b"frida-server")
            pid_checks = iter(["", "4567"])
            calls: list[tuple[str, ...]] = []

            def run_adb(_adb: Path, *arguments: str, timeout: float = 15) -> str:
                calls.append(arguments)
                if arguments == ("devices",):
                    return "List of devices attached\nSERIAL device"
                if arguments[-2:] == ("getprop", "ro.product.model"):
                    return "Test Phone"
                if arguments[-2:] == ("getprop", "ro.product.cpu.abi"):
                    return "arm64-v8a"
                command = arguments[-1]
                if command == "id":
                    return "uid=0(root) gid=0(root)"
                if command.startswith("ls -l "):
                    raise RuntimeError("not found")
                if command.startswith("pidof "):
                    return next(pid_checks)
                return ""

            with (
                patch("wechat_shop_scraper.mobile_keyword._asset_dir", return_value=assets),
                patch("wechat_shop_scraper.mobile_keyword._run_adb_binary", run_adb),
                patch("wechat_shop_scraper.mobile_keyword._verify_remote_frida") as verify,
                patch("wechat_shop_scraper.mobile_keyword.time.sleep"),
            ):
                result = setup_frida_server_via_usb()

            self.assertIn("Test Phone", result)
            self.assertTrue(any("push" in arguments for arguments in calls))
            self.assertTrue(any(arguments[-1].startswith("mv -f ") for arguments in calls))
            self.assertTrue(any(arguments[-1].startswith("nohup ") for arguments in calls))
            verify.assert_called_once()

    def test_reuses_running_frida_server(self) -> None:
        messages: list[str] = []
        listener = MobileKeywordListener("", lambda _cards: None, log=messages.append)
        calls: list[tuple[str, ...]] = []

        def run_adb(*arguments: str, timeout: float = 15) -> str:
            calls.append(arguments)
            return "30159"

        listener._run_adb = run_adb  # type: ignore[method-assign]
        listener._ensure_frida_server()

        self.assertEqual(len(calls), 1)
        self.assertIn("已在运行", messages[0])

    def test_starts_existing_frida_server_with_root(self) -> None:
        messages: list[str] = []
        listener = MobileKeywordListener("", lambda _cards: None, log=messages.append)
        pid_checks = iter(["", "30160"])
        calls: list[tuple[str, ...]] = []

        def run_adb(*arguments: str, timeout: float = 15) -> str:
            calls.append(arguments)
            command = arguments[-1]
            if command.startswith("pidof "):
                return next(pid_checks)
            if command == "id":
                return "uid=0(root) gid=0(root)"
            if command.startswith("ls -l "):
                return (
                    "-rwxr-xr-x 1 shell shell 53539200 "
                    "/data/local/tmp/frida-server-17.17.0"
                )
            return ""

        listener._run_adb = run_adb  # type: ignore[method-assign]
        with patch("wechat_shop_scraper.mobile_keyword.time.sleep"):
            listener._ensure_frida_server()

        commands = [arguments[-1] for arguments in calls]
        self.assertTrue(any(command.startswith("chmod 755 ") for command in commands))
        self.assertTrue(any(command.startswith("nohup ") for command in commands))
        self.assertTrue(any("自动启动成功" in message for message in messages))

    def test_reports_missing_frida_server_file(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)

        def run_adb(*arguments: str, timeout: float = 15) -> str:
            command = arguments[-1]
            if command.startswith("pidof "):
                return ""
            if command == "id":
                return "uid=0(root) gid=0(root)"
            return ""

        listener._run_adb = run_adb  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "手机缺少 Frida Server 17.17.0"):
            listener._ensure_frida_server()

    def test_reads_keyword_from_mobile_runtime_state(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        listener._call = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "result": {
                "value": {
                    "keyword": " 马丁靴男皮鞋 ",
                    "search_id": "search-123",
                }
            }
        }

        self.assertEqual(
            listener._read_keyword(),
            ("马丁靴男皮鞋", "search-123"),
        )

    def test_keyword_must_stay_stable_and_have_products(self) -> None:
        listener = MobileKeywordListener(
            "", lambda _cards: None, keyword_stable_seconds=2.0
        )

        self.assertFalse(listener._keyword_is_committed("男", "search-1", False, 0.0))
        self.assertFalse(
            listener._keyword_is_committed("男士开车", "search-2", True, 3.0)
        )
        self.assertFalse(
            listener._keyword_is_committed("男士开车", "search-2", True, 4.9)
        )
        self.assertTrue(
            listener._keyword_is_committed("男士开车", "search-2", True, 5.0)
        )

    def test_normalizes_wifi_address_and_default_port(self) -> None:
        self.assertEqual(normalize_wifi_address("192.168.8.23"), "192.168.8.23:5555")
        self.assertEqual(
            normalize_wifi_address("192.168.8.23：39821"),
            "192.168.8.23:39821",
        )

    def test_extracts_usb_device_and_wifi_route_address(self) -> None:
        output = (
            "List of devices attached\n"
            "ZY32MGJ8WQ device product:mumba model:XT2537_4\n"
            "192.168.8.23:5555 device product:mumba model:XT2537_4\n"
        )
        self.assertEqual(_usb_serials(output), ["ZY32MGJ8WQ"])
        self.assertEqual(
            _wifi_ip_from_route(
                "192.168.8.0/24 dev wlan0 proto kernel scope link src 192.168.8.23"
            ),
            "192.168.8.23",
        )

    def test_prefers_wlan_route_when_mobile_data_route_comes_first(self) -> None:
        route = (
            "10.34.251.184/30 dev rmnet_data2 proto kernel scope link "
            "src 10.34.251.186\n"
            "192.168.1.0/24 dev wlan0 proto kernel scope link "
            "src 192.168.1.230\n"
        )

        self.assertEqual(_wifi_ip_from_route(route), "192.168.1.230")

    def test_builds_keyword_product_and_excludes_live_cards(self) -> None:
        rows = [
            {
                "product_id": "10000157138013",
                "title": "马丁靴男鞋",
                "main_image_url": "https://wst.wxapp.tc.qq.com/main.jpg?imageView2/2/w/540",
                "price": "¥29.9",
                "sales_text": "已售2.9万+",
                "sales_number": "2.9",
                "sales_unit": "万",
                "shop_name": "亿承男鞋",
                "kind": "商品",
            },
            {
                "product_id": "10001327680631",
                "title": "直播间马丁靴",
                "main_image_url": "https://wst.wxapp.tc.qq.com/live.jpg",
                "price": "¥238",
                "sales_text": "已售14",
                "sales_number": "14",
                "sales_unit": "",
                "shop_name": "直播店",
                "kind": "直播商品",
            },
        ]

        cards = cards_from_dom(rows, "马丁靴男皮鞋")

        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card.product_id, "10000157138013")
        self.assertEqual(card.keyword, "马丁靴男皮鞋")
        self.assertEqual(card.shop_name, "亿承男鞋")
        self.assertEqual(card.price_fen, 2990)
        self.assertEqual(card.sales, 29000)
        self.assertFalse(card.sales_exact)
        self.assertEqual(card.source, "安卓微信关键词搜索")

    def test_missing_sales_wording_stays_unknown(self) -> None:
        cards = cards_from_dom(
            [
                {
                    "product_id": "10000343511041",
                    "title": "无销量文案商品",
                    "main_image_url": "https://wst.wxapp.tc.qq.com/main.jpg",
                    "price": "¥47.5",
                    "sales_text": "",
                    "sales_number": "0",
                    "sales_unit": "",
                    "shop_name": "KIZN",
                    "kind": "商品",
                }
            ],
            "马丁靴男皮鞋",
        )

        self.assertIsNone(cards[0].sales)
        self.assertFalse(cards[0].sales_exact)
        self.assertEqual(cards[0].display_sales, "")

    def test_click_page_point_dispatches_pressed_and_released(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        calls: list[tuple[str, dict[str, object]]] = []
        listener._call = lambda method, params=None: calls.append(  # type: ignore[method-assign]
            (method, params or {})
        ) or {}

        listener._click_page_point({"x": 12.5, "y": 34.5}, "好评多")

        self.assertEqual([item[1]["type"] for item in calls], [
            "mousePressed",
            "mouseReleased",
        ])
        self.assertTrue(all(item[0] == "Input.dispatchMouseEvent" for item in calls))

    def test_debug_forward_chooses_another_port_when_preferred_is_busy(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            busy_port = int(occupied.getsockname()[1])
            listener = MobileKeywordListener(
                "", lambda _cards: None, port=busy_port
            )
            listener._wechat_pid = 12345
            calls: list[tuple[str, ...]] = []

            def run_adb(*arguments: str, timeout: float = 15) -> str:
                calls.append(arguments)
                if arguments == ("shell", "cat", "/proc/net/unix"):
                    return (
                        "0000000000000000: 00000002 00000000 00010000 "
                        "0001 01 123456 "
                        "@webview_devtools_remote_xweb_12345"
                    )
                if f"tcp:{busy_port}" in arguments:
                    raise RuntimeError("cannot bind listener")
                return ""

            listener._run_adb = run_adb  # type: ignore[method-assign]
            with patch(
                "wechat_shop_scraper.mobile_keyword.urllib.request.urlopen",
                return_value=io.BytesIO(b"[]"),
            ):
                listener._forward_debug_socket()

        self.assertNotEqual(listener.port, busy_port)
        self.assertTrue(any(f"tcp:{listener.port}" in call for call in calls))

    def test_debug_forward_uses_standard_socket_when_xweb_socket_is_absent(
        self,
    ) -> None:
        listener = MobileKeywordListener("", lambda _cards: None, port=61234)
        listener._wechat_pid = 19647
        calls: list[tuple[str, ...]] = []
        messages: list[str] = []
        listener.log = messages.append

        def run_adb(*arguments: str, timeout: float = 15) -> str:
            calls.append(arguments)
            if arguments == ("shell", "cat", "/proc/net/unix"):
                return (
                    "0000000000000000: 00000002 00000000 00010000 "
                    "0001 01 256441 @webview_devtools_remote_19647"
                )
            return ""

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                pass

            def read(self) -> bytes:
                return b"[]"

        listener._run_adb = run_adb  # type: ignore[method-assign]
        with patch(
            "wechat_shop_scraper.mobile_keyword.urllib.request.urlopen",
            return_value=FakeResponse(),
        ):
            listener._forward_debug_socket()

        self.assertTrue(any(
            "localabstract:webview_devtools_remote_19647" in call
            for call in calls
        ))
        self.assertFalse(any(
            "localabstract:webview_devtools_remote_xweb_19647" in call
            for call in calls
        ))
        self.assertTrue(any("标准 WebView" in message for message in messages))

    def test_good_review_filter_only_clicks_checkbox_when_unchecked(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        states = iter([
            {
                "filter_panel_open": False,
                "filter_toggle_point": {"x": 10, "y": 10},
            },
            {
                "filter_panel_open": True,
                "good_review_checked": False,
                "good_review_point": {"x": 20, "y": 20},
            },
            {
                "filter_panel_open": True,
                "good_review_checked": True,
            },
            {
                "filter_panel_open": True,
                "min_price_text": "最低价",
            },
            {
                "filter_panel_open": True,
                "max_price_text": "最高价",
            },
            {
                "filter_panel_open": True,
                "confirm_point": {"x": 30, "y": 30},
            },
            {"filter_panel_open": False},
        ])
        clicked: list[str] = []
        listener._read_page_state = lambda: next(states)  # type: ignore[method-assign]
        listener._click_page_point = (  # type: ignore[method-assign]
            lambda _point, label: clicked.append(label)
        )
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._apply_good_review_filter("老爹鞋男")

        self.assertEqual(clicked, ["筛选", "好评多", "确定"])

    def test_good_review_filter_retries_opening_panel_before_failing(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        states = iter([
            {
                "filter_panel_open": False,
                "filter_toggle_point": {"x": 10, "y": 10},
            },
            {
                "filter_panel_open": False,
                "filter_toggle_point": {"x": 10, "y": 10},
            },
            {
                "filter_panel_open": True,
                "good_review_checked": True,
            },
            {
                "filter_panel_open": True,
                "min_price_text": "最低价",
            },
            {
                "filter_panel_open": True,
                "max_price_text": "最高价",
            },
            {
                "filter_panel_open": True,
                "confirm_point": {"x": 30, "y": 30},
            },
            {"filter_panel_open": False},
        ])
        clicked: list[str] = []
        listener._read_page_state = lambda: next(states)  # type: ignore[method-assign]
        listener._click_page_point = (  # type: ignore[method-assign]
            lambda _point, label: clicked.append(label)
        )
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._apply_good_review_filter("老爹鞋男")

        self.assertEqual(clicked, ["筛选", "筛选", "确定"])

    def test_good_review_filter_returns_false_when_option_is_missing(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        states = iter([
            {
                "filter_panel_open": False,
                "filter_toggle_point": {"x": 10, "y": 10},
            },
            {
                "filter_panel_open": True,
                "good_review_available": False,
                "confirm_point": {"x": 30, "y": 30},
            },
            {"filter_panel_open": False},
        ])
        clicked: list[str] = []
        listener._read_page_state = lambda: next(states)  # type: ignore[method-assign]
        listener._click_page_point = (  # type: ignore[method-assign]
            lambda _point, label: clicked.append(label)
        )
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        applied = listener._apply_good_review_filter("三角洲卡套")

        self.assertFalse(applied)
        self.assertEqual(clicked, ["筛选", "确定"])

    def test_keyword_without_good_review_filter_skips_without_retry(self) -> None:
        finished: list[tuple[str, int, int, str]] = []
        progress: list[tuple[str, int, int, str]] = []
        attempts: dict[str, int] = {}
        waits: list[float] = []
        scrolled: list[str] = []
        errors: list[str] = []
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            keywords=["三角洲卡套", "正常关键词"],
            on_progress=lambda *event: progress.append(event),
            on_keyword_finished=lambda *event: finished.append(event),
            on_error=errors.append,
        )

        def submit(keyword: str) -> None:
            attempts[keyword] = attempts.get(keyword, 0) + 1

        listener._submit_keyword = submit  # type: ignore[method-assign]
        listener._wait_for_committed_results = (  # type: ignore[method-assign]
            lambda _keyword, **_kwargs: [ProductCard("10000123456789")]
        )
        listener._select_product_tab = lambda _keyword: None  # type: ignore[method-assign]
        listener._apply_good_review_filter = (  # type: ignore[method-assign]
            lambda keyword: keyword != "三角洲卡套"
        )
        listener._scroll_keyword_to_end = (  # type: ignore[method-assign]
            lambda keyword, **_kwargs: scrolled.append(keyword) or 1
        )
        listener._stop.wait = (  # type: ignore[method-assign]
            lambda seconds: waits.append(seconds) or False
        )

        listener._automation_loop()

        self.assertEqual(attempts, {"三角洲卡套": 1, "正常关键词": 1})
        self.assertEqual(scrolled, ["正常关键词"])
        self.assertEqual(finished[0], ("三角洲卡套", 1, 2, "无好评多，已跳过"))
        self.assertEqual(finished[1], ("正常关键词", 2, 2, "已完成"))
        self.assertFalse(any("重试" in event[3] for event in progress))
        self.assertEqual(waits, [])
        self.assertEqual(errors, [])

    def test_keyword_selects_product_tab_before_opening_filters(self) -> None:
        events: list[str] = []
        listener = MobileKeywordListener("", lambda _cards: None)
        cards = [ProductCard("10000123456789")]
        listener._submit_keyword = (  # type: ignore[method-assign]
            lambda _keyword: events.append("submit")
        )
        listener._wait_for_committed_results = (  # type: ignore[method-assign]
            lambda _keyword, **_kwargs: events.append("wait") or cards
        )
        listener._select_product_tab = (  # type: ignore[attr-defined]
            lambda _keyword: events.append("product-tab")
        )
        listener._apply_good_review_filter = (  # type: ignore[method-assign]
            lambda _keyword: events.append("filter") or True
        )
        listener._scroll_keyword_to_end = (  # type: ignore[method-assign]
            lambda _keyword, **_kwargs: events.append("scroll") or 1
        )

        listener._collect_one_keyword("鞋子", 1, 1)

        self.assertEqual(
            events,
            ["submit", "wait", "product-tab", "filter", "wait", "scroll"],
        )

    def test_keyword_locks_each_result_version_before_scrolling(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        cards = [ProductCard("10000123456789")]
        signatures = [
            ("鞋子", "initial", ("10000123456789",), 1, False),
            ("鞋子", "product-tab", ("10000123456789",), 1, False),
            ("鞋子", "filtered", ("10000123456789",), 1, False),
        ]
        previous_signatures: list[tuple[object, ...] | None] = []
        scroll_versions: list[str] = []

        def wait_for_results(
            _keyword: str,
            **kwargs: object,
        ) -> list[ProductCard]:
            previous_signatures.append(kwargs.get("previous_signature"))
            listener._last_committed_result_signature = signatures[
                len(previous_signatures) - 1
            ]
            return cards

        listener._submit_keyword = lambda _keyword: None  # type: ignore[method-assign]
        listener._wait_for_committed_results = wait_for_results  # type: ignore[method-assign]
        listener._select_product_tab = lambda _keyword: True  # type: ignore[method-assign]
        listener._apply_good_review_filter = lambda _keyword: True  # type: ignore[method-assign]
        listener._scroll_keyword_to_end = (  # type: ignore[method-assign]
            lambda _keyword, expected_search_id="": scroll_versions.append(
                expected_search_id
            )
            or 1
        )

        listener._collect_one_keyword("鞋子", 1, 1)

        self.assertEqual(
            previous_signatures,
            [None, signatures[0], signatures[1]],
        )
        self.assertEqual(scroll_versions, ["filtered"])

    def test_product_tab_click_waits_until_selected(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        states = iter(
            [
                {
                    "keyword": "鞋子",
                    "product_tab_available": True,
                    "product_tab_selected": False,
                    "product_tab_point": {"x": 150, "y": 21},
                },
                {
                    "keyword": "鞋子",
                    "product_tab_available": True,
                    "product_tab_selected": True,
                },
            ]
        )
        clicked: list[str] = []
        listener._read_page_state = lambda: next(states)  # type: ignore[method-assign]
        listener._click_page_point = (  # type: ignore[method-assign]
            lambda _point, label: clicked.append(label)
        )
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._select_product_tab("鞋子")

        self.assertEqual(clicked, ["商品"])

    def test_keyword_failure_waits_and_retries_three_times_then_moves_on(self) -> None:
        progress: list[tuple[str, int, int, str]] = []
        finished: list[tuple[str, int, int, str]] = []
        errors: list[str] = []
        completed: list[bool] = []
        waits: list[float] = []
        attempts: dict[str, int] = {}
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            keywords=["坏关键词", "正常关键词"],
            on_progress=lambda *event: progress.append(event),
            on_keyword_finished=lambda *event: finished.append(event),
            on_complete=lambda: completed.append(True),
            on_error=errors.append,
        )

        def submit(keyword: str) -> None:
            attempts[keyword] = attempts.get(keyword, 0) + 1
            if keyword == "坏关键词":
                raise RuntimeError("页面临时失败")

        listener._submit_keyword = submit  # type: ignore[method-assign]
        listener._wait_for_committed_results = (  # type: ignore[method-assign]
            lambda _keyword, **_kwargs: [ProductCard("10000123456789")]
        )
        listener._select_product_tab = lambda _keyword: None  # type: ignore[method-assign]
        listener._apply_good_review_filter = lambda _keyword: None  # type: ignore[method-assign]
        listener._scroll_keyword_to_end = (  # type: ignore[method-assign]
            lambda _keyword, **_kwargs: 1
        )
        listener._stop.wait = (  # type: ignore[method-assign]
            lambda seconds: waits.append(seconds) or False
        )

        listener._automation_loop()

        self.assertEqual(attempts, {"坏关键词": 4, "正常关键词": 1})
        self.assertEqual(waits, [60.0, 60.0, 60.0])
        self.assertEqual(finished[0], ("坏关键词", 1, 2, "失败后跳过"))
        self.assertEqual(finished[1], ("正常关键词", 2, 2, "已完成"))
        self.assertTrue(any("60 秒后重试" in event[3] for event in progress))
        self.assertEqual(errors, [])
        self.assertEqual(completed, [True])

    def test_two_consecutive_exhausted_keywords_pause_on_second(self) -> None:
        finished: list[tuple[str, int, int, str]] = []
        errors: list[str] = []
        completed: list[bool] = []
        attempts: dict[str, int] = {}
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            keywords=["坏关键词一", "坏关键词二", "不会执行"],
            on_keyword_finished=lambda *event: finished.append(event),
            on_complete=lambda: completed.append(True),
            on_error=errors.append,
        )

        def submit(keyword: str) -> None:
            attempts[keyword] = attempts.get(keyword, 0) + 1
            raise RuntimeError(f"{keyword}页面失败")

        listener._submit_keyword = submit  # type: ignore[method-assign]
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._automation_loop()

        self.assertEqual(attempts, {"坏关键词一": 4, "坏关键词二": 4})
        self.assertEqual(finished, [("坏关键词一", 1, 3, "失败后跳过")])
        self.assertEqual(completed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("连续 2 个关键词", errors[0])
        self.assertIn("坏关键词二", errors[0])

    def test_automation_resumes_from_saved_start_index(self) -> None:
        submitted: list[str] = []
        progress: list[tuple[str, int, int, str]] = []
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            keywords=["已完成关键词", "暂停关键词", "后续关键词"],
            start_index=1,
            on_progress=lambda *event: progress.append(event),
        )
        listener._submit_keyword = submitted.append  # type: ignore[method-assign]
        listener._wait_for_committed_results = (  # type: ignore[method-assign]
            lambda _keyword, **_kwargs: [ProductCard("10000123456789")]
        )
        listener._select_product_tab = lambda _keyword: None  # type: ignore[method-assign]
        listener._apply_good_review_filter = lambda _keyword: None  # type: ignore[method-assign]
        listener._scroll_keyword_to_end = (  # type: ignore[method-assign]
            lambda _keyword, **_kwargs: 1
        )

        listener._automation_loop()

        self.assertEqual(submitted, ["暂停关键词", "后续关键词"])
        self.assertEqual(progress[0][1:3], (2, 3))

    def test_page_reentry_runs_after_thirty_finished_keywords_before_next(
        self,
    ) -> None:
        keywords = [f"关键词{index}" for index in range(1, 32)]
        events: list[tuple[str, object]] = []
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            keywords=keywords,
            on_keyword_finished=lambda keyword, *_args: events.append(
                ("finished", keyword)
            ),
        )
        listener._collect_one_keyword = (  # type: ignore[method-assign]
            lambda keyword, _index, _total: "已完成"
        )
        listener._reenter_shop_search_page = (  # type: ignore[attr-defined]
            lambda next_keyword: events.append(("reenter", next_keyword))
        )

        listener._automation_loop()

        self.assertEqual(events[29], ("finished", "关键词30"))
        self.assertEqual(events[30], ("reenter", "关键词31"))
        self.assertEqual(events[31], ("finished", "关键词31"))

    def test_periodic_restart_uses_configured_keyword_interval(self) -> None:
        keywords = ["关键词1", "关键词2", "关键词3"]
        events: list[tuple[str, object]] = []
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            keywords=keywords,
            restart_keyword_interval=2,
            on_keyword_finished=lambda keyword, *_args: events.append(
                ("finished", keyword)
            ),
        )
        listener._collect_one_keyword = (  # type: ignore[method-assign]
            lambda keyword, _index, _total: "已完成"
        )
        listener._reenter_shop_search_page = (  # type: ignore[attr-defined]
            lambda next_keyword: events.append(("reenter", next_keyword))
        )

        listener._automation_loop()

        self.assertEqual(
            events,
            [
                ("finished", "关键词1"),
                ("finished", "关键词2"),
                ("reenter", "关键词3"),
                ("finished", "关键词3"),
            ],
        )

    def test_zero_restart_interval_disables_periodic_restart(self) -> None:
        restarted: list[str] = []
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            keywords=["关键词1", "关键词2"],
            restart_keyword_interval=0,
        )
        listener._collect_one_keyword = (  # type: ignore[method-assign]
            lambda keyword, _index, _total: "已完成"
        )
        listener._reenter_shop_search_page = restarted.append  # type: ignore[attr-defined]

        listener._automation_loop()

        self.assertEqual(restarted, [])

    def test_foreground_activity_normalizes_wechat_component(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        listener._run_adb = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            "topResumedActivity=ActivityRecord{abc u0 "
            "com.tencent.mm/.ui.LauncherUI t69}"
        )

        self.assertEqual(
            listener._foreground_activity(),  # type: ignore[attr-defined]
            "com.tencent.mm.ui.LauncherUI",
        )

    def test_discovers_at_most_six_wechat_users_and_starts_with_foreground(
        self,
    ) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        messages: list[str] = []
        listener.log = messages.append

        def run_adb(*args: str, **_kwargs: object) -> str:
            if args == ("shell", "pm", "list", "users"):
                return "\n".join(
                    f"UserInfo{{{user_id}:应用分身:{user_id}}} running"
                    for user_id in (0, 900, 901, 902, 903, 904, 905)
                )
            if args[:5] == ("shell", "pm", "list", "packages", "--user"):
                return "package:com.tencent.mm"
            raise AssertionError(args)

        listener._run_adb = run_adb  # type: ignore[method-assign]
        listener._foreground_activity_context = (  # type: ignore[method-assign]
            lambda: (901, "com.tencent.mm.ui.LauncherUI")
        )

        listener._discover_wechat_instances()  # type: ignore[attr-defined]

        self.assertEqual(len(listener._wechat_instances), 6)
        self.assertEqual(listener._wechat_instances[0], (901, "微信2"))
        self.assertEqual(listener._wechat_instances[-1], (900, "微信1"))
        self.assertTrue(any("最多支持 6 个" in message for message in messages))

    def test_current_wechat_pid_selects_process_for_active_android_user(
        self,
    ) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        listener._wechat_instances = [(0, "微信"), (900, "微信1")]
        listener._wechat_instance_index = 1
        listener._run_adb = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            "USER PID PPID VSZ RSS WCHAN ADDR S NAME\n"
            "u0_a364 31885 1 0 0 0 0 S com.tencent.mm\n"
            "u900_a364 27410 1 0 0 0 0 S com.tencent.mm\n"
            "u900_a364 27499 1 0 0 0 0 S com.tencent.mm:tools"
        )

        self.assertEqual(listener._current_wechat_pid(), 27410)

    def test_switch_after_last_wechat_wraps_to_first_and_retries_keyword(
        self,
    ) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        listener._wechat_instances = [
            (0, "微信"),
            (900, "微信1"),
            (901, "微信2"),
        ]
        listener._wechat_instance_index = 2
        listener._wechat_pid = 27713
        events: list[object] = []
        listener.log = lambda message: events.append(message)
        listener._drop_socket = lambda: events.append("drop")  # type: ignore[method-assign]
        listener._release_wechat_runtime = (  # type: ignore[method-assign]
            lambda remove_remote=False: events.append(("release", remove_remote))
        )
        listener._run_adb = (  # type: ignore[method-assign]
            lambda *args, **_kwargs: events.append(args) or ""
        )
        listener._restart_wechat_to_me = lambda: events.append("restart")  # type: ignore[method-assign]
        listener._open_shop_search_from_wechat_me = (  # type: ignore[method-assign]
            lambda keyword="": events.append(("open-search", keyword))
        )

        listener._switch_to_next_wechat("当前关键词")  # type: ignore[attr-defined]

        self.assertEqual(listener._wechat_user_id, 0)
        self.assertIn(
            (
                "shell",
                "am",
                "force-stop",
                "--user",
                "901",
                "com.tencent.mm",
            ),
            events,
        )
        self.assertIn("restart", events)
        self.assertIn(("open-search", "当前关键词"), events)
        self.assertTrue(
            any(
                isinstance(event, str) and "回到第一个微信" in event
                for event in events
            )
        )

    def test_search_healthcheck_distinguishes_real_empty_keyword(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        listener._wechat_instances = [(0, "微信"), (900, "微信1")]
        submitted: list[str] = []
        listener._submit_keyword = submitted.append  # type: ignore[method-assign]
        listener._wait_for_committed_results = (  # type: ignore[method-assign]
            lambda _keyword, **_kwargs: [ProductCard("10000123456789")]
        )

        listener._verify_current_wechat_search("冷门关键词")  # type: ignore[attr-defined]

        self.assertEqual(submitted, ["手机"])

    def test_search_healthcheck_marks_wechat_unavailable_when_control_is_empty(
        self,
    ) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        listener._wechat_instances = [(0, "微信"), (900, "微信1")]
        listener._submit_keyword = lambda _keyword: None  # type: ignore[method-assign]
        listener._wait_for_committed_results = (  # type: ignore[method-assign]
            lambda _keyword, **_kwargs: []
        )

        with self.assertRaises(WechatSearchUnavailable):
            listener._verify_current_wechat_search("冷门关键词")  # type: ignore[attr-defined]

    def test_unavailable_wechat_switches_and_retries_same_keyword(self) -> None:
        finished: list[tuple[str, int, int, str]] = []
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            keywords=["当前关键词"],
            on_keyword_finished=lambda *event: finished.append(event),
        )
        listener._wechat_instances = [(0, "微信"), (900, "微信1")]
        attempts = 0
        switches: list[str] = []

        def collect(_keyword: str, _index: int, _total: int) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise WechatSearchUnavailable("微信搜索自检失败")
            return "已完成"

        listener._collect_one_keyword = collect  # type: ignore[method-assign]
        listener._switch_to_next_wechat = switches.append  # type: ignore[method-assign]

        listener._automation_loop()

        self.assertEqual(attempts, 2)
        self.assertEqual(switches, ["当前关键词"])
        self.assertEqual(finished, [("当前关键词", 1, 1, "已完成")])

    def test_wait_for_activity_ignores_transient_missing_resumed_page(
        self,
    ) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        activities = iter(
            [RuntimeError("页面切换中"), "com.tencent.mm.ui.LauncherUI"]
        )

        def foreground() -> str:
            value = next(activities)
            if isinstance(value, Exception):
                raise value
            return value

        listener._foreground_activity = foreground  # type: ignore[attr-defined]
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        self.assertEqual(
            listener._wait_for_foreground_activity(  # type: ignore[attr-defined]
                {"com.tencent.mm.ui.LauncherUI"}, timeout=2
            ),
            "com.tencent.mm.ui.LauncherUI",
        )

    def test_open_shop_search_uses_scaled_search_bar_and_recovers(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        events: list[object] = []
        listener._click_shop_entry_from_wechat_me = (  # type: ignore[attr-defined]
            lambda: events.append("click-entry")
        )
        listener._wait_for_foreground_activity = (  # type: ignore[attr-defined]
            lambda expected, **_kwargs: events.append(expected) or next(iter(expected))
        )
        listener._screen_size = lambda: (1080, 2400)  # type: ignore[attr-defined]
        listener._run_adb = (  # type: ignore[method-assign]
            lambda *args, **_kwargs: events.append(args) or ""
        )
        listener._ensure_frida_server = lambda: events.append("frida-server")  # type: ignore[method-assign]
        listener._enable_webview_debug = lambda: events.append("attach")  # type: ignore[method-assign]
        listener._recover_page_connection = (  # type: ignore[attr-defined]
            lambda: events.append("recover")
        )
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._open_shop_search_from_wechat_me()  # type: ignore[attr-defined]

        self.assertEqual(events[0], "click-entry")
        self.assertIn(
            ("shell", "input", "tap", "540", "255"),
            events,
        )
        self.assertEqual(events[-1], "recover")

    def test_open_shop_search_attaches_only_after_search_activity(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        events: list[object] = []
        listener._click_shop_entry_from_wechat_me = lambda: None  # type: ignore[attr-defined]
        listener._screen_size = lambda: (1080, 2400)  # type: ignore[attr-defined]
        listener._run_adb = lambda *_args, **_kwargs: ""  # type: ignore[method-assign]
        listener._wait_for_foreground_activity = (  # type: ignore[attr-defined]
            lambda expected, **_kwargs: events.append(("activity", expected))
            or next(iter(expected))
        )
        listener._ensure_frida_server = lambda: events.append("frida-server")  # type: ignore[method-assign]
        listener._enable_webview_debug = lambda: events.append("attach")  # type: ignore[method-assign]
        listener._recover_page_connection = lambda: events.append("recover")  # type: ignore[attr-defined]
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._open_shop_search_from_wechat_me()  # type: ignore[attr-defined]

        search_ready = (
            "activity",
            {
                "com.tencent.mm.plugin.webview.ui.tools.fts."
                "MMFTSSOSHomeWebViewUI"
            },
        )
        self.assertLess(events.index(search_ready), events.index("attach"))

    def test_open_shop_search_submits_keyword_before_debug_connection(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        events: list[object] = []
        listener._click_shop_entry_from_wechat_me = lambda: None  # type: ignore[attr-defined]
        listener._wait_for_foreground_activity = (  # type: ignore[attr-defined]
            lambda expected, **_kwargs: next(iter(expected))
        )
        listener._screen_size = lambda: (1080, 2400)  # type: ignore[attr-defined]
        listener._run_adb = lambda *_args, **_kwargs: ""  # type: ignore[method-assign]
        listener._ensure_frida_server = lambda: events.append("frida-server")  # type: ignore[method-assign]
        listener._enable_webview_debug = lambda: events.append("attach")  # type: ignore[method-assign]
        listener._submit_keyword = (  # type: ignore[attr-defined]
            lambda keyword: events.append(("submit", keyword))
        )
        listener._recover_page_connection = lambda: events.append("recover")  # type: ignore[attr-defined]
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._open_shop_search_from_wechat_me("可机洗纯棉床品三件套")  # type: ignore[attr-defined]

        self.assertLess(events.index("attach"), events.index(("submit", "可机洗纯棉床品三件套")))
        self.assertLess(events.index(("submit", "可机洗纯棉床品三件套")), events.index("recover"))
        self.assertEqual(
            listener._pending_bootstrap_keyword,  # type: ignore[attr-defined]
            "可机洗纯棉床品三件套",
        )

    def test_open_shop_search_retries_keyword_until_native_activity_is_ready(
        self,
    ) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        submissions: list[str] = []
        listener._click_shop_entry_from_wechat_me = lambda: None  # type: ignore[attr-defined]
        listener._wait_for_foreground_activity = (  # type: ignore[attr-defined]
            lambda expected, **_kwargs: next(iter(expected))
        )
        listener._screen_size = lambda: (1080, 2400)  # type: ignore[attr-defined]
        listener._run_adb = lambda *_args, **_kwargs: ""  # type: ignore[method-assign]
        listener._ensure_frida_server = lambda: None  # type: ignore[method-assign]
        listener._enable_webview_debug = lambda: None  # type: ignore[method-assign]
        listener._recover_page_connection = lambda: None  # type: ignore[attr-defined]
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        def submit(keyword: str) -> None:
            submissions.append(keyword)
            if len(submissions) < 3:
                raise RuntimeError("未找到微信商品搜索页面")

        listener._submit_keyword = submit  # type: ignore[attr-defined]

        listener._open_shop_search_from_wechat_me("可机洗纯棉床品三件套")  # type: ignore[attr-defined]

        self.assertEqual(
            submissions,
            ["可机洗纯棉床品三件套"] * 3,
        )

    def test_collect_reuses_keyword_submitted_during_full_restart(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        submitted: list[str] = []
        listener._pending_bootstrap_keyword = "可机洗纯棉床品三件套"  # type: ignore[attr-defined]
        listener._submit_keyword = submitted.append  # type: ignore[attr-defined]
        listener._wait_for_committed_results = (  # type: ignore[attr-defined]
            lambda _keyword, **_kwargs: []
        )
        listener._verify_current_wechat_search = lambda _keyword: None  # type: ignore[attr-defined]

        outcome = listener._collect_one_keyword(  # type: ignore[attr-defined]
            "可机洗纯棉床品三件套",
            1,
            1,
        )

        self.assertEqual(outcome, "无商品，已跳过")
        self.assertEqual(submitted, [])

    def test_shop_reentry_selects_me_tab_before_shop_entry(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        calls: list[tuple[str, ...]] = []
        listener._screen_size = lambda: (1080, 2400)  # type: ignore[attr-defined]
        listener._ui_text_center = (  # type: ignore[attr-defined]
            lambda text, **_kwargs: (945, 2300)
            if text == "我"
            else (540, 1340)
        )
        listener._run_adb = (  # type: ignore[method-assign]
            lambda *args, **_kwargs: calls.append(args) or ""
        )
        listener._wait_for_foreground_activity = (  # type: ignore[attr-defined]
            lambda expected, **_kwargs: next(iter(expected))
        )
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._click_shop_entry_from_wechat_me()  # type: ignore[attr-defined]

        self.assertEqual(
            calls,
            [
                ("shell", "input", "tap", "945", "2300"),
                ("shell", "input", "tap", "540", "1340"),
            ],
        )

    def test_shop_entry_uses_exact_label_instead_of_adjacent_works_row(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        calls: list[tuple[str, ...]] = []
        hierarchy = """<?xml version="1.0" encoding="UTF-8"?>
        <hierarchy rotation="0">
          <node text="我" bounds="[900,2200][1080,2400]" />
          <node text="作品" bounds="[100,1200][980,1440]" />
          <node text="小店与卡包" bounds="[100,1440][980,1680]" />
        </hierarchy>"""
        listener._ui_hierarchy_xml = lambda: hierarchy  # type: ignore[attr-defined]
        listener._screen_size = lambda: (1080, 2400)  # type: ignore[attr-defined]
        listener._run_adb = (  # type: ignore[method-assign]
            lambda *args, **_kwargs: calls.append(args) or ""
        )
        listener._wait_for_foreground_activity = (  # type: ignore[attr-defined]
            lambda expected, **_kwargs: next(iter(expected))
        )
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._click_shop_entry_from_wechat_me()  # type: ignore[attr-defined]

        taps = [call for call in calls if call[:3] == ("shell", "input", "tap")]
        self.assertIn(("shell", "input", "tap", "540", "1560"), taps)
        self.assertNotIn(("shell", "input", "tap", "540", "1340"), taps)

    def test_ui_text_center_falls_back_to_wechat_process_view(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        listener._uiautomator_text_center = (  # type: ignore[attr-defined]
            lambda _text, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("系统控件树为空")
            )
        )
        listener._frida_ui_text_center = (  # type: ignore[attr-defined]
            lambda text, **_kwargs: (266, 1336)
            if text == "小店与卡包"
            else (945, 2303)
        )

        self.assertEqual(
            listener._ui_text_center("小店与卡包"),  # type: ignore[attr-defined]
            (266, 1336),
        )

    def test_shop_entry_never_uses_fixed_row_when_exact_label_is_missing(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        calls: list[tuple[str, ...]] = []
        listener._screen_size = lambda: (1080, 2400)  # type: ignore[attr-defined]

        def locate(text: str, **_kwargs: object) -> tuple[int, int]:
            if text == "我":
                return 945, 2303
            raise RuntimeError("没有识别到小店与卡包")

        listener._ui_text_center = locate  # type: ignore[attr-defined]
        listener._run_adb = (  # type: ignore[method-assign]
            lambda *args, **_kwargs: calls.append(args) or ""
        )
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "没有识别到小店与卡包"):
            listener._click_shop_entry_from_wechat_me()  # type: ignore[attr-defined]

        taps = [call for call in calls if call[:3] == ("shell", "input", "tap")]
        self.assertEqual(taps, [("shell", "input", "tap", "945", "2303")])

    def test_shop_entry_retries_while_wechat_cold_start_finishes(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        calls: list[tuple[str, ...]] = []
        shop_attempts = 0
        listener._screen_size = lambda: (1080, 2400)  # type: ignore[attr-defined]
        listener._ui_text_center = (  # type: ignore[attr-defined]
            lambda text, **_kwargs: (945, 2300)
            if text == "我"
            else (540, 1340)
        )
        listener._run_adb = (  # type: ignore[method-assign]
            lambda *args, **_kwargs: calls.append(args) or ""
        )

        def wait_for_shop(expected: set[str], **_kwargs: object) -> str:
            nonlocal shop_attempts
            shop_attempts += 1
            if shop_attempts < 3:
                raise RuntimeError("入口尚未加载")
            return next(iter(expected))

        listener._wait_for_foreground_activity = wait_for_shop  # type: ignore[attr-defined]
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._click_shop_entry_from_wechat_me()  # type: ignore[attr-defined]

        taps = [call for call in calls if call[:3] == ("shell", "input", "tap")]
        self.assertEqual(
            taps,
            [
                ("shell", "input", "tap", "945", "2300"),
                ("shell", "input", "tap", "540", "1340"),
                ("shell", "input", "tap", "540", "1340"),
                ("shell", "input", "tap", "540", "1340"),
            ],
        )

    def test_shop_entry_accepts_wechat_appbrand_activity_variant(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        expected_activities: list[set[str]] = []
        listener._screen_size = lambda: (1080, 2400)  # type: ignore[attr-defined]
        listener._ui_text_center = (  # type: ignore[attr-defined]
            lambda text, **_kwargs: (945, 2300)
            if text == "我"
            else (540, 1340)
        )
        listener._run_adb = lambda *_args, **_kwargs: ""  # type: ignore[method-assign]
        listener._wait_for_foreground_activity = (  # type: ignore[attr-defined]
            lambda expected, **_kwargs: expected_activities.append(expected)
            or "com.tencent.mm.plugin.appbrand.ui.AppBrandPluginUI1"
        )
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._click_shop_entry_from_wechat_me()  # type: ignore[attr-defined]

        self.assertIn(
            "com.tencent.mm.plugin.appbrand.ui.AppBrandPluginUI1",
            expected_activities[0],
        )

    def test_open_shop_search_waits_for_new_debug_target(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        attempts: list[int] = []
        waits: list[float] = []
        listener._click_shop_entry_from_wechat_me = lambda: None  # type: ignore[attr-defined]
        listener._wait_for_foreground_activity = (  # type: ignore[attr-defined]
            lambda expected, **_kwargs: next(iter(expected))
        )
        listener._screen_size = lambda: (1080, 2400)  # type: ignore[attr-defined]
        listener._run_adb = lambda *_args, **_kwargs: ""  # type: ignore[method-assign]
        listener._ensure_frida_server = lambda: None  # type: ignore[method-assign]
        listener._enable_webview_debug = lambda: None  # type: ignore[method-assign]

        def recover() -> None:
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                raise RuntimeError("调试页尚未出现")

        listener._recover_page_connection = recover  # type: ignore[attr-defined]
        listener._stop.wait = (  # type: ignore[method-assign]
            lambda seconds: waits.append(seconds) or False
        )

        listener._open_shop_search_from_wechat_me()  # type: ignore[attr-defined]

        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(waits, [2.5, 3.0, 1.0, 1.0])

    def test_open_shop_search_retries_tap_while_shop_is_still_loading(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        calls: list[tuple[str, ...]] = []
        search_waits = 0
        shop_activity = "com.tencent.mm.plugin.appbrand.ui.AppBrandPluginUI"
        search_activity = (
            "com.tencent.mm.plugin.webview.ui.tools.fts."
            "MMFTSSOSHomeWebViewUI"
        )
        listener._click_shop_entry_from_wechat_me = lambda: None  # type: ignore[attr-defined]

        def wait_for_activity(expected: set[str], **_kwargs: object) -> str:
            nonlocal search_waits
            if shop_activity in expected:
                return shop_activity
            search_waits += 1
            if search_waits == 1:
                raise RuntimeError("搜索框首次点击时页面仍在加载")
            return search_activity

        listener._wait_for_foreground_activity = wait_for_activity  # type: ignore[attr-defined]
        listener._foreground_activity = lambda: shop_activity  # type: ignore[attr-defined]
        listener._screen_size = lambda: (1080, 2400)  # type: ignore[attr-defined]
        listener._run_adb = (  # type: ignore[method-assign]
            lambda *args, **_kwargs: calls.append(args) or ""
        )
        listener._ensure_frida_server = lambda: None  # type: ignore[method-assign]
        listener._enable_webview_debug = lambda: None  # type: ignore[method-assign]
        listener._recover_page_connection = lambda: None  # type: ignore[attr-defined]
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._open_shop_search_from_wechat_me()  # type: ignore[attr-defined]

        taps = [call for call in calls if call[:3] == ("shell", "input", "tap")]
        self.assertEqual(len(taps), 2)

    def test_page_reentry_retries_three_times_then_pauses(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        attempts: list[str] = []
        waits: list[float] = []
        listener._restart_wechat_to_me = (  # type: ignore[attr-defined]
            lambda: attempts.append("restart")
        )

        def fail_open(_keyword: str = "") -> None:
            attempts.append("open")
            raise RuntimeError("小店未加载")

        listener._open_shop_search_from_wechat_me = fail_open  # type: ignore[attr-defined]
        listener._stop.wait = (  # type: ignore[method-assign]
            lambda seconds: waits.append(seconds) or False
        )

        with self.assertRaisesRegex(RuntimeError, "连续 3 次失败"):
            listener._reenter_shop_search_page("下一个词")  # type: ignore[attr-defined]

        self.assertEqual(attempts, ["restart", "open"] * 3)
        self.assertEqual(waits, [5.0, 5.0])

    def test_periodic_reentry_fully_restarts_wechat_before_reopening(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        events: list[str] = []
        listener._exit_shop_to_wechat_me = (  # type: ignore[attr-defined]
            lambda: events.append("page-exit")
        )
        listener._restart_wechat_to_me = (  # type: ignore[attr-defined]
            lambda: events.append("process-restart")
        )
        listener._open_shop_search_from_wechat_me = (  # type: ignore[attr-defined]
            lambda keyword="": events.append(f"open-search:{keyword}")
        )

        listener._reenter_shop_search_page("下一个关键词")  # type: ignore[attr-defined]

        self.assertEqual(
            events,
            ["process-restart", "open-search:下一个关键词"],
        )

    def test_full_wechat_restart_releases_runtime_without_launcher_attach(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        listener._wechat_pid = 101
        events: list[object] = []
        listener._drop_socket = lambda: events.append("drop-socket")  # type: ignore[method-assign]
        listener._release_wechat_runtime = (  # type: ignore[method-assign]
            lambda remove_remote=False: events.append(("release", remove_remote))
        )
        listener._run_adb = (  # type: ignore[method-assign]
            lambda *args, **_kwargs: events.append(args) or ""
        )
        listener._wait_for_foreground_activity = (  # type: ignore[attr-defined]
            lambda expected, **_kwargs: events.append(("activity", expected))
            or next(iter(expected))
        )
        listener._current_wechat_pid = lambda: 202  # type: ignore[method-assign]
        listener._ensure_frida_server = lambda: events.append("frida-server")  # type: ignore[method-assign]
        listener._enable_webview_debug = (  # type: ignore[method-assign]
            lambda pid: events.append(("attach", pid))
        )
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._restart_wechat_to_me()  # type: ignore[attr-defined]

        force_stop = (
            "shell",
            "am",
            "force-stop",
            "--user",
            "0",
            "com.tencent.mm",
        )
        start = (
            "shell",
            "am",
            "start",
            "--user",
            "0",
            "-n",
            "com.tencent.mm/.ui.LauncherUI",
        )
        self.assertLess(events.index(force_stop), events.index(start))
        self.assertIn(("release", True), events)
        self.assertNotIn(("attach", 202), events)
        self.assertNotIn("frida-server", events)

    def test_full_wechat_restart_waits_for_cold_start_before_navigation(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        events: list[object] = []
        listener._drop_socket = lambda: None  # type: ignore[method-assign]
        listener._release_wechat_runtime = lambda **_kwargs: None  # type: ignore[method-assign]
        listener._run_adb = (  # type: ignore[method-assign]
            lambda *args, **_kwargs: events.append(args) or ""
        )
        listener._wait_for_foreground_activity = (  # type: ignore[attr-defined]
            lambda expected, **_kwargs: events.append("launcher-ready")
            or next(iter(expected))
        )
        listener._current_wechat_pid = lambda: 202  # type: ignore[method-assign]
        listener._ensure_frida_server = lambda: events.append("frida-server")  # type: ignore[method-assign]
        listener._enable_webview_debug = (  # type: ignore[method-assign]
            lambda pid: events.append(("attach", pid))
        )
        listener._stop.wait = (  # type: ignore[method-assign]
            lambda seconds: events.append(("wait", seconds)) or False
        )

        listener._restart_wechat_to_me()  # type: ignore[attr-defined]

        self.assertLess(events.index("launcher-ready"), events.index(("wait", 8.0)))
        self.assertNotIn(("attach", 202), events)

    def test_clone_restart_uses_default_launcher_instead_of_shell_start(
        self,
    ) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        listener._wechat_instances = [(0, "微信"), (900, "微信1")]
        listener._wechat_instance_index = 1
        events: list[object] = []
        listener._drop_socket = lambda: None  # type: ignore[method-assign]
        listener._release_wechat_runtime = lambda **_kwargs: None  # type: ignore[method-assign]
        listener._run_adb = (  # type: ignore[method-assign]
            lambda *args, **_kwargs: events.append(args) or ""
        )
        listener._launch_wechat_user_via_launcher = (  # type: ignore[method-assign]
            lambda user_id: events.append(("launcher-apps", user_id))
        )
        listener._wait_for_foreground_activity = (  # type: ignore[method-assign]
            lambda expected, **_kwargs: next(iter(expected))
        )
        listener._current_wechat_pid = lambda: 27410  # type: ignore[method-assign]
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._restart_wechat_to_me()  # type: ignore[attr-defined]

        self.assertIn(("launcher-apps", 900), events)
        self.assertFalse(
            any(
                isinstance(event, tuple)
                and event[:3] == ("shell", "am", "start")
                for event in events
            )
        )

    def test_price_field_clears_old_value_and_types_digits(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        states = iter([
            {
                "min_price_text": "20",
                "min_price_point": {"x": 10, "y": 10},
                "keyboard_open": False,
            },
            {
                "min_price_text": "20",
                "min_price_point": {"x": 10, "y": 10},
                "keyboard_open": False,
            },
            {
                "keyboard_open": True,
                "min_price_active": True,
                "keyboard_delete_point": {"x": 20, "y": 20},
                "keyboard_digits": {
                    "5": {"x": 30, "y": 30},
                    "0": {"x": 40, "y": 40},
                },
            },
            {
                "min_price_text": "50",
            },
        ])
        clicked: list[str] = []
        listener._read_page_state = lambda: next(states)  # type: ignore[method-assign]
        listener._evaluate = lambda _expression: True  # type: ignore[method-assign]
        listener._click_page_point = (  # type: ignore[method-assign]
            lambda _point, label: clicked.append(label)
        )
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._set_price_field("min", 50)

        self.assertEqual(
            clicked,
            [
                "最低价",
                "数字键盘删除",
                "数字键盘删除",
                "数字键盘删除",
                "数字 5",
                "数字 0",
            ],
        )

    def test_price_field_scrolls_filter_content_before_activation(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        states = iter([
            {
                "min_price_text": "最低价",
                "min_price_point": {"x": 10, "y": 10},
                "keyboard_open": False,
            },
            {
                "keyboard_open": True,
                "min_price_active": True,
                "keyboard_digits": {
                    "5": {"x": 30, "y": 30},
                    "0": {"x": 40, "y": 40},
                },
            },
            {"min_price_text": "50", "min_price_active": True},
        ])
        evaluated: list[str] = []
        listener._read_page_state = lambda: next(states)  # type: ignore[method-assign]
        listener._evaluate = (  # type: ignore[method-assign]
            lambda expression: evaluated.append(expression) or True
        )
        listener._click_page_point = lambda _point, _label: None  # type: ignore[method-assign]
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._set_price_field("min", 50)

        self.assertTrue(
            any("ui-content-wrapper-scroll" in expression for expression in evaluated)
        )

    def test_good_review_uses_dom_fallback_when_pointer_click_does_not_stick(
        self,
    ) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        states = iter([
            {
                "filter_panel_open": True,
                "good_review_available": True,
                "good_review_checked": False,
                "good_review_point": {"x": 10, "y": 10},
            },
            {
                "filter_panel_open": True,
                "good_review_available": True,
                "good_review_checked": False,
            },
            {
                "filter_panel_open": True,
                "good_review_available": True,
                "good_review_checked": True,
            },
            {"filter_panel_open": True, "min_price_text": "最低价"},
            {"filter_panel_open": True, "max_price_text": "最高价"},
            {"filter_panel_open": True},
            {"filter_panel_open": False},
        ])
        evaluated: list[str] = []
        listener._read_page_state = lambda: next(states)  # type: ignore[method-assign]
        listener._evaluate = (  # type: ignore[method-assign]
            lambda expression: evaluated.append(expression) or True
        )
        listener._click_page_point = lambda _point, _label: None  # type: ignore[method-assign]
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._apply_good_review_filter("新型手机腰包")

        self.assertTrue(
            any(
                "FinderProductAdvanceSearchServiceRecommendFilter" in expression
                and ".click()" in expression
                for expression in evaluated
            )
        )

    def test_uses_first_nonempty_page_when_xweb_marks_every_target_hidden(
        self,
    ) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        listener.port = 9223
        response = io.BytesIO(
            b'[{"id":"current","type":"page",'
            b'"webSocketDebuggerUrl":"ws://current",'
            b'"description":"{\\"visible\\":false,\\"empty\\":false}"},'
            b'{"id":"stale","type":"page",'
            b'"webSocketDebuggerUrl":"ws://stale",'
            b'"description":"{\\"visible\\":false,\\"empty\\":true}"}]'
        )

        with patch("wechat_shop_scraper.mobile_keyword.urllib.request.urlopen", return_value=response):
            target = listener._visible_target()

        self.assertEqual(target["id"], "current")

    def test_open_price_keyboard_is_not_dismissed_before_filtering(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        states = iter([
            {
                "filter_panel_open": True,
                "keyboard_open": True,
                "keyboard_confirm_point": {"x": 5, "y": 5},
                "good_review_checked": False,
                "good_review_point": {"x": 10, "y": 10},
            },
            {
                "filter_panel_open": True,
                "keyboard_open": True,
                "good_review_checked": True,
            },
            {
                "filter_panel_open": True,
                "keyboard_open": True,
                "min_price_text": "最低价",
            },
            {
                "filter_panel_open": True,
                "keyboard_open": True,
                "max_price_text": "最高价",
            },
            {
                "filter_panel_open": True,
                "confirm_point": {"x": 20, "y": 20},
            },
            {"filter_panel_open": False},
        ])
        clicked: list[str] = []
        listener._read_page_state = lambda: next(states)  # type: ignore[method-assign]
        listener._click_page_point = (  # type: ignore[method-assign]
            lambda _point, label: clicked.append(label)
        )
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._apply_good_review_filter("老爹鞋男")

        self.assertEqual(clicked, ["好评多", "确定"])

    def test_price_field_does_not_toggle_off_an_active_input(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        states = iter([
            {
                "min_price_text": "最低价",
                "min_price_active": True,
                "keyboard_open": True,
                "keyboard_digits": {
                    "5": {"x": 30, "y": 30},
                    "0": {"x": 40, "y": 40},
                },
            },
            {"min_price_text": "50", "min_price_active": True},
        ])
        clicked: list[str] = []
        listener._read_page_state = lambda: next(states)  # type: ignore[method-assign]
        listener._click_page_point = (  # type: ignore[method-assign]
            lambda _point, label: clicked.append(label)
        )
        listener._stop.wait = lambda _seconds: False  # type: ignore[method-assign]

        listener._set_price_field("min", 50)

        self.assertEqual(clicked, ["数字 5", "数字 0"])

    def test_scroll_finishes_after_bottom_is_stable_without_user_count(self) -> None:
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            keywords=["老爹鞋男"],
            poll_seconds=0,
        )
        cards = cards_from_dom(
            [{
                "product_id": "10000157138013",
                "title": "老爹鞋男",
                "main_image_url": "https://wst.wxapp.tc.qq.com/main.jpg",
                "price": "¥39.9",
                "sales_text": "已售1437",
                "sales_number": "1437",
                "sales_unit": "",
                "shop_name": "嘉途鞋业",
                "kind": "商品",
            }],
            "老爹鞋男",
        )
        listener._evaluate = lambda _expression: True  # type: ignore[method-assign]
        listener._read_cards = lambda _keyword=None: cards  # type: ignore[method-assign]
        listener._read_page_state = lambda: {  # type: ignore[method-assign]
            "keyword": "老爹鞋男",
            "scroll_height": 2400,
            "at_bottom": True,
            "explicit_end": False,
        }

        count = listener._scroll_keyword_to_end("老爹鞋男")

        self.assertEqual(count, 1)
        self.assertGreaterEqual(END_STABLE_ROUNDS, 3)

    def test_scroll_uses_faster_interval_without_shortening_bottom_guard(self) -> None:
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            keywords=["老爹鞋男"],
            poll_seconds=1.0,
        )
        waits: list[float] = []
        cards = [ProductCard("10000157138013", keyword="老爹鞋男")]
        listener._evaluate = lambda _expression: True  # type: ignore[method-assign]
        listener._read_cards = lambda _keyword=None: cards  # type: ignore[method-assign]
        listener._read_page_state = lambda: {  # type: ignore[method-assign]
            "keyword": "老爹鞋男",
            "scroll_height": 2400,
            "at_bottom": True,
            "explicit_end": False,
        }
        listener._stop.wait = (  # type: ignore[method-assign]
            lambda seconds: waits.append(seconds) or False
        )

        listener._scroll_keyword_to_end("老爹鞋男")

        self.assertTrue(waits)
        self.assertNotIn(1.0, waits)
        self.assertTrue(all(seconds <= 0.6 for seconds in waits))
        self.assertGreaterEqual(sum(waits[1:]), 3.0)

    def test_result_wait_reconnects_after_page_reload_timeout(self) -> None:
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            poll_seconds=0,
            keyword_stable_seconds=0,
        )
        cards = cards_from_dom(
            [{
                "product_id": "10000157138013",
                "title": "男士开车专用鞋",
                "main_image_url": "https://wst.wxapp.tc.qq.com/main.jpg",
                "price": "¥39.9",
                "sales_text": "已售1437",
                "sales_number": "1437",
                "sales_unit": "",
                "shop_name": "嘉途鞋业",
                "kind": "商品",
            }],
            "男士开车专用鞋",
        )
        state = {
            "keyword": "男士开车专用鞋",
            "search_id": "search-1",
            "empty_result": False,
        }
        attempts = iter([TimeoutError("Connection timed out"), state, state])

        def read_state() -> dict[str, object]:
            value = next(attempts)
            if isinstance(value, Exception):
                raise value
            return value

        listener._connect = lambda: setattr(listener, "_socket", object())  # type: ignore[method-assign]
        listener._read_page_state = read_state  # type: ignore[method-assign]
        listener._read_cards = lambda _keyword=None: cards  # type: ignore[method-assign]

        result = listener._wait_for_committed_results("男士开车专用鞋", timeout=1)

        self.assertEqual(result, cards)

    def test_filtered_wait_rejects_stable_prefilter_result(self) -> None:
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            poll_seconds=0,
            keyword_stable_seconds=0,
        )
        old_cards = [ProductCard("10000111111111", keyword="鞋子")]
        filtered_cards = [ProductCard("10000222222222", keyword="鞋子")]
        old_state = {
            "keyword": "鞋子",
            "search_id": "search-before-filter",
            "empty_result": False,
        }
        filtered_state = {
            "keyword": "鞋子",
            "search_id": "search-after-filter",
            "empty_result": False,
        }
        states = iter([old_state, old_state, filtered_state, filtered_state])
        cards = iter([old_cards, old_cards, filtered_cards, filtered_cards])
        listener._socket = object()
        listener._read_page_state = lambda: next(states)  # type: ignore[method-assign]
        listener._read_cards = lambda _keyword=None: next(cards)  # type: ignore[method-assign]
        prefilter_signature = (
            "鞋子",
            "search-before-filter",
            ("10000111111111",),
            1,
            False,
        )

        result = listener._wait_for_committed_results(
            "鞋子",
            timeout=1,
            previous_signature=prefilter_signature,
        )

        self.assertEqual(result, filtered_cards)
        self.assertEqual(
            listener._last_committed_result_signature,
            (
                "鞋子",
                "search-after-filter",
                ("10000222222222",),
                1,
                False,
            ),
        )

    def test_scroll_stops_if_filtered_result_version_changes(self) -> None:
        listener = MobileKeywordListener(
            "", lambda _cards: None, poll_seconds=0
        )
        listener._read_page_state = lambda: {  # type: ignore[method-assign]
            "keyword": "鞋子",
            "search_id": "unexpected-new-search",
            "scroll_height": 2400,
            "at_bottom": False,
            "explicit_end": False,
        }
        listener._evaluate = lambda _expression: True  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "筛选结果版本已变化"):
            listener._scroll_keyword_to_end(
                "鞋子", expected_search_id="filtered-search"
            )

    def test_result_wait_refreshes_debug_runtime_after_remote_closes(self) -> None:
        listener = MobileKeywordListener(
            "",
            lambda _cards: None,
            poll_seconds=0,
            keyword_stable_seconds=0,
        )
        listener._socket = object()
        cards = cards_from_dom(
            [{
                "product_id": "10000157138013",
                "title": "洞洞鞋女",
                "main_image_url": "https://wst.wxapp.tc.qq.com/main.jpg",
                "price": "¥59.9",
                "sales_text": "已售888",
                "sales_number": "888",
                "sales_unit": "",
                "shop_name": "测试鞋店",
                "kind": "商品",
            }],
            "洞洞鞋女",
        )
        stable = {
            "keyword": "洞洞鞋女",
            "search_id": "search-new-process",
            "empty_result": False,
        }
        attempts = iter([
            ConnectionError("Remote end closed connection without response"),
            stable,
            stable,
        ])
        recoveries: list[str] = []

        def read_state() -> dict[str, object]:
            value = next(attempts)
            if isinstance(value, Exception):
                raise value
            return value

        def recover() -> None:
            recoveries.append("recovered")
            listener._socket = object()

        listener._read_page_state = read_state  # type: ignore[method-assign]
        listener._read_cards = lambda _keyword=None: cards  # type: ignore[method-assign]
        listener._connect = lambda: setattr(  # type: ignore[method-assign]
            listener, "_socket", object()
        )
        listener._recover_page_connection = recover  # type: ignore[attr-defined]

        result = listener._wait_for_committed_results("洞洞鞋女", timeout=1)

        self.assertEqual(result, cards)
        self.assertEqual(recoveries, ["recovered"])

    def test_page_recovery_reattaches_when_wechat_pid_changed(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None, port=61234)
        listener._wechat_pid = 11111
        listener._socket = object()
        events: list[str] = []
        listener._current_wechat_pid = lambda: 22222  # type: ignore[attr-defined]
        listener._release_wechat_runtime = (  # type: ignore[attr-defined]
            lambda remove_remote=True: events.append("release")
        )

        def enable(target_pid: int | None = None) -> None:
            events.append("attach")
            listener._wechat_pid = target_pid or 22222

        listener._enable_webview_debug = enable  # type: ignore[method-assign]
        listener._run_adb = (  # type: ignore[method-assign]
            lambda *_arguments, **_kwargs: events.append("remove-forward") or ""
        )
        listener._forward_debug_socket = (  # type: ignore[method-assign]
            lambda: events.append("forward")
        )

        def connect() -> None:
            events.append("connect")
            listener._socket = object()

        listener._connect = connect  # type: ignore[method-assign]

        listener._recover_page_connection()  # type: ignore[attr-defined]

        self.assertEqual(listener._wechat_pid, 22222)
        self.assertEqual(
            events,
            ["release", "attach", "remove-forward", "forward", "connect"],
        )

    def test_page_recovery_uses_fresh_port_after_local_refusal(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None, port=61234)
        listener._wechat_pid = 22222
        listener._current_wechat_pid = lambda: 22222  # type: ignore[attr-defined]
        listener._run_adb = lambda *_args, **_kwargs: ""  # type: ignore[method-assign]
        forwarded_ports: list[int] = []

        def forward() -> None:
            forwarded_ports.append(listener.port)
            if listener.port == 0:
                listener.port = 62345

        listener._forward_debug_socket = forward  # type: ignore[method-assign]
        connect_attempts = 0

        def connect() -> None:
            nonlocal connect_attempts
            connect_attempts += 1
            if connect_attempts == 1:
                raise urllib.error.URLError(
                    ConnectionRefusedError(10061, "目标计算机积极拒绝")
                )
            listener._socket = object()

        listener._connect = connect  # type: ignore[method-assign]

        listener._recover_page_connection()  # type: ignore[attr-defined]

        self.assertEqual(forwarded_ports, [61234, 0])
        self.assertEqual(connect_attempts, 2)
        self.assertEqual(listener.port, 62345)

    def test_page_recovery_reattaches_when_same_pid_loses_xweb_socket(self) -> None:
        listener = MobileKeywordListener("", lambda _cards: None, port=61234)
        listener._wechat_pid = 22222
        listener._current_wechat_pid = lambda: 22222  # type: ignore[attr-defined]
        events: list[str] = []
        runtime_refreshed = False

        def release(remove_remote: bool = True) -> None:
            events.append("release")

        def enable(target_pid: int | None = None) -> None:
            nonlocal runtime_refreshed
            events.append("attach")
            runtime_refreshed = True
            listener._wechat_pid = target_pid or 22222

        listener._release_wechat_runtime = release  # type: ignore[method-assign]
        listener._enable_webview_debug = enable  # type: ignore[method-assign]
        listener._run_adb = (  # type: ignore[method-assign]
            lambda *_arguments, **_kwargs: events.append("remove-forward") or ""
        )

        def forward() -> None:
            events.append("forward")
            if listener.port == 0:
                listener.port = 62345

        listener._forward_debug_socket = forward  # type: ignore[method-assign]

        def connect() -> None:
            events.append("connect")
            if not runtime_refreshed:
                raise ConnectionError(
                    "Remote end closed connection without response"
                )
            listener._socket = object()

        listener._connect = connect  # type: ignore[method-assign]

        listener._recover_page_connection()  # type: ignore[attr-defined]

        self.assertEqual(listener.port, 62345)
        self.assertEqual(
            events,
            [
                "remove-forward",
                "forward",
                "connect",
                "release",
                "attach",
                "remove-forward",
                "forward",
                "connect",
            ],
        )

    def test_auto_selected_port_is_not_logged_as_port_zero_in_use(self) -> None:
        messages: list[str] = []
        listener = MobileKeywordListener(
            "", lambda _cards: None, port=0, log=messages.append
        )
        listener._run_adb = lambda *_args, **_kwargs: ""  # type: ignore[method-assign]

        with patch(
            "wechat_shop_scraper.mobile_keyword._available_local_port",
            return_value=62345,
        ):
            selected = listener._forward_to_available_local_port(
                0,
                "localabstract:webview_devtools_remote_xweb_22222",
                "微信页面连接",
            )

        self.assertEqual(selected, 62345)
        self.assertFalse(any("端口 0" in message for message in messages))

    def test_webview_debug_attaches_adb_current_pid_not_stale_frida_process(
        self,
    ) -> None:
        listener = MobileKeywordListener("", lambda _cards: None)
        listener._adb_serial = "ZY32MGJ8WQ"
        listener._current_wechat_pid = lambda: 20938  # type: ignore[method-assign]
        attached: list[int] = []
        selected_serials: list[str] = []

        class FakeScript:
            def __init__(self) -> None:
                self.callback = lambda _message, _data: None

            def on(self, _event: str, callback: object) -> None:
                self.callback = callback  # type: ignore[assignment]

            def load(self) -> None:
                self.callback(
                    {
                        "type": "send",
                        "payload": {
                            "type": "webview_debug_ready",
                            "enabled": True,
                        },
                    },
                    None,
                )

            def unload(self) -> None:
                pass

        class FakeSession:
            def create_script(self, _source: str) -> FakeScript:
                return FakeScript()

            def detach(self) -> None:
                pass

        class FakeDevice:
            def enumerate_processes(self) -> list[object]:
                return [
                    types.SimpleNamespace(pid=12292, name="com.tencent.mm"),
                    types.SimpleNamespace(pid=20938, name="com.tencent.mm"),
                ]

            def attach(self, pid: int) -> FakeSession:
                attached.append(pid)
                return FakeSession()

        fake_device = FakeDevice()
        fake_frida = types.SimpleNamespace(
            __version__="17.17.0",
            get_device_manager=lambda: object(),
            get_device=lambda serial, timeout=10: (
                selected_serials.append(serial) or fake_device
            ),
            get_usb_device=lambda timeout=10: fake_device,
        )

        with patch.dict(sys.modules, {"frida": fake_frida}):
            listener._enable_webview_debug()

        self.assertEqual(attached, [20938])
        self.assertEqual(listener._wechat_pid, 20938)
        self.assertEqual(selected_serials, ["ZY32MGJ8WQ"])


if __name__ == "__main__":
    unittest.main()
