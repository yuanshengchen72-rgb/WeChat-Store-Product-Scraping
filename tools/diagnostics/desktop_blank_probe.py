"""Single-controller live verification. No ProductStore or checkpoint writes."""
from pathlib import Path
import argparse
import json
import logging
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wechat_shop_scraper import desktop_keyword as dk


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=1)
    args = parser.parse_args()
    output = Path(__file__).resolve().parent / "blank-probe.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(output, encoding="utf-8")])
    log = logging.info
    if dk._powershell_json("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq '微信小店商品列表采集.exe' } | Select-Object ProcessId"):
        raise RuntimeError("Old collector still running; do not attach twice")
    marker = Path(__file__).resolve().parents[2] / "build/desktop-recovery-v6/微信小店商品列表采集/data/.cache/desktop-host.json"
    backend = dk.DesktopWechatBackend(log, host_state_path=marker)
    # Trace protocol boundaries, never product/page content.
    original_message = backend.cdp_bridge._handle_message
    from wechat_shop_scraper.desktop_proto import decode_debug_message
    def message(raw):
        decoded = decode_debug_message(raw)
        category = decoded.get("category")
        if category in {"setupContext", "addJsContext", "removeJsContext"}:
            log("PROTOCOL %s %s", category, backend.cdp_bridge.diagnostic_state())
        if category in {"chromeDevtools", "chromeDevtoolsResult"}:
            data = decoded.get("data") or {}
            payload = json.loads(data.get("payload") or "{}")
            if payload.get("id") is not None:
                log("PROTOCOL_REPLY outer_id=%s inner_id=%s error=%s", data.get("op_id"),
                    payload.get("id"), bool(payload.get("error")))
        return original_message(raw)
    backend.cdp_bridge._handle_message = message
    try:
        backend.start()
        log("ORIGINAL_PIDS weixin=%s wmpf=%s", backend.weixin_pid, backend.wmpf_pid)
        for cycle in range(args.cycles):
            backend.open_search_page(force=True, timeout=30)
            log("SEARCH_ROUTE_VERIFIED cycle=%s %s", cycle + 1, backend.cdp_bridge.diagnostic_state())
            counts = []
            def page(cards):
                counts.append(len(cards))
                log("PAGE cycle=%s page=%s cards=%s", cycle + 1, len(counts), len(cards))
            result = backend.collect_keyword("宿舍下铺蚊帐", 50, 2000, page)
            log("KEYWORD_COMPLETE cycle=%s pages=%s cards=%s outcome=%s", cycle + 1, len(counts), sum(counts), result)
    finally:
        log("FINAL_STATE %s", backend.cdp_bridge.diagnostic_state())
        backend.pause()
        backend.stop()
        log("CONTROLLER_RELEASED")


if __name__ == "__main__":
    main()
