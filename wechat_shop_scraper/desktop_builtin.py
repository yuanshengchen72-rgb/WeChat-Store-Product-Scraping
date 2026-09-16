"""Normal, built-in shop startup before the guarded assistant connection.

Only the two exact verified Weixin DLLs are supported. No chat objects, saved
session pointers, credential data, URI guessing or WMPF initialization patches.
The normal dispatcher copies a native-default request synchronously and obtains
the current logged-in services itself. See docs/desktop-builtin-launch.md.
"""
from __future__ import annotations

from contextlib import contextmanager
import time

from .desktop_bootstrap import _ShopUI, _check_cancel, ShopBootstrapError, ShopBootstrapTimeout
from .desktop_bootstrap import FileHelperShopOpener, ShopBootstrapEntryUnavailable


_CONSTRUCTORS = {
    '8bf9ae1edf52676bbfa2e0c4024af207c0f9bba589758f99de467515c90635e1': 0x7866f0,
    '8f7406a8a465e851ee10eaecccb572d0e5b4e00d480c770938c714c396097b74': 0x7869b0,
}


def _validate_main(expected_pid):
    from .desktop_keyword import _visible_weixin_identity, loaded_module_path, _sha256
    pid, thread = _visible_weixin_identity()
    if pid != expected_pid or not thread:
        raise ShopBootstrapError('微信主进程已变化，无法建立小店启动连接；请保持微信登录')
    path = loaded_module_path(pid, 'Weixin.dll')
    constructor = _CONSTRUCTORS.get(_sha256(path)) if path else None
    if constructor is None:
        raise ShopBootstrapError('当前微信文件未适配内置小店启动；支持指定的 4.1.13.63 / 4.1.13.65，已停止调用')
    return thread, constructor


_SCRIPT = r'''
'use strict';
if (Process.arch !== 'x64') throw new Error('Expected x64');
const base = Process.getModuleByName('Weixin.dll').base;
const construct = new NativeFunction(base.add(CONSTRUCTOR_OFFSET),'pointer',['pointer']);
const launch = new NativeFunction(base.add(0x220a80),'void',['pointer','pointer','pointer']);
const held = [];
let state = 'idle', cancelled = false;
function text(value) {
  const object = Memory.alloc(32);
  const capacity = Math.max(31,value.length);
  const data = Memory.alloc(capacity+1);
  data.writeUtf8String(value);
  object.writeByteArray(new Uint8Array(32));
  object.writePointer(data);
  object.add(16).writeU64(value.length);
  object.add(24).writeU64(capacity);
  held.push(object,data);
  return object;
}
rpc.exports = {
  begin(thread) {
    if(state !== 'idle') throw new Error('Only one launch per controller');
    state = 'pending';
    Process.runOnThread(Number(thread), function() {
      if(cancelled) { state = 'cancelled'; return; }
      const request = Memory.alloc(0xe8);
      held.push(request);
      construct(request);
      request.add(8).writePointer(text('wxca8d4b8e8feedc2a'));
      request.add(24).writePointer(text('subpackages/searchCenter/pages/globalSearch/globalSearch'));
      request.add(0xe4).writeU32(1|4);
      // Preserve native defaults, including scene=1000. Native launch copies
      // the request before returning; never destroy Frida-owned strings with
      // the C++ heap destructor. All buffers live until this script unloads.
      launch(ptr(0), request, ptr(0));
      state = 'dispatched';
    }).catch(function(_) {state='failed';});
    return true;
  },
  status() {return state;},
  cancel() {cancelled=true;}
};
'''


class _NativeLaunch:
    def __init__(self, pid, constructor):
        self.pid, self.constructor = pid, constructor
        self.session = self.script = None

    def __enter__(self):
        import frida
        try:
            self.session = frida.get_local_device().attach(self.pid)
            self.script = self.session.create_script(
                _SCRIPT.replace('CONSTRUCTOR_OFFSET', str(self.constructor)))
            self.script.load()
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def begin(self, thread):
        self.script.exports_sync.begin(thread)

    def status(self):
        value = self.script.exports_sync.status()
        if value in {'failed', 'cancelled'}:
            raise ShopBootstrapError('微信正常启动连接未完成；当前词保留')
        return value

    def __exit__(self, *_):
        if self.script:
            try:
                self.script.exports_sync.cancel()
            except Exception:
                pass  # The process/script may already have disappeared.
            try:
                self.script.unload()
            except Exception:
                pass
            self.script = None
        if self.session:
            try:
                self.session.detach()
            except Exception:
                pass
            self.session = None


@contextmanager
def _shop_ui(**kwargs):
    import uiautomation as auto
    with auto.UIAutomationInitializerInThread():
        yield _ShopUI(auto, **kwargs)


