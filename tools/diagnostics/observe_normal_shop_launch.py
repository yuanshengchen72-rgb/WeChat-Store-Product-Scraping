"""Bounded human-in-the-loop launch metadata probe; never launches an app.

Windows adaptation of diagnosing-bugs' HITL loop: emit READY, have the user
close/open the designated shop card, then inspect only public launch metadata.
No native calls, argument rewriting, chat/database reads or credential output.
"""
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
from wechat_shop_scraper.desktop_keyword import loaded_module_path, SUPPORTED_PROFILE

SCRIPT = r'''
'use strict';
const module = Process.getModuleByName('Weixin.dll');
let events = 0;
const hook = Interceptor.attach(module.base.add(0x7b51b0), {onEnter(args) {
  try {
    const name = args[1].readUtf8String();
    if (!name || !/^wechat_[a-z_]{1,120}$/.test(name) ||
        !/(xweb|applet|wxa|shop)/.test(name) || events >= 80) return;
    const size = this.context.rsp.add(40).readU32();
    const output = {event:'launch-metadata', command:name, size:size};
    if (size > 0 && size <= 131072) {
      const value = args[3].readUtf8String(size);
      // Only public identifiers and path without query parameters may leave
      // this process. Never send the original payload, URLs or user strings.
      output.appids = [...new Set(value.match(/\bwx[0-9a-f]{16}\b/g)||[])];
      output.routes = [...new Set(value.match(/(?:subpackages|pages)\/[a-zA-Z0-9_./-]+/g)||[])];
      try {
        const object = JSON.parse(value);
        output.knownFields = ['appid','app_id','url','path','scene','time','launch_config_base64']
          .filter(key=>Object.prototype.hasOwnProperty.call(object,key));
      } catch (_) {}
    }
    events++;
    send(output);
  } catch (_) {
    // No memory, raw error (which can include a payload) or stack dump.
  }
}});
rpc.exports = {finish() { hook.detach(); return events; }};
'''

SHOP_SCRIPT = r'''
'use strict';
const module = Process.getModuleByName('Weixin.dll');
let events = 0;
function str(object) {
  try {
    const size = object.add(16).readU64().toNumber();
    const capacity = object.add(24).readU64().toNumber();
    if(size < 0 || size > 16384 || capacity < size) return '';
    return (capacity < 16 ? object : object.readPointer()).readUtf8String(size) || '';
  } catch(_) { return ''; }
}
function publicRoute(value) {
  if(!/^(?:\/?subpackages\/|\/?pages\/)/.test(value)) return '';
  const parts = value.split('?');
  const allowed = [];
  for(const pair of (parts[1]||'').split('&')) {
    const entry = pair.split('=');
    if(['appid','app_id','shop_appid','entrance_id','scene'].includes(entry[0]) &&
       /^[a-zA-Z0-9_%.-]{0,160}$/.test(entry[1]||'')) allowed.push(pair);
  }
  return parts[0] + (allowed.length ? '?' + allowed.join('&') : '');
}
function classify(value) {
  if(/^wx[0-9a-f]{16}$/.test(value)) return {appid:value};
  const route = publicRoute(value);
  if(route) return {route:route};
  if(value === 'lining李宁') return {shop:value};
  return {length:value.length};
}
const hooks = [];
hooks.push(Interceptor.attach(module.base.add(0x3ec94e0), {onEnter(args) {
  if(events++ > 20) return;
  send({event:'normal-shop-card',identity:classify(str(args[1]))});
}}));
hooks.push(Interceptor.attach(module.base.add(0x3ecafa0), {onEnter(args) {
  if(events++ > 20) return;
  send({event:'normal-shop-launch-info',fields:[0,32,64,96].map(offset=>classify(str(args[1].add(offset))))});
}}));
hooks.push(Interceptor.attach(module.base.add(0x220a80), {onEnter(args) {
  const caller = this.returnAddress.sub(module.base).toUInt32();
  if(caller < 0x3ec0000 || caller >= 0x3ed0000 || events++ > 20) return;
  try {
    const request = args[1];
    send({event:'normal-shop-launch-request',caller:caller.toString(16),
      appid:classify(str(request.add(8).readPointer())),
      route:classify(str(request.add(24).readPointer())),
      referrer:classify(str(request.add(32).readPointer())),
      scene:request.add(48).readU32(),thread:Process.getCurrentThreadId()});
  } catch(_) {send({event:'normal-shop-launch-request-unreadable'});}
}}));
rpc.exports = {finish() {hooks.forEach(hook=>hook.detach()); return events;}};
'''


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    pid = int(sys.argv[1])
    process = psutil.Process(pid)
    if process.name().lower() != 'weixin.exe':
        raise RuntimeError('Expected the verified Weixin.exe process')
    if any(p.info['name'] == '微信小店商品列表采集.exe'
           for p in psutil.process_iter(['name'])):
        raise RuntimeError('Collector must be closed')
    path = loaded_module_path(pid, 'Weixin.dll')
    if not path or hashlib.sha256(path.read_bytes()).hexdigest() != SUPPORTED_PROFILE.weixin_sha256:
        raise RuntimeError('Probe only supports exact verified Weixin 4.1.13.63 DLL')
    session = frida.get_local_device().attach(pid)
    script = None
    count = 0
    def on_message(message, _data):
        nonlocal count
        if message.get('type') == 'send':
            count += 1
            print(json.dumps(message['payload'], ensure_ascii=False), flush=True)
    try:
        script = session.create_script(SHOP_SCRIPT if '--shop' in sys.argv else SCRIPT)
        script.on('message', on_message)
        script.load()
        seconds = 600 if '--long' in sys.argv else 120
        print(f'READY {time.strftime("%H:%M:%S")}: close the shop window, then open only the existing lining李宁 card once; {seconds}-second observation', flush=True)
        end = time.monotonic() + seconds
        while time.monotonic() < end and process.is_running():
            time.sleep(0.25)
        print('CAPTURED_EVENTS', count, flush=True)
        return 0 if count else 2
    finally:
        if script is not None:
            try:
                script.exports_sync.finish()
                script.unload()
            except frida.InvalidOperationError:
                pass
        session.detach()
        print('OBSERVER_REMOVED', flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
