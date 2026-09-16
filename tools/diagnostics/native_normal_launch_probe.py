"""Isolated normal applet launch through the verified Weixin request path.

No WMPF attachment, assistant commands, authentication changes, chat access,
process termination, keyword search or product/checkpoint storage. This remains
a diagnostic until a fresh-host launch and collection connection are verified.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import typing

from typing_extensions import NotRequired
if not hasattr(typing, 'NotRequired'):
    typing.NotRequired = NotRequired
import frida
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wechat_shop_scraper.desktop_keyword import (
    SHOP_APP_ID, SEARCH_PAGE_ROUTE, SUPPORTED_PROFILE, loaded_module_path,
    _visible_weixin_identity, _powershell_json, _wmpf_root_pid,
)

SCRIPT = r'''
'use strict';
const base = Process.getModuleByName('Weixin.dll').base;
// Verified .63: wmpf_host request default constructor (0xe8 bytes).
// The normal launch dispatcher ignores its first argument, resolves current
// services itself, and copies the const input before scheduling async work.
const construct = new NativeFunction(base.add(0x7866f0),'pointer',['pointer']);
const launch = new NativeFunction(base.add(0x220a80),'void',['pointer','pointer','pointer']);
const held = [];
function stdstring(value) {
  if(!/^[\x20-\x7e]*$/.test(value)) throw new Error('Only public ASCII route data allowed');
  const object = Memory.alloc(32);
  const data = Memory.allocUtf8String(value);
  object.writeByteArray(new Uint8Array(32));
  object.writePointer(data);
  object.add(16).writeU64(value.length);
  object.add(24).writeU64(Math.max(31,value.length));
  held.push(object,data);
  return object;
}
rpc.exports = {open(appid, route, threadId) {
  if(appid !== 'wxca8d4b8e8feedc2a') throw new Error('Only requested shop app allowed');
  if(!/^subpackages\/(?:searchCenter\/pages\/globalSearch\/globalSearch|shopWindow\/pages\/wxShopHome\/index)(?:\?[a-zA-Z0-9_%=&.-]*)?$/.test(route))
    throw new Error('Only shop/search entry routes allowed');
  const request = Memory.alloc(0xe8);
  request.writeByteArray(new Uint8Array(0xe8));
  held.push(request);
  return Process.runOnThread(Number(threadId), function() {
    construct(request);
    request.add(8).writePointer(stdstring(appid));
    request.add(24).writePointer(stdstring(route));
    request.add(0xe4).writeU32(1|4);
    // Preserve all default native fields, including normal scene 1000.
    launch(ptr(0), request, ptr(0));
  }).then(()=>({dispatched:true}));
}};
'''


def snapshot(main_pid):
    host = _wmpf_root_pid(main_pid)
    if not host:
        return {'pid': 0, 'window': False, 'shop_title': False}
    row = _powershell_json(
        f'Get-Process -Id {host} -ErrorAction SilentlyContinue | '
        'Select-Object MainWindowHandle,MainWindowTitle'
    ) or {}
    return {'pid': host, 'window': bool(row.get('MainWindowHandle')),
            'shop_title': row.get('MainWindowTitle') in {'小店','小店与卡包'}}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('pid', type=int)
    parser.add_argument('--route', default=SEARCH_PAGE_ROUTE)
    parser.add_argument('--fresh-host', action='store_true')
    args = parser.parse_args()
    process = psutil.Process(args.pid)
    if process.name().lower() != 'weixin.exe':
        raise RuntimeError('Expected Weixin.exe')
    if any(p.info['name'] == '微信小店商品列表采集.exe'
           for p in psutil.process_iter(['name'])):
        raise RuntimeError('Collector must be closed')
    main_pid, thread_id = _visible_weixin_identity()
    if main_pid != args.pid or not thread_id:
        raise RuntimeError('Main Weixin identity does not match')
    path = loaded_module_path(main_pid, 'Weixin.dll')
    if not path or hashlib.sha256(path.read_bytes()).hexdigest() != SUPPORTED_PROFILE.weixin_sha256:
        raise RuntimeError('Only exact verified .63 DLL supported')
    print('BEFORE', json.dumps(snapshot(main_pid)), flush=True)
    if args.fresh_host:
        old = snapshot(main_pid)
        if not old['window'] or not old['shop_title']:
            raise RuntimeError('Cold probe requires a confirmed shop host')
        host = psutil.Process(old['pid'])
        if host.ppid() != main_pid or host.name().lower() != 'wechatappex.exe':
            raise RuntimeError('Unexpected miniapp host identity')
        if any(arg.startswith('--type=') for arg in host.cmdline()):
            raise RuntimeError('Expected root miniapp host')
        children = host.children(recursive=True)
        if any(p.name().lower() != 'wechatappex.exe' or p.exe() != host.exe() for p in children):
            raise RuntimeError('Unexpected process in miniapp tree; nothing stopped')
        host.terminate()
        host.wait(8)
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(children, timeout=5)
        if not process.is_running():
            raise RuntimeError('Weixin main disappeared')
        print('OLD_MINIAPP_HOST_STOPPED', old['pid'], flush=True)
    session = frida.get_local_device().attach(main_pid)
    script = None
    try:
        script = session.create_script(SCRIPT)
        script.load()
        print('NORMAL_LAUNCH', json.dumps(script.exports_sync.open(SHOP_APP_ID,args.route,thread_id)), flush=True)
        end = time.monotonic() + 35
        stable = 0
        while time.monotonic() < end:
            state = snapshot(main_pid)
            stable = stable + 1 if state['window'] and state['shop_title'] else 0
            if stable >= 3:
                if args.fresh_host and state['pid'] == old['pid']:
                    raise RuntimeError('Host PID was not replaced')
                print('SHOP_WINDOW_CONFIRMED', json.dumps(state), flush=True)
                print('Page contents and fresh-host recovery must still be independently verified', flush=True)
                return 0
            time.sleep(1)
        print('FAIL: no stable shop window', flush=True)
        return 2
    finally:
        if script:
            try:
                script.unload()
            except frida.InvalidOperationError:
                pass
        session.detach()
        print('PROBE_DETACHED', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
