"""Normal, narrowly scoped File Transfer Assistant shop-card recovery.

No chat dump, credentials, global mouse/keyboard input, or process termination.
Only the user-designated card in an independently opened filehelper window is
addressed. An absent card/window is a recoverable precondition, not permission
to inspect another conversation or invent a launch URI.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import threading
import time
from typing import Callable


SHOP_NAME = "lining李宁"
CARD_NAME = "小店名片lining李宁品牌自营"
FILEHELPER_ID = "ChatSingleWindowfilehelper"
MESSAGE_LIST_ID = "chat_message_list"


class ShopBootstrapError(RuntimeError):
    pass


class ShopBootstrapTimeout(ShopBootstrapError):
    """The designated entry exists, but closing/rendering did not finish."""


class ShopBootstrapEntryUnavailable(ShopBootstrapError):
    """Wait for the designated entry; never count this as a failed keyword."""


def _check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise ShopBootstrapError("采集已停止，取消自动打开小程序")


def _sent_card_x(left: int, right: int, dpi: int) -> int:
    padding = round(100 * (dpi or 96) / 96)
    if right - left < padding * 2:
        raise ShopBootstrapError("李宁卡片显示区域过窄，请展开文件传输助手窗口")
    return right - padding


class FileHelperShopOpener:
    """Close -> normal card -> rendered shop. Safe to inject into backend tests."""

    def check_entry(self, *, weixin_pid: int, get_host_pid: Callable[[], int],
                    cancel_event: threading.Event, log: Callable[[str], None]) -> None:
        """Preflight the recovery entry without closing or launching any applet."""
        _check_cancel(cancel_event)
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            _ShopUI(auto, weixin_pid, get_host_pid, cancel_event, log).require_card()
        _check_cancel(cancel_event)

    def __call__(self, *, weixin_pid: int, get_host_pid: Callable[[], int],
                 cancel_event: threading.Event, log: Callable[[str], None]) -> None:
        _check_cancel(cancel_event)
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():
            ui = _ShopUI(auto, weixin_pid, get_host_pid, cancel_event, log)
            # Verify that the exact entry exists BEFORE closing the old applet.
            ui.require_card()
            log("自动恢复：先关闭小店小程序，保留微信登录和当前关键词断点")
            ui.close_shop()
            deadline = time.monotonic() + 8.0
            while ui.shop_windows():
                _check_cancel(cancel_event)
                if time.monotonic() >= deadline:
                    raise ShopBootstrapTimeout("小程序窗口未完成关闭；未重复打开，也未结束微信进程")
                cancel_event.wait(0.2)
            _check_cancel(cancel_event)
            log("自动恢复：从文件传输助手打开固定店铺“lining李宁”")
            ui.open_card(log)
            deadline = time.monotonic() + 35.0
            stable = 0
            while time.monotonic() < deadline:
                _check_cancel(cancel_event)
                stable = stable + 1 if ui.shop_rendered() else 0
                if stable >= 3:
                    log("自动恢复：lining李宁首页已显示，正在重新建立搜索连接")
                    return
                cancel_event.wait(0.3)
            raise ShopBootstrapTimeout("已打开李宁小店卡片，但店铺内容仍未加载；当前关键词断点保留")


class _ShopUI:
    def __init__(self, auto, weixin_pid: int, get_host_pid: Callable[[], int],
                 cancel_event: threading.Event, log: Callable[[str], None]):
        self.auto = auto
        self.cancel_event = cancel_event
        self.log = log
        # A dedicated COM client per worker avoids cross-apartment element reuse.
        import comtypes.client
        core = self.core = comtypes.client.GetModule("UIAutomationCore.dll")
        self.client = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=core.IUIAutomation
        )
        self.weixin_pid = weixin_pid
        self.get_host_pid = get_host_pid
        self._host_pid = 0
        self._host_checked = 0.0
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user32.ScreenToClient.restype = wintypes.BOOL
        self.user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        self.user32.GetDpiForWindow.restype = wintypes.UINT
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.ShowWindowAsync.restype = wintypes.BOOL
        self._enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumWindows.argtypes = [self._enum_type, wintypes.LPARAM]

    def _windows(self, pid, titles, *, include_hidden=False):
        handles = []
        @self._enum_type
        def visit(hwnd, _):
            owner = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and (include_hidden or self.user32.IsWindowVisible(hwnd)):
                title = ctypes.create_unicode_buffer(128)
                self.user32.GetWindowTextW(hwnd, title, 128)
                if title.value in titles:
                    handles.append(hwnd)
            return True
        self.user32.EnumWindows(visit, 0)
        return handles

    def _find(self, parent, *, scope=4, **properties):
        """Provider-side exact query; never export or enumerate other messages."""
        ids = {"name": 30005, "automation_id": 30011, "pid": 30002,
               "control_type": 30003, "offscreen": 30022}
        conditions = [self.client.CreatePropertyCondition(ids[key], value)
                      for key, value in properties.items()]
        condition = conditions[0]
        for other in conditions[1:]:
            condition = self.client.CreateAndCondition(condition, other)
        rows = parent.FindAll(scope, condition)
        return [rows.GetElement(index) for index in range(rows.Length)]

    def require_card(self):
        try:
            return self._require_card()
        except ShopBootstrapError:
            raise
        except Exception as exc:
            # UIA elements can disappear between HWND enumeration and query.
            # Preserve the typed recovery state rather than a keyword failure.
            raise ShopBootstrapEntryUnavailable(
                f"文件传输助手入口控件暂不可读（{type(exc).__name__}），将重新检查"
            ) from exc

    def _require_card(self):
        _check_cancel(self.cancel_event)
        windows = [self.client.ElementFromHandle(hwnd)
                   for hwnd in self._windows(self.weixin_pid, {"文件传输助手"}, include_hidden=True)]
        windows = [item for item in windows if item.CurrentAutomationId == FILEHELPER_ID]
        if len(windows) != 1:
            raise ShopBootstrapEntryUnavailable(
                f"未识别到唯一的独立文件传输助手窗口（识别数={len(windows)}）。"
                "请单独打开文件传输助手；入口恢复后自动继续，无需再点开始；不会读取其他聊天"
            )
        window = windows[0]
        hwnd = window.CurrentNativeWindowHandle
        if self.user32.IsIconic(hwnd) or not self.user32.IsWindowVisible(hwnd):
            _check_cancel(self.cancel_event)
            self.log("自动恢复入口：显示已确认的独立文件传输助手窗口，不激活、不移动鼠标")
            if not self.user32.ShowWindowAsync(hwnd, 4):  # SW_SHOWNOACTIVATE
                raise ShopBootstrapEntryUnavailable("独立文件传输助手窗口暂不能显示，将重新检查")
            self.cancel_event.wait(0.3)
            _check_cancel(self.cancel_event)
            if self.user32.IsIconic(hwnd) or not self.user32.IsWindowVisible(hwnd):
                raise ShopBootstrapEntryUnavailable("独立文件传输助手窗口尚未恢复显示，将重新检查")
            window = self.client.ElementFromHandle(hwnd)
            if window.CurrentAutomationId != FILEHELPER_ID:
                raise ShopBootstrapEntryUnavailable("文件传输助手窗口身份已变化，将重新定位")
        lists = self._find(window, automation_id=MESSAGE_LIST_ID)
        if len(lists) != 1:
            raise ShopBootstrapEntryUnavailable("文件传输助手消息区域尚未就绪")
        cards = self._find(lists[0], name=CARD_NAME, control_type=50007, offscreen=False)
        if not cards:
            # Query only the designated card, including off-screen matches. Do
            # not enumerate message contents, scroll other chats or guess bounds.
            offscreen = self._find(lists[0], name=CARD_NAME, control_type=50007)
            if offscreen:
                _check_cancel(self.cancel_event)
                try:
                    pattern = offscreen[-1].GetCurrentPattern(10017)  # ScrollItem
                    pattern.QueryInterface(self.core.IUIAutomationScrollItemPattern).ScrollIntoView()
                except Exception as exc:
                    raise ShopBootstrapEntryUnavailable(
                        "李宁卡片不在可见区域，当前微信未能自动滚动到该卡片；请让指定卡片可见"
                    ) from exc
                self.log("自动恢复入口：已请求将指定的 lining李宁卡片滚动到可见区域")
                self.cancel_event.wait(0.3)
                _check_cancel(self.cancel_event)
                window = self.client.ElementFromHandle(hwnd)
                if window.CurrentAutomationId != FILEHELPER_ID:
                    raise ShopBootstrapEntryUnavailable("文件传输助手窗口身份已变化，将重新定位")
                lists = self._find(window, automation_id=MESSAGE_LIST_ID)
                cards = (self._find(lists[0], name=CARD_NAME, control_type=50007, offscreen=False)
                         if len(lists) == 1 else [])
            if not cards:
                raise ShopBootstrapEntryUnavailable(
                    "文件传输助手中未找到可见的“lining李宁”小店卡片；请让指定卡片可见，之后自动继续；不会点击其他店铺"
                )
        _check_cancel(self.cancel_event)
        return window, cards[-1]

    def shop_windows(self):
        if time.monotonic() - self._host_checked >= 3.0:
            self._host_pid = self.get_host_pid()
            self._host_checked = time.monotonic()
        if not self._host_pid:
            return []
        return self._windows(self._host_pid, {"小店", "小店与卡包"})

    def close_shop(self):
        for hwnd in self.shop_windows():
            _check_cancel(self.cancel_event)
            # Use the applet's own close action when exposed by Qt. A bare
            # WM_CLOSE can dismiss the shell before its applet is torn down.
            try:
                window = self.client.ElementFromHandle(hwnd)
                buttons = self._find(window, name='关闭', control_type=50000, offscreen=False)
                if len(buttons) == 1:
                    _check_cancel(self.cancel_event)
                    pattern = buttons[0].GetCurrentPattern(10000)
                    pattern.QueryInterface(self.core.IUIAutomationInvokePattern).Invoke()
                    continue
            except ShopBootstrapError:
                raise
            except Exception:
                pass  # Older providers expose no Invoke pattern.
            _check_cancel(self.cancel_event)
            if not self.user32.PostMessageW(hwnd, 0x0010, 0, 0):
                raise ShopBootstrapError("无法正常关闭小程序窗口")

    def open_card(self, log):
        window, card = self.require_card()  # reacquire after the close, no stale bounds
        hwnd = window.CurrentNativeWindowHandle
        rect = card.CurrentBoundingRectangle
        labels = self._find(card, name=SHOP_NAME, offscreen=False)
        if labels:
            rect = labels[0].CurrentBoundingRectangle
        if labels:
            target_x = (rect.left + rect.right) // 2
        else:
            # Qt exposes the entire message row as one ListItem. The user sent
            # this designated card themselves: its bubble is right-aligned.
            # Stay inside that row's card, left of the avatar, using window DPI
            # rather than absolute screen coordinates or the row's empty centre.
            target_x = _sent_card_x(rect.left, rect.right, self.user32.GetDpiForWindow(hwnd))
        point = wintypes.POINT(target_x, (rect.top + rect.bottom) // 2)
        if rect.right <= rect.left or rect.bottom <= rect.top:
            raise ShopBootstrapError("李宁小店卡片位置尚未可用")
        if not self.user32.ScreenToClient(hwnd, ctypes.byref(point)):
            raise ShopBootstrapError("无法定位文件传输助手内的小店卡片")
        position = (point.x & 0xffff) | ((point.y & 0xffff) << 16)
        # Post only to the exact chat window. Never move the user's real cursor,
        # type into an edit field, send Enter, or restore a stale mouse position.
        for message, buttons in ((0x0200, 0), (0x0201, 1), (0x0202, 0)):
            if not self.user32.PostMessageW(hwnd, message, buttons, position):
                raise ShopBootstrapError("无法打开文件传输助手内的李宁小店卡片")

    def shop_rendered(self):
        for hwnd in self.shop_windows():
            window = self.client.ElementFromHandle(hwnd)
            if self._find(window, name=SHOP_NAME, offscreen=False):
                return True
        return False

    def search_rendered(self):
        """Visible search controls, not merely the title of an empty applet."""
        for hwnd in self.shop_windows():
            try:
                window = self.client.ElementFromHandle(hwnd)
                if (self._find(window, name='搜索订单和商品', offscreen=False)
                        and self._find(window, name='搜索', offscreen=False)):
                    return True
            except Exception:
                # A replaced host invalidates its old UIA nodes. Reacquire next
                # iteration; never inspect any main-window/chat descendants.
                continue
        return False

    def enter_search_keyword(self, keyword):
        """Fill only the visible 小店 search box and invoke its search button."""
        for hwnd in self.shop_windows():
            _check_cancel(self.cancel_event)
            window = self.client.ElementFromHandle(hwnd)
            # WMPF 25560 exposes the placeholder as a custom control type on
            # some launches, so match the exact visible names first and avoid
            # rejecting a usable field solely by provider-reported type.
            edits = self._find(window, name='搜索订单和商品', offscreen=False)
            buttons = self._find(window, name='搜索', offscreen=False)
            if len(edits) != 1 or len(buttons) != 1:
                continue
            try:
                try:
                    value = edits[0].GetCurrentPattern(10002)
                    value.QueryInterface(self.core.IUIAutomationValuePattern).SetValue(str(keyword))
                except Exception:
                    # Some WMPF builds expose no ValuePattern (NULL COM
                    # pointer) although the field is keyboard-editable.
                    edits[0].SetFocus()
                    self.auto.SendKeys('{Ctrl}a{Delete}')
                    self.auto.SendKeys(str(keyword))
                try:
                    invoke = buttons[0].GetCurrentPattern(10000)
                    invoke.QueryInterface(self.core.IUIAutomationInvokePattern).Invoke()
                except Exception:
                    # WMPF sometimes exposes the button without InvokePattern;
                    # UIA's scoped Click still targets only this 小店 window.
                    rect = buttons[0].CurrentBoundingRectangle
                    self.auto.Click((rect.left + rect.right) // 2,
                                    (rect.top + rect.bottom) // 2)
                return
            except Exception as exc:
                raise ShopBootstrapError(f'无法通过小店搜索框输入关键词：{exc}') from exc
        raise ShopBootstrapError('小店搜索框尚未可用')
