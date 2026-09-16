from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable


LogCallback = Callable[[str], None]
CountCallback = Callable[[], int]


if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
else:
    user32 = None
    kernel32 = None
    WNDENUMPROC = None

SW_RESTORE = 9
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]


if user32 is not None:
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT


def _window_text(hwnd: int) -> str:
    if user32 is None:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _pid_of(hwnd: int) -> int:
    if user32 is None:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_name(pid: int) -> str:
    if kernel32 is None or not pid:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return os.path.basename(buffer.value)
    finally:
        kernel32.CloseHandle(handle)
    return ""


def find_shop_window() -> int | None:
    if user32 is None or WNDENUMPROC is None:
        return None
    candidates: list[tuple[int, int]] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        title = _window_text(hwnd)
        exe = _process_name(_pid_of(hwnd)).lower()
        if "wechatappex" not in exe:
            return True
        score = 0
        if "小店" in title:
            score += 10
        if 300 <= width <= 700 and 500 <= height <= 1200:
            score += 3
        if height > width:
            score += 1
        if score:
            candidates.append((score, int(hwnd)))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _bring(hwnd: int) -> bool:
    if user32 is None or not user32.IsWindow(hwnd):
        return False
    user32.ShowWindow(hwnd, SW_RESTORE)
    foreground = user32.GetForegroundWindow()
    current_thread = kernel32.GetCurrentThreadId() if kernel32 else 0
    other_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    if current_thread and other_thread:
        user32.AttachThreadInput(current_thread, other_thread, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if current_thread and other_thread:
            user32.AttachThreadInput(current_thread, other_thread, False)
    return True


def scroll_window(hwnd: int, notches: int = 4) -> bool:
    if user32 is None or not user32.IsWindow(hwnd):
        return False
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    old_cursor = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(old_cursor))
    old_foreground = user32.GetForegroundWindow()
    x = rect.left + max(20, (rect.right - rect.left) // 2)
    y = rect.top + max(30, int((rect.bottom - rect.top) * 0.72))
    try:
        if not _bring(hwnd):
            return False
        user32.SetCursorPos(x, y)
        time.sleep(0.04)
        event = INPUT()
        event.type = 0
        event.mi.dwFlags = MOUSEEVENTF_WHEEL
        event.mi.mouseData = (-WHEEL_DELTA * notches) & 0xFFFFFFFF
        return user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) == 1
    finally:
        user32.SetCursorPos(old_cursor.x, old_cursor.y)
        if old_foreground and old_foreground != hwnd and user32.IsWindow(old_foreground):
            _bring(old_foreground)


class AutoScroller:
    def __init__(
        self,
        get_count: CountCallback,
        log: LogCallback | None = None,
        interval: float = 2.5,
        empty_limit: int = 8,
        max_scrolls: int = 240,
    ) -> None:
        self.get_count = get_count
        self.log = log or (lambda _message: None)
        self.interval = interval
        self.empty_limit = empty_limit
        self.max_scrolls = max_scrolls
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.2)
        self._thread = None

    def _run(self) -> None:
        if self._stop.wait(1.2):
            return
        hwnd = find_shop_window()
        if not hwnd:
            self.log("没有找到微信小店窗口，请手动滚动商品列表")
            return
        self.log("自动滚动已启动；只滚动列表，不会点击商品")
        last_count = self.get_count()
        empty = 0
        for _index in range(self.max_scrolls):
            if self._stop.is_set():
                break
            if not scroll_window(hwnd):
                self.log("自动滚动失败，请改为手动滚动")
                break
            if self._stop.wait(self.interval):
                break
            count = self.get_count()
            if count > last_count:
                empty = 0
                last_count = count
            else:
                empty += 1
                if empty >= self.empty_limit:
                    self.log("连续多次没有新商品，可能已经滚到底")
                    break
        self.log("自动滚动结束")
