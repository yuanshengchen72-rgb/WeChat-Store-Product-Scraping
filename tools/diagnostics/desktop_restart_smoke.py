"""Bounded live restart check. Close the collector first; no batch data is written."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wechat_shop_scraper.desktop_keyword import DesktopKeywordListener, DesktopWechatBackend
from wechat_shop_scraper import desktop_keyword


class SlowValidationBackend(DesktopWechatBackend):
    def collect_keyword(self, *args, **kwargs):
        began = time.monotonic()
        result = self._evaluate_page_async(
            "new Promise(resolve=>setTimeout(()=>resolve({ok:true,slowProbe:true}),6200))", 30,
        )
        if result != {"ok": True, "slowProbe": True}:
            raise AssertionError(f"Unexpected slow request: {result}")
        self.log(f"slow-async-probe passed in {time.monotonic()-began:.1f}s (>5000ms)")
        return super().collect_keyword(*args, **kwargs)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    # One controlled startup attempt: do not repeatedly crash a native host.
    desktop_keyword.PAGE_REENTRY_MAX_ATTEMPTS = 1
    desktop_keyword.MAX_KEYWORD_RETRIES = 0
    if "--entry-only" in sys.argv:
        marker = Path(__file__).resolve().parents[2] / "build" / "desktop-candidate-v5" / "微信小店商品列表采集" / "data" / ".cache" / "desktop-host.json"
        for entry_cycle in range(2):
            backend = DesktopWechatBackend(lambda message: print(message, flush=True), host_state_path=marker)
            try:
                backend.start()
                backend.open_search_page()
                print(f"ENTRY_READY cycle={entry_cycle+1} weixin={backend.weixin_pid} wmpf={backend.wmpf_pid}", flush=True)
            finally:
                backend.pause()
                backend.stop()
        return 0
    backend = SlowValidationBackend()
    for cycle in range(1, 3):
        done = threading.Event()
        errors = []
        page_sizes = []
        def log(message):
            print(time.strftime("%H:%M:%S"), f"cycle={cycle}", message, flush=True)
        def products(cards):
            page_sizes.append(len(cards))
            log(f"page={len(page_sizes)} cards={len(cards)}")
            if cycle == 1 and len(page_sizes) == 4:
                listener._stop.set()
                done.set()
        def failed(message):
            errors.append(message)
            done.set()
        listener = DesktopKeywordListener(
            keyword="", keywords=["可水洗单人榻榻米床垫"],
            min_price=50, max_price=2000, on_products=products,
            log=log, on_complete=done.set, on_error=failed,
            restart_keyword_interval=0, retry_seconds=2,
            backend=backend,
        )
        backend.log = log
        backend.cdp_bridge.log = log
        def start():
            try:
                listener.start()
            except Exception as exc:
                failed(str(exc))
        starter = threading.Thread(target=start, daemon=True)
        starter.start()
        try:
            if not done.wait(150):
                errors.append("live test timed out")
            log(f"RESULT pages={page_sizes} errors={errors}")
        finally:
            listener.stop()
            starter.join()
            listener.stop()
        if errors:
            backend.stop()
            return 1
    log(f"FINAL_RELEASE weixin={backend.weixin_pid} wmpf={backend.wmpf_pid}")
    backend.stop()
    log("FINAL_RELEASE_RETURNED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
