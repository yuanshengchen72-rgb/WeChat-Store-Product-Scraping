"""Probe the existing normal launch URI without Frida or a chat window.

Only process metadata is recorded. Never terminate a process, initialize a
native backend, collect products, or read/write the user's data directory.
"""
from pathlib import Path
import json
import sys
import time

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wechat_shop_scraper.desktop_keyword import (
    DesktopWechatBackend, DesktopWechatError, _launch_wmpf_host,
    _powershell_json, _wmpf_root_pid,
)


def snapshot(main_pid):
    host = _wmpf_root_pid(main_pid)
    if not host:
        return {'pid': 0, 'window': False, 'initialized': False}
    row = _powershell_json(
        f'Get-Process -Id {host} -ErrorAction SilentlyContinue | '
        'Select-Object Id,MainWindowHandle,MainWindowTitle'
    )
    if not isinstance(row, dict):
        row = {}
    try:
        DesktopWechatBackend()._require_initialized_host(host)
        initialized = True
    except DesktopWechatError:
        initialized = False
    return {'pid': host, 'window': bool(row.get('MainWindowHandle')),
            'shop_title': row.get('MainWindowTitle') in {'小店', '小店与卡包'},
            'initialized': initialized}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    main_pid = int(sys.argv[1])
    if psutil.Process(main_pid).name().lower() != 'weixin.exe':
        raise RuntimeError('Target is not Weixin.exe')
    if any(p.info['name'] == '微信小店商品列表采集.exe'
           for p in psutil.process_iter(['name'])):
        raise RuntimeError('Close the collector before this isolated test')
    before = snapshot(main_pid)
    print('BEFORE', json.dumps(before), flush=True)
    if before['initialized']:
        raise RuntimeError('Cannot test cold initialization on an initialized shop')
    try:
        returned_pid = _launch_wmpf_host(main_pid, timeout=8)
        print('LEGACY_RETURNED_PID', returned_pid, flush=True)
    except DesktopWechatError as exc:
        print('LAUNCH_ERROR', str(exc), flush=True)
    time.sleep(2)
    after = snapshot(main_pid)
    print('AFTER', json.dumps(after), flush=True)
    if not after['initialized']:
        print('FAIL: URI dispatch did not initialize a shop; native attach was not attempted', flush=True)
        return 1
    print('NORMAL_SHOP_INITIALIZED: search connection still requires independent verification', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
