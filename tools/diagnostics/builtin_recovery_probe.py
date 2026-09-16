"""Opt-in production-chain test; no formal products/checkpoints are written."""
from pathlib import Path
import argparse
import hashlib
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'.tools/native-bootstrap'))
from wechat_shop_scraper.desktop_keyword import DesktopWechatBackend, _visible_weixin_identity, SEARCH_STATE_SCRIPT
from wechat_shop_scraper.desktop_builtin import BuiltinShopOpener
import psutil


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('pid', type=int)
    parser.add_argument('--fresh-host', action='store_true')
    parser.add_argument('--collect', action='store_true')
    parser.add_argument('--entry-only', action='store_true')
    parser.add_argument('--timeout', type=int, default=150)
    args = parser.parse_args()
    if _visible_weixin_identity()[0] != args.pid:
        raise RuntimeError('Weixin identity changed')
    if any(p.info['name'] == '微信小店商品列表采集.exe' for p in psutil.process_iter(['name'])):
        raise RuntimeError('Collector must remain closed')
    batch = Path('E:/微信小店/新/微信小店关键词采集/家纺/家纺60909')
    hashes = {name: hashlib.sha256((batch / name).read_bytes()).hexdigest()
              for name in ('products.json','keyword_checkpoint.json')}
    def log(message):
        print(time.strftime('%H:%M:%S'), message, flush=True)
    backend = DesktopWechatBackend(log, normal_shop_opener=BuiltinShopOpener(),
        host_state_path=ROOT/'tools/diagnostics/builtin-probe-host.json')
    backend._shop_state_path = ROOT/'tools/diagnostics/builtin-probe-shops.json'
    timer = threading.Timer(args.timeout, backend.cancel_event.set)
    timer.daemon = True
    timer.start()
    try:
        backend.prepare_recovery_entry()
        if args.entry_only:
            backend._bootstrap_normal_shop()
            log('BUILTIN_SEARCH_RENDERED')
            return
        backend.recover_search_runtime()
        log(f'FIRST_SEARCH_CONNECTED main={backend.weixin_pid} host={backend.wmpf_pid}')
        if args.fresh_host:
            old_pid = backend.wmpf_pid
            backend.stop()
            host = psutil.Process(old_pid)
            if host.ppid() != args.pid or host.name().lower() != 'wechatappex.exe':
                raise RuntimeError('Unexpected miniapp root')
            children = host.children(recursive=True)
            if any(p.name().lower() != 'wechatappex.exe' or p.exe()!=host.exe() for p in children):
                raise RuntimeError('Unexpected miniapp descendant')
            host.terminate()
            host.wait(8)
            for child in children:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(children, timeout=5)
            log(f'TEST_HOST_REPLACEMENT old={old_pid}; Weixin preserved')
            backend.recover_search_runtime()
            if backend.wmpf_pid == old_pid:
                raise AssertionError('Miniapp host did not change')
            log(f'FRESH_HOST_SEARCH_CONNECTED main={backend.weixin_pid} host={backend.wmpf_pid}')
        backend.pause()
        backend.cancel_event.clear()
        backend.start()
        backend.open_search_page()
        log('STOP_START_SEARCH_VERIFIED')
        if args.collect:
            pages = []
            def cards(items):
                pages.append(len(items))
                log(f'TEST_PAGE {len(pages)} cards={len(items)} (memory only)')
            outcome = backend.collect_keyword('可水洗单人榻榻米床垫', 50, 2000, cards)
            if not sum(pages):
                raise AssertionError('No products collected')
            log(f'FULL_KEYWORD_COMPLETE pages={len(pages)} cards={sum(pages)} outcome={outcome}')
        assert _visible_weixin_identity()[0] == args.pid
        log('PASS_BUILTIN_RECOVERY')
    finally:
        timer.cancel()
        backend.pause()
        backend.stop()
        for name, digest in hashes.items():
            assert hashlib.sha256((batch / name).read_bytes()).hexdigest() == digest, name
        log('FORMAL_DATA_UNCHANGED CONTROLLER_RELEASED')


if __name__ == '__main__':
    main()
