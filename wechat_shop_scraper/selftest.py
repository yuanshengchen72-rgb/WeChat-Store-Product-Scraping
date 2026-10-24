from __future__ import annotations

import json
import os
import sys
import tkinter
import typing
import tempfile
from pathlib import Path

from typing_extensions import NotRequired

from .app_data import prepare_frozen_tk_runtime
from .desktop_proto import decode_debug_message, wrap_connect_js_context
from .parser import ProductCardParser
from .mobile_keyword import EXPECTED_FRIDA_VERSION, _asset_dir


if not hasattr(typing, "NotRequired"):
    typing.NotRequired = NotRequired  # type: ignore[attr-defined]


def run_self_test() -> int:
    def fail(reason: str) -> int:
        report_path = os.environ.get("WECHAT_SCRAPER_SELFTEST_REPORT", "").strip()
        if report_path:
            try:
                Path(report_path).write_text(reason, encoding="utf-8")
            except OSError:
                pass
        return 1

    try:
        # The repository keeps the Windows UIAutomation/comtypes runtime in
        # .tools/native-bootstrap for source-tree tests.  Frozen builds place
        # the same modules on the bundled import path already.
        if not getattr(sys, "frozen", False):
            bootstrap = Path(__file__).resolve().parents[1] / ".tools" / "native-bootstrap"
            if bootstrap.is_dir() and str(bootstrap) not in sys.path:
                sys.path.insert(0, str(bootstrap))
        import frida  # type: ignore
        import websocket  # type: ignore
        import uiautomation
        import comtypes
        from .desktop_builtin import BuiltinShopOpener

        if not hasattr(uiautomation, "UIAutomationInitializerInThread"):
            return fail("缺少小店窗口恢复组件")
        if not hasattr(BuiltinShopOpener, 'check_entry'):
            return fail("缺少内置小店启动组件")

        if getattr(frida, "__version__", "") != EXPECTED_FRIDA_VERSION:
            return fail("frida 版本不正确或未打包")
        if not getattr(websocket, "__version__", ""):
            return fail("websocket-client 未正确打包")
        assets = _asset_dir()
        if not (assets / "mobile_search_automation.bundle.js").is_file():
            return fail("缺少手机自动化脚本")
        if not (assets / "launcher_user_switch.bundle.js").is_file():
            return fail("缺少微信分身启动脚本")
        if not (assets / "native_ui_locator.bundle.js").is_file():
            return fail("缺少微信界面定位脚本")
        if not (
            assets / f"frida-server-{EXPECTED_FRIDA_VERSION}-android-arm64"
        ).is_file():
            return fail("缺少手机端 Frida Server")
        if not (assets / "platform-tools" / "adb.exe").is_file():
            return fail("缺少内置 ADB")
        prepare_frozen_tk_runtime()
        interpreter = tkinter.Tcl()
        if not interpreter.eval("info patchlevel"):
            return fail("Tcl/Tk 运行库不可用")
        payload = {
            "product_cards": [
                {
                    "product_id": "10000123456789",
                    "title": "portable-self-test",
                    "img_urls": ["https://example.test/main.jpg"],
                    "selling_price": 100,
                    "sales": 1,
                    "platform_name": "portable-self-test-shop",
                }
            ]
        }
        cards = ProductCardParser().parse(json.dumps(payload).encode("utf-8"))
        if len(cards) != 1 or cards[0].shop_name != "portable-self-test-shop":
            return fail("商品解析器自检失败")
        desktop_fixture = wrap_connect_js_context(1, "7")
        if decode_debug_message(desktop_fixture).get("category") != "connectJsContext":
            return fail("电脑版微信采集协议自检失败")
        from .incremental_storage import IncrementalProductStore
        with tempfile.TemporaryDirectory(prefix='collector-selftest-') as directory:
            store = IncrementalProductStore(directory)
            store.upsert_many(cards)
            recovered = IncrementalProductStore(directory)
            if len(recovered) != 1 or not recovered.export_csv():
                return fail("增量保存/恢复/导出组件自检失败")
        return 0
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}")
