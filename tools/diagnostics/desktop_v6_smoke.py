"""Explicitly authorised live test; callbacks never write the user's batch."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wechat_shop_scraper import desktop_keyword as dk


def log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)


class ProbeBackend(dk.DesktopWechatBackend):
    def __init__(self):
        super().__init__(log)
        self.phase = "pause"
        self.pending_job = threading.Event()
        self.awaiting_cancel = False
        self.injected_failure = False
        self.recoveries = 0
        self.restarts = 0
        self.launches = 0

    def evaluate(self, script, *args, **kwargs):
        result = super().evaluate(script, *args, **kwargs)
        if self.awaiting_cancel and "jobStarted" in script:
            self.pending_job.set()
            log("PENDING_PAGE_JOB_STARTED")
        return result

    def _read_search_page(self, script, page_number):
        if self.phase == "pause" and page_number == 3:
            self.awaiting_cancel = True
            # Keep a real JS job in flight; stop before it can return/commit.
            self._evaluate_page_async(
                "new Promise(resolve=>setTimeout(()=>resolve({ok:true}),30000))", 40
            )
            raise AssertionError("pause test failed to cancel the pending job")
        if self.phase == "recover" and page_number == 3 and not self.injected_failure:
            self.injected_failure = True
            log("INJECT_SINGLE_CDP_TIMEOUT_AT_PAGE_3")
            raise dk.DesktopWechatError("页面调试命令超时：Runtime.evaluate（受控故障注入）")
        return super()._read_search_page(script, page_number)

    def _launch_applet(self, *args, **kwargs):
        before = self.cdp_bridge.connection_generation
        super()._launch_applet(*args, **kwargs)
        after = self.cdp_bridge.connection_generation
        if after <= before:
            raise AssertionError("relaunch reused old debug connection")
        self.launches += 1
        log(f"NEW_PAGE_CONNECTION {before}->{after}")

    def recover_search_runtime(self):
        self.recoveries += 1
        return super().recover_search_runtime()

    def restart_search_runtime(self):
        self.restarts += 1
        return super().restart_search_runtime()


def check_health(backend):
    if not backend.weixin_pid or not backend.wmpf_pid:
        return True
    rows = dk._powershell_json(
        f"Get-Process -Id {backend.weixin_pid},{backend.wmpf_pid} -ErrorAction SilentlyContinue | "
        "Select-Object Id,Responding"
    )
    if isinstance(rows, dict):
        rows = [rows]
    return isinstance(rows, list) and len(rows) == 2 and all(row.get("Responding") for row in rows)


def run_cycle(backend, phase, keywords):
    backend.phase = phase
    backend.awaiting_cancel = False
    done = threading.Event()
    errors, finished, counts = [], [], []

    def failed(message):
        errors.append(message)
        done.set()

    def products(cards):
        counts.append(len(cards))
        log(f"READ_PAGE phase={phase} batch={len(counts)} cards={len(cards)}")

    listener = dk.DesktopKeywordListener(
        keyword="", keywords=keywords, backend=backend, log=log,
        min_price=50, max_price=2000, on_products=products,
        on_error=failed, on_complete=done.set,
        on_keyword_finished=lambda *args: finished.append(args),
        restart_keyword_interval=1 if len(keywords) > 1 else 0,
        retry_seconds=2,
    )

    def start():
        try:
            listener.start()
        except Exception as exc:
            failed(f"{type(exc).__name__}: {exc}")

    starter = threading.Thread(target=start, daemon=True)
    starter.start()
    deadline, bad_health = time.monotonic() + 240, 0
    next_health = time.monotonic() + 3
    while not done.wait(0.1):
        if phase == "pause" and backend.pending_job.is_set():
            break
        if time.monotonic() > deadline:
            failed("bounded test exceeded 240s")
            break
        if time.monotonic() >= next_health:
            next_health = time.monotonic() + 3
            bad_health = 0 if check_health(backend) else bad_health + 1
            if bad_health >= 3:
                failed("WeChat/mini-app stopped responding; no further retries")
                break
    began = time.monotonic()
    listener.stop()
    starter.join()
    listener.stop()
    log(f"STOP_RETURNED phase={phase} seconds={time.monotonic()-began:.2f}")
    log(f"CYCLE_RESULT {json.dumps({'phase': phase, 'counts': counts, 'finished': finished, 'errors': errors}, ensure_ascii=False)}")
    if errors:
        raise RuntimeError(errors[0])
    if phase == "pause":
        if not backend.pending_job.is_set() or finished or len(counts) != 2:
            raise AssertionError("in-flight pause scenario was not exercised")
    elif len(finished) != len(keywords) or not counts:
        raise AssertionError("keyword did not reach an actual final page")
    if not check_health(backend):
        raise RuntimeError("original WeChat process no longer responds")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    # No second collector may still be running, even if it is idle.
    collectors = dk._powershell_json(
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq '微信小店商品列表采集.exe' } | Select-Object ProcessId"
    )
    if collectors:
        raise RuntimeError("Close the old collector before live verification")
    dk.PAGE_REENTRY_MAX_ATTEMPTS = 1
    dk.MAX_KEYWORD_RETRIES = 1
    backend = ProbeBackend()
    try:
        run_cycle(backend, "pause", ["桑蚕丝云片枕"])
        run_cycle(backend, "resume", ["桑蚕丝云片枕"])
        run_cycle(backend, "recover", ["桑蚕丝云片枕", "小清新保暖四件套"])
        if backend.recoveries != 1 or backend.restarts != 1:
            raise AssertionError("automatic fault/timed relaunch was not exercised")
        log(f"SCENARIOS_PASSED launches={backend.launches} recoveries={backend.recoveries} timed={backend.restarts}")
    finally:
        backend.pause()
        log(f"FINAL_RELEASE weixin={backend.weixin_pid} wmpf={backend.wmpf_pid}")
        backend.stop()
        log("FINAL_RELEASE_RETURNED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
