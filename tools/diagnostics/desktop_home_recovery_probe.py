"""Opt-in single-controller live test. Never writes the user's batch."""
from pathlib import Path
import hashlib
import json
import logging
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from wechat_shop_scraper import desktop_keyword as dk


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(
                            Path(__file__).with_suffix('.log'), encoding='utf-8')])
    log = logging.info
    if dk._powershell_json("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq '微信小店商品列表采集.exe' } | Select-Object ProcessId"):
        raise RuntimeError('Collector still running; refusing concurrent attachment')
    batch = Path('E:/微信小店/新/微信小店关键词采集/家纺/家纺60909')
    hashes = {name: hashlib.sha256((batch / name).read_bytes()).hexdigest()
              for name in ('products.json', 'keyword_checkpoint.json')}
    backend = dk.DesktopWechatBackend(log, host_state_path=ROOT / 'build/desktop-recovery-v7/微信小店商品列表采集/data/.cache/desktop-host.json')
    # Diagnostic cache remains separate from both the formal batch and v7.
    backend._shop_state_path = ROOT / 'tools/diagnostics/desktop-home-recovery-shops.json'
    if '--home-only' in sys.argv:
        backend._remember_recovery_shops(json.loads(backend._shop_state_path.read_text(encoding='utf-8'))[:1])
    real_wait = backend.cdp_bridge.wait_runtime_refresh
    launches = []
    real_launch = backend._launch_applet

    def launch(timeout=25.0, **kwargs):
        launches.append(kwargs.get('entry_url', dk.SEARCH_PAGE_ROUTE))
        return real_launch(timeout, **kwargs)

    backend._launch_applet = launch
    try:
        backend.start()
        log('ORIGINAL_PIDS main=%s host=%s', backend.weixin_pid, backend.wmpf_pid)
        if '--home-only' in sys.argv:
            backend._restore_shop_home(timeout=15)
            backend._navigate_search_page(15)
            log('HOME_AND_SEARCH_VERIFIED')
            return
        pages = []
        def page(cards):
            pages.append(len(cards))
            log('PAGE %s cards=%s', len(pages), len(cards))
        backend.collect_keyword('母婴加厚保暖棉被芯', 50, 2000, page)
        if not backend._recovery_shops:
            raise AssertionError('Search did not return usable public shop IDs')
        log('BASELINE_ALL_PAGES pages=%s cards=%s recovery_shops=%s', len(pages), sum(pages), len(backend._recovery_shops))
        pending_failure = True
        def fail_once(*args, **kwargs):
            nonlocal pending_failure
            ready = real_wait(*args, **kwargs)
            if pending_failure:
                pending_failure = False
                log('SIMULATED_SEARCH_NOT_READY actual_ready=%s', ready)
                return False
            return ready
        backend.cdp_bridge.wait_runtime_refresh = fail_once
        backend.recover_search_runtime()
        assert any(route.startswith(dk.SHOP_HOME_ROUTE + '?') for route in launches)
        log('HOME_AND_SEARCH_VERIFIED')
        pages.clear()
        backend.collect_keyword('母婴加厚保暖棉被芯', 50, 2000, page)
        log('SAME_WORD_COMPLETE pages=%s cards=%s', len(pages), sum(pages))
    finally:
        backend.pause()
        backend.stop()
        for name, digest in hashes.items():
            assert hashlib.sha256((batch / name).read_bytes()).hexdigest() == digest, name
        log('FORMAL_DATA_UNCHANGED CONTROLLER_RELEASED')


if __name__ == '__main__':
    main()
