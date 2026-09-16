from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from typing import Callable

from .license import (
    LicenseAuthorization,
    LicenseClient,
    LicenseError,
    LicenseStorage,
    masked_code,
)


class LicenseGate:
    def __init__(
        self,
        root: tk.Tk,
        on_authorized: Callable[[LicenseAuthorization], None],
        *,
        storage: LicenseStorage | None = None,
        client: LicenseClient | None = None,
    ) -> None:
        self.root = root
        self.on_authorized = on_authorized
        self.storage = storage or LicenseStorage()
        self.client = client or LicenseClient()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.installation_id = self.storage.get_or_create_installation_id()
        self.root.title("微信小店商品列表采集 · 卡密验证")
        self.root.geometry("520x330")
        self.root.minsize(480, 310)
        self.root.resizable(False, False)
        self.status_text = tk.StringVar(value="请输入管理员提供的卡密")
        self.code_text = tk.StringVar()
        self._build_ui()
        self.root.after(120, self._drain_events)
        saved_code = self.storage.load_code()
        if saved_code:
            self.code_text.set(saved_code)
            self.root.after(180, lambda: self._validate(saved_code, automatic=True))

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("LicenseTitle.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("LicenseHint.TLabel", foreground="#64748b")
        self.frame = ttk.Frame(self.root, padding=28)
        self.frame.pack(fill="both", expand=True)
        ttk.Label(
            self.frame,
            text="微信小店商品列表采集",
            style="LicenseTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            self.frame,
            text="首次使用输入卡密；验证成功后会安全保存在当前 Windows 用户中。",
            style="LicenseHint.TLabel",
            wraplength=440,
        ).pack(anchor="w", pady=(8, 24))
        form = ttk.Frame(self.frame)
        form.pack(fill="x")
        ttk.Label(form, text="卡密").pack(anchor="w", pady=(0, 7))
        self.code_entry = ttk.Entry(
            form,
            textvariable=self.code_text,
            font=("Consolas", 11),
        )
        self.code_entry.pack(fill="x", ipady=5)
        self.code_entry.bind("<Return>", lambda _event: self._validate_from_input())
        self.status_label = ttk.Label(
            form,
            textvariable=self.status_text,
            style="LicenseHint.TLabel",
            wraplength=440,
        )
        self.status_label.pack(anchor="w", pady=(10, 0))
        actions = ttk.Frame(self.frame)
        actions.pack(fill="x", side="bottom")
        self.validate_button = ttk.Button(
            actions,
            text="验证并打开软件",
            command=self._validate_from_input,
        )
        self.validate_button.pack(side="right")
        self.code_entry.focus_set()

    def _validate_from_input(self) -> None:
        self._validate(self.code_text.get(), automatic=False)

    def _validate(self, code: str, *, automatic: bool) -> None:
        if str(self.validate_button["state"]) == "disabled":
            return
        code = code.strip()
        if not code:
            self._show_error("请输入卡密。")
            return
        self.validate_button.configure(state="disabled")
        self.code_entry.configure(state="disabled")
        self.status_label.configure(foreground="#64748b")
        self.status_text.set(
            f"正在验证已保存卡密 {masked_code(code)}…"
            if automatic
            else "正在连接服务器验证卡密…"
        )
        thread = threading.Thread(
            target=self._validate_worker,
            args=(code,),
            daemon=True,
        )
        thread.start()

    def _validate_worker(self, code: str) -> None:
        try:
            authorization = self.client.validate(code, self.installation_id)
            self.storage.save_code(code)
        except (LicenseError, ValueError, OSError) as exc:
            self.events.put(("error", exc))
            return
        self.events.put(("authorized", authorization))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "error":
                    self._show_error(str(payload))
                    self.validate_button.configure(state="normal")
                    self.code_entry.configure(state="normal")
                    self.code_entry.focus_set()
                    self.code_entry.selection_range(0, "end")
                elif kind == "authorized" and isinstance(payload, LicenseAuthorization):
                    self._finish(payload)
                    return
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(120, self._drain_events)

    def _finish(self, authorization: LicenseAuthorization) -> None:
        self.frame.destroy()
        self.root.resizable(True, True)
        self.on_authorized(authorization)

    def _show_error(self, message: str) -> None:
        self.status_text.set(message)
        self.status_label.configure(foreground="#c82b35")


def format_expiry(value: str | None) -> str:
    if not value:
        return "永久有效"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return value
