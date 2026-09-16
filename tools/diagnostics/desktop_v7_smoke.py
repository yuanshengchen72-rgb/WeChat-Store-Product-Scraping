"""Run v7 lifecycle checks using the existing bounded, no-storage harness."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wechat_shop_scraper import desktop_keyword as dk
from desktop_v6_smoke import ProbeBackend, run_cycle, log


class V7ProbeBackend(ProbeBackend):
    def __init__(self):
        super().__init__()
        self.host_state_path = (Path(__file__).resolve().parents[2] /
            "build/desktop-recovery-v6/微信小店商品列表采集/data/.cache/desktop-host.json")

    def _launch_applet(self, *args, **kwargs):
        before = self.cdp_bridge._runtime_epoch
        dk.DesktopWechatBackend._launch_applet(self, *args, **kwargs)
        if self.cdp_bridge._runtime_epoch <= before or not self.cdp_bridge.runtime_ready:
            raise AssertionError("relaunch did not verify a fresh page epoch")
        self.launches += 1
        log("NEW_PAGE_VERIFIED " + self.cdp_bridge.diagnostic_state())


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if dk._powershell_json("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq '微信小店商品列表采集.exe' } | Select-Object ProcessId"):
        raise RuntimeError("Close the old collector before live verification")
    dk.PAGE_REENTRY_MAX_ATTEMPTS = 1
    dk.MAX_KEYWORD_RETRIES = 1
    backend = V7ProbeBackend()
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