class BuiltinShopOpener:
    """Close only shop windows -> normal built-in entry -> rendered search."""
    entry_description = '内置小店启动入口，不依赖文件传输助手或店铺卡片'

    def enter_search_keyword(self, *, weixin_pid, get_host_pid, cancel_event,
                             keyword, log=None):
        """UIA fallback for WeChat builds whose page JS bridge returns empty."""
        with _shop_ui(weixin_pid=weixin_pid, get_host_pid=get_host_pid,
                      cancel_event=cancel_event,
                      log=log or (lambda _message: None)) as ui:
            ui.enter_search_keyword(keyword)

    def check_entry(self, *, weixin_pid, get_host_pid, cancel_event, log):
        _check_cancel(cancel_event)
        _validate_main(weixin_pid)
        _check_cancel(cancel_event)

    def __call__(self, *, weixin_pid, get_host_pid, cancel_event, log,
                 force_relaunch=False):
        _check_cancel(cancel_event)
        thread, constructor = _validate_main(weixin_pid)
        with _shop_ui(weixin_pid=weixin_pid, get_host_pid=get_host_pid,
                      cancel_event=cancel_event, log=log) as ui:
            # A visible shop host is already the user's verified launch
            # surface.  On 4.1.13.65 closing it first can destroy the only
            # WMPF instance from which the native globalSearch dispatch works.
            # Reuse that host and let the native entry replace its route.
            if ui.shop_windows() and not force_relaunch:
                log('检测到已打开的小店窗口，保留宿主并直接切换到关键词搜索页')
                # During recovery the normal entry may already have rendered
                # the real search page.  Re-dispatching the native launch on
                # that live WMPF host can terminate the child process before
                # its new debug session is registered (the failure seen on
                # 4.1.13.65/25560).  Reuse the visible page and let the
                # backend attach to it instead of stacking another launch.
                deadline = time.monotonic() + 35
                stable = 0
                while time.monotonic() < deadline:
                    _check_cancel(cancel_event)
                    stable = stable + 1 if ui.search_rendered() else 0
                    if stable >= 3:
                        log('检测到现有小店搜索页，跳过重复启动，直接等待采集连接')
                        return
                    cancel_event.wait(0.25)
            elif force_relaunch and ui.shop_windows():
                log('采集会话未绑定，先关闭现有小店页面，再重新打开搜索页')
                ui.close_shop()
                deadline = time.monotonic() + 8
                while ui.shop_windows():
                    _check_cancel(cancel_event)
                    if time.monotonic() >= deadline:
                        raise ShopBootstrapTimeout('旧小店页面未完成关闭；未重复启动，当前词保留')
                    cancel_event.wait(0.2)
                _check_cancel(cancel_event)
            else:
                log('自动恢复：小店窗口尚未打开，保留微信登录并调用内置搜索入口')
            _check_cancel(cancel_event)
            # The binary/main-thread check is repeated after closing: no stale
            # process identity or native address may cross the shutdown step.
            thread, constructor = _validate_main(weixin_pid)
            log('自动恢复：通过内置入口独立打开小店关键词搜索页，无需文件传输助手')
            with _NativeLaunch(weixin_pid, constructor) as native:
                _check_cancel(cancel_event)
                native.begin(thread)
                deadline = time.monotonic() + 35
                stable = 0
                while time.monotonic() < deadline:
                    _check_cancel(cancel_event)
                    dispatched = native.status() == 'dispatched'
                    stable = stable + 1 if dispatched and ui.search_rendered() else 0
                    if stable >= 3:
                        log('内置启动完成：搜索页内容已显示，正在重新建立采集连接')
                        return
                    cancel_event.wait(0.25)
            raise ShopBootstrapTimeout('内置入口已调用，但搜索页内容尚未就绪；将自动重试，当前词保留')


class ResilientShopOpener:
    """Try the native entry, then the user's designated shop-card entry.

    WeChat 4.1.13.65 can accept the native dispatch while refusing to create a
    cold WMPF host.  The fallback is deliberately limited to the exact
    lining李宁 card in File Transfer Assistant; it never guesses another chat
    or treats a PID as proof that the miniapp opened.
    """

    entry_description = '内置小店入口；冷启动失败时自动使用已指定的 lining李宁店铺卡片'

    def __init__(self, primary=None, fallback=None):
        self.primary = primary or BuiltinShopOpener()
        self.fallback = fallback or FileHelperShopOpener()

    def check_entry(self, **kwargs):
        # Version validation is useful even when the fallback is needed.  The
        # fallback card is checked only immediately before it is clicked.
        checker = getattr(self.primary, 'check_entry', None)
        if callable(checker):
            try:
                return checker(**kwargs)
            except ShopBootstrapEntryUnavailable:
                raise
            except ShopBootstrapError as primary_error:
                # New WeChat builds may be safely matched for collection while
                # their native built-in launcher constructor has not yet been
                # verified.  Let the designated card opener prove its own
                # entry instead of rejecting the version before fallback.
                fallback_checker = getattr(self.fallback, 'check_entry', None)
                if not callable(fallback_checker):
                    raise primary_error
                log = kwargs.get('log') or (lambda _message: None)
                log(f'内置小店入口暂未适配，正在检查固定店铺卡片恢复入口：{primary_error}')
                return fallback_checker(**kwargs)

    def enter_search_keyword(self, **kwargs):
        enter = getattr(self.primary, 'enter_search_keyword', None)
        if callable(enter):
            return enter(**kwargs)
        return self.fallback.enter_search_keyword(**kwargs)

    def __call__(self, **kwargs):
        log = kwargs.get('log') or (lambda _message: None)
        try:
            return self.primary(**kwargs)
        except (ShopBootstrapTimeout, ShopBootstrapError) as primary_error:
            _check_cancel(kwargs.get('cancel_event'))
            log(f'内置小店入口未创建宿主，自动切换已指定的 lining李宁店铺卡片：{primary_error}')
            try:
                fallback_kwargs = dict(kwargs)
                fallback_kwargs.pop('force_relaunch', None)
                return self.fallback(**fallback_kwargs)
            except ShopBootstrapEntryUnavailable as fallback_error:
                # The visible log must not redirect the user to a chat window
                # when the primary native launch already created and then
                # lost the WMPF host.  Keep the primary failure so the backend
                # can apply its crash/compatibility policy and retry safely.
                log(
                    f'未检测到可用的文件传输助手入口（{fallback_error}），'
                    '不再等待文件传输助手，继续按内置入口恢复'
                )
                raise primary_error
            except ShopBootstrapError as fallback_error:
                raise ShopBootstrapEntryUnavailable(
                    f'内置入口和李宁店铺卡片暂不可用（{fallback_error}）；将自动重试，当前词保留'
                ) from fallback_error
