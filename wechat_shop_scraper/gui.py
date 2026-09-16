from __future__ import annotations

import os
import json
import logging
from logging.handlers import RotatingFileHandler
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .app_data import (
    cache_usage,
    clear_runtime_cache,
    default_output_directory,
    format_bytes,
    prepare_frozen_tk_runtime,
)
from .desktop_keyword import DesktopKeywordListener, DesktopWechatBackend
from .frida_bridge import FridaShopListener
from .keyword_checkpoint import KeywordCheckpointStore
from .license import LicenseAuthorization
from .license_ui import LicenseGate, format_expiry
from .mobile_keyword import (
    CONNECTION_USB,
    CONNECTION_WIFI,
    MobileKeywordListener,
    configure_adb_wifi,
    setup_frida_server_via_usb,
)
from .model import ProductCard
from .incremental_storage import IncrementalProductStore as ProductStore


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = application_dir()
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / ".cache"
MODE_STORE = "电脑微信店铺采集"
MODE_KEYWORD = "安卓手机关键词采集"
MODE_DESKTOP_KEYWORD = "电脑版微信关键词采集"
KEYWORD_MODES = (MODE_KEYWORD, MODE_DESKTOP_KEYWORD)


def is_keyword_mode(mode: str) -> bool:
    return mode in KEYWORD_MODES


def stopped_status(
    batch_finished: bool,
    pause_reason: str | None,
) -> tuple[str, str]:
    if batch_finished:
        return "全部关键词已采集完成", "全部关键词已采集完成，数据文件已更新"
    if pause_reason:
        return (
            f"自动采集已暂停：{pause_reason}",
            "自动采集保持暂停，可修复后从断点继续",
        )
    return "已停止", "采集已停止"


def safe_keyword_directory(keyword: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", keyword).strip(" ._")
    return safe[:80] or "未命名关键词"


def parse_keyword_list(value: str) -> list[str]:
    keywords: list[str] = []
    for part in re.split(r"[\r\n,，;；]+", value):
        keyword = part.strip()
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return keywords


def parse_price_range(
    min_value: str,
    max_value: str,
) -> tuple[int | None, int | None]:
    def parse_one(value: str, label: str) -> int | None:
        text = value.strip()
        if not text:
            return None
        if not text.isdecimal():
            raise ValueError(f"{label}只能填写整数元")
        return int(text)

    min_price = parse_one(min_value, "最低价")
    max_price = parse_one(max_value, "最高价")
    if (
        min_price is not None
        and max_price is not None
        and min_price > max_price
    ):
        raise ValueError("最低价不能大于最高价")
    return min_price, max_price


def parse_restart_interval(value: str) -> int:
    text = value.strip()
    if not text:
        return 0
    if not text.isdecimal():
        raise ValueError("微信重启间隔只能填写非负整数")
    return int(text)


class ShopScraperApp:
    def __init__(
        self,
        root: tk.Tk,
        authorization: LicenseAuthorization | None = None,
    ) -> None:
        self.root = root
        self.root.title("微信小店商品采集 · 20260913-v15-稳定性优化版")
        self._runtime_log = logging.Logger("collector-runtime")
        try:
            (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                DATA_DIR / "logs" / "collector-runtime.log", maxBytes=2 * 1024 * 1024,
                backupCount=2, encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self._runtime_log.addHandler(handler)
        except OSError:
            self._runtime_log.addHandler(logging.NullHandler())
        self.root.geometry("1180x720")
        self.root.minsize(900, 560)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.store = ProductStore(
            default_output_directory(DATA_DIR, keyword_mode=False)
        )
        self._checkpoint_store = KeywordCheckpointStore(
            default_output_directory(DATA_DIR, keyword_mode=True)
            / "keyword_checkpoint.json"
        )
        self.listener: (
            FridaShopListener | MobileKeywordListener | DesktopKeywordListener | None
        ) = None
        self.running = False
        self._stopping = False
        self._closing = False
        self._storage_failed = False
        self._final_export_failed = False
        self._stop_thread: threading.Thread | None = None
        self._export_thread: threading.Thread | None = None
        self._last_export_at = time.monotonic()
        self._start_thread: threading.Thread | None = None
        self._desktop_backend: DesktopWechatBackend | None = None
        self.active_mode = MODE_STORE
        self._last_csv_export_error: str | None = None
        self.status_text = tk.StringVar(value="未启动")
        self.count_text = tk.StringVar(value=f"已保存 {len(self.store)} 个商品")
        self.authorization = authorization
        self.mode_text = tk.StringVar(value=MODE_STORE)
        self.output_path_text = tk.StringVar(value=str(self.store.output_dir))
        self._output_path_customized = False
        self.current_keyword_text = tk.StringVar(value="当前：未开始")
        self._pending_keywords: list[str] = []
        self._pending_min_price: int | None = None
        self._pending_max_price: int | None = None
        self._pending_restart_interval = 30
        self._pending_connection = CONNECTION_USB
        self._pending_wifi_address = ""
        self.min_price_text = tk.StringVar()
        self.max_price_text = tk.StringVar()
        self.restart_interval_text = tk.StringVar(value="30")
        self.restart_action_text = tk.StringVar(value="完整重启微信：每")
        self.connection_text = tk.StringVar(value=CONNECTION_USB)
        self.wifi_address_text = tk.StringVar()
        self.wifi_setup_running = False
        self.phone_setup_running = False
        self._batch_finished = False
        self._resume_index = 0
        self._automation_pause_reason: str | None = None
        self.description_text = tk.StringVar()
        self._build_ui()
        try:
            saved_output = json.loads((DATA_DIR / "last-output.json").read_text(encoding="utf-8"))
            saved_path = Path(str(saved_output.get("directory") or ""))
            if saved_output.get("directory") and saved_path.is_dir():
                self._output_path_customized = True
                self.output_path_text.set(str(saved_path))
                self._select_output_store()
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self._restore_keyword_session()
        self._select_output_store()
        self.root.after(150, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Status.TLabel", foreground="#475569")
        style.configure("Treeview", rowheight=30, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="微信小店商品采集", style="Title.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.count_text).pack(side="right", pady=8)
        if self.authorization is not None:
            ttk.Label(
                header,
                text=f"卡密有效期：{format_expiry(self.authorization.expires_at)}",
                foreground="#64748b",
            ).pack(side="right", padx=(0, 18), pady=8)

        ttk.Label(outer, textvariable=self.description_text, foreground="#64748b").pack(
            fill="x", pady=(6, 14)
        )

        mode_bar = ttk.Frame(outer)
        mode_bar.pack(fill="x", pady=(0, 12))
        ttk.Label(mode_bar, text="采集模式：").pack(side="left")
        self.mode_combo = ttk.Combobox(
            mode_bar,
            textvariable=self.mode_text,
            values=(MODE_STORE, MODE_KEYWORD, MODE_DESKTOP_KEYWORD),
            state="readonly",
            width=22,
        )
        self.mode_combo.pack(side="left")
        self.mode_combo.bind("<<ComboboxSelected>>", self._on_mode_changed)
        ttk.Label(mode_bar, text="关键词列表：").pack(side="left", padx=(18, 4))
        self.keyword_entry = tk.Text(
            mode_bar,
            width=38,
            height=3,
            wrap="word",
            state="disabled",
            font=("Microsoft YaHei UI", 9),
        )
        self.keyword_entry.pack(side="left")
        ttk.Label(
            mode_bar,
            text="每行一个；无“好评多”时自动跳过，不计失败",
            foreground="#64748b",
        ).pack(side="left", padx=(8, 0))
        ttk.Label(
            mode_bar,
            textvariable=self.current_keyword_text,
            foreground="#2563eb",
        ).pack(side="left", padx=(12, 0))

        price_bar = ttk.Frame(outer)
        price_bar.pack(fill="x", pady=(0, 12))
        ttk.Label(price_bar, text="预设价格：").pack(side="left")
        ttk.Label(price_bar, text="最低价").pack(side="left")
        self.min_price_entry = ttk.Entry(
            price_bar,
            textvariable=self.min_price_text,
            width=10,
            state="disabled",
        )
        self.min_price_entry.pack(side="left", padx=(4, 4))
        ttk.Label(price_bar, text="元  ～  最高价").pack(side="left")
        self.max_price_entry = ttk.Entry(
            price_bar,
            textvariable=self.max_price_text,
            width=10,
            state="disabled",
        )
        self.max_price_entry.pack(side="left", padx=(4, 4))
        ttk.Label(
            price_bar,
            text="元（整数；任一项留空表示不限；所有关键词共用）",
            foreground="#64748b",
        ).pack(side="left")

        restart_bar = ttk.Frame(outer)
        restart_bar.pack(fill="x", pady=(0, 12))
        ttk.Label(restart_bar, textvariable=self.restart_action_text).pack(side="left")
        self.restart_interval_entry = ttk.Entry(
            restart_bar,
            textvariable=self.restart_interval_text,
            width=8,
            state="disabled",
        )
        self.restart_interval_entry.pack(side="left", padx=(4, 4))
        ttk.Label(restart_bar, text="个关键词").pack(side="left")
        ttk.Label(
            restart_bar,
            text="（默认 30；填 0 或留空表示关闭；建议 20～30）",
            foreground="#64748b",
        ).pack(side="left", padx=(8, 0))

        output_bar = ttk.Frame(outer)
        output_bar.pack(fill="x", pady=(0, 12))
        ttk.Label(output_bar, text="本批保存到：").pack(side="left")
        self.output_path_entry = ttk.Entry(
            output_bar,
            textvariable=self.output_path_text,
            width=48,
            state="readonly",
        )
        self.output_path_entry.pack(side="left", padx=(4, 0), fill="x", expand=True)
        self.output_choose_button = ttk.Button(
            output_bar,
            text="选择保存位置",
            command=self._choose_output_directory,
        )
        self.output_choose_button.pack(side="left", padx=(8, 0))
        self.output_default_button = ttk.Button(
            output_bar,
            text="恢复默认",
            command=self._use_default_output_directory,
        )
        self.output_default_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            output_bar,
            text="（换空目录＝新批次）",
            foreground="#64748b",
        ).pack(side="left", padx=(8, 0))

        connection_bar = ttk.Frame(outer)
        connection_bar.pack(fill="x", pady=(0, 12))
        ttk.Label(connection_bar, text="手机连接：").pack(side="left")
        self.connection_combo = ttk.Combobox(
            connection_bar,
            textvariable=self.connection_text,
            values=(CONNECTION_USB, CONNECTION_WIFI),
            state="disabled",
            width=14,
        )
        self.connection_combo.pack(side="left")
        self.connection_combo.bind(
            "<<ComboboxSelected>>", self._on_connection_changed
        )
        ttk.Label(connection_bar, text="WiFi 地址：").pack(
            side="left", padx=(18, 4)
        )
        self.wifi_address_entry = ttk.Entry(
            connection_bar,
            textvariable=self.wifi_address_text,
            width=22,
            state="disabled",
        )
        self.wifi_address_entry.pack(side="left")
        self.wifi_setup_button = ttk.Button(
            connection_bar,
            text="通过 USB 启用 WiFi",
            command=self._setup_wifi,
            state="disabled",
        )
        self.wifi_setup_button.pack(side="left", padx=(8, 0))
        self.phone_setup_button = ttk.Button(
            connection_bar,
            text="一键配置新手机",
            command=self._setup_phone,
            state="disabled",
        )
        self.phone_setup_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            connection_bar,
            text="（新手机先 Root 并连接数据线）",
            foreground="#64748b",
        ).pack(side="left", padx=(8, 0))
        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(0, 12))
        self.start_button = ttk.Button(toolbar, text="开始采集", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            toolbar, text="停止", command=self._stop, state="disabled"
        )
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="打开数据目录", command=self._open_data_dir).pack(
            side="left", padx=(8, 0)
        )
        self.cache_button = ttk.Button(
            toolbar,
            text="清理软件缓存",
            command=self._clear_cache,
        )
        self.cache_button.pack(side="left", padx=(8, 0))
        self.recovery_test_button = ttk.Button(
            toolbar, text="测试自动恢复", command=self._test_desktop_recovery
        )
        self.recovery_test_button.pack(side="left", padx=(8, 0))
        ttk.Label(toolbar, textvariable=self.status_text, style="Status.TLabel").pack(
            side="right"
        )
        self._on_mode_changed()

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True)
        columns = (
            "product_id",
            "keyword",
            "shop_name",
            "title",
            "price",
            "sales",
            "image",
        )
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {
            "product_id": "商品 ID",
            "keyword": "关键词",
            "shop_name": "店铺名",
            "title": "标题",
            "price": "价格",
            "sales": "销量",
            "image": "主图链接",
        }
        widths = {
            "product_id": 145,
            "keyword": 120,
            "shop_name": 140,
            "title": 290,
            "price": 90,
            "sales": 100,
            "image": 340,
        }
        for name in columns:
            self.table.heading(name, text=headings[name])
            self.table.column(name, width=widths[name], minwidth=70, anchor="w")
        scroll_y = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        scroll_x = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=8)
        log_frame.pack(fill="x", pady=(12, 0))
        self.log_box = tk.Text(
            log_frame,
            height=7,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            background="#f8fafc",
            relief="flat",
        )
        self.log_box.pack(fill="x")
        self._write_log(
            f"请选择店铺或关键词模式，再开始监听并手动滚到底。CSV 每 1000 个商品分卷，"
            f"从 {self.store.csv_path.name} 开始；"
            f"总数据保存在 {self.store.json_path}"
        )

    def _load_existing(self) -> None:
        for card in self.store.snapshot():
            self._upsert_table(card)

    def _restore_keyword_session(self) -> None:
        session = self._checkpoint_store.saved_session()
        if session is None:
            return
        restored_mode = session.mode if session.mode in KEYWORD_MODES else MODE_KEYWORD
        self.mode_text.set(restored_mode)
        self._on_mode_changed()
        self.keyword_entry.configure(state="normal")
        self.keyword_entry.delete("1.0", "end")
        self.keyword_entry.insert("1.0", "\n".join(session.keywords))
        self.min_price_text.set(
            "" if session.min_price is None else str(session.min_price)
        )
        self.max_price_text.set(
            "" if session.max_price is None else str(session.max_price)
        )
        self.restart_interval_text.set(str(session.restart_keyword_interval))
        self._resume_index = session.next_index
        keyword = session.keywords[session.next_index]
        self.current_keyword_text.set(
            f"将从 {session.next_index + 1}/{len(session.keywords)}：{keyword} 继续"
        )
        self._write_log(
            f"已恢复上次关键词列表和价格；将从第 {session.next_index + 1}/"
            f"{len(session.keywords)} 个关键词“{keyword}”继续"
        )

    def _on_mode_changed(self, _event: object | None = None) -> None:
        selected_mode = self.mode_text.get()
        keyword_mode = is_keyword_mode(selected_mode)
        mobile_keyword_mode = selected_mode == MODE_KEYWORD
        if hasattr(self, "recovery_test_button"):
            self.recovery_test_button.configure(state="normal" if (
                not self.running
                and not getattr(self, "_recovery_test_running", False)
            ) else "disabled")
        if not self._output_path_customized:
            self.output_path_text.set(
                str(default_output_directory(DATA_DIR, keyword_mode=keyword_mode))
            )
        controls_enabled = (
            keyword_mode
            and not self.running
            and not getattr(self, "_recovery_test_running", False)
            and not self.wifi_setup_running
            and not self.phone_setup_running
        )
        if hasattr(self, "keyword_entry"):
            self.keyword_entry.configure(
                state="normal" if controls_enabled else "disabled"
            )
        if hasattr(self, "connection_combo"):
            self.connection_combo.configure(
                state="readonly"
                if controls_enabled and mobile_keyword_mode
                else "disabled"
            )
            self.wifi_setup_button.configure(
                state="normal"
                if controls_enabled
                and mobile_keyword_mode
                and not self.wifi_setup_running
                else "disabled"
            )
            self.phone_setup_button.configure(
                state="normal"
                if controls_enabled and mobile_keyword_mode
                else "disabled"
            )
            self._on_connection_changed()
        if hasattr(self, "min_price_entry"):
            entry_state = "normal" if controls_enabled else "disabled"
            self.min_price_entry.configure(state=entry_state)
            self.max_price_entry.configure(state=entry_state)
            self.restart_interval_entry.configure(state=entry_state)
        if hasattr(self, "output_choose_button"):
            general_state = (
                "normal"
                if not self.running
                and not getattr(self, "_recovery_test_running", False)
                and not self.wifi_setup_running
                and not self.phone_setup_running
                else "disabled"
            )
            self.output_choose_button.configure(state=general_state)
            self.output_default_button.configure(state=general_state)
            self.cache_button.configure(state=general_state)
        if selected_mode == MODE_KEYWORD:
            self.restart_action_text.set("完整重启微信：每")
            self.description_text.set(
                "手机停在微信商品搜索结果页；每行填写一个关键词。软件将自动搜索、"
                "切到“商品”、勾选“好评多”、填写预设价格、确定、下滑到没有新商品后切换下一个；"
                "若筛选面板没有“好评多”，则直接跳过且不计失败；"
                "可自动识别并循环切换最多 6 个微信/微信分身；统一保存并排除直播卡片。"
            )
        elif selected_mode == MODE_DESKTOP_KEYWORD:
            self.restart_action_text.set("完整重开小程序：每")
            self.description_text.set(
                "使用已登录的电脑版微信直接批量搜索关键词，不需要连接手机。"
                "软件会自动进入“小店与卡包”搜索页、切到商品、选择“好评多”、"
                "填写价格并读取全部分页；没有“好评多”的关键词直接跳过，"
                "断点、失败重试、数据去重和每 1000 条分表规则与手机模式一致。"
            )
        else:
            self.restart_action_text.set("完整重启微信：每")
            self.description_text.set(
                "先点开始采集，再到电脑微信店铺点击“销量”排序并手动滚动到底；"
                "不会点击商品详情。"
            )

    def _on_connection_changed(self, _event: object | None = None) -> None:
        enabled = (
            self.mode_text.get() == MODE_KEYWORD
            and self.connection_text.get() == CONNECTION_WIFI
            and not self.running
            and not self.wifi_setup_running
            and not self.phone_setup_running
        )
        if hasattr(self, "wifi_address_entry"):
            self.wifi_address_entry.configure(state="normal" if enabled else "disabled")

    def _setup_wifi(self) -> None:
        if self.running or self.wifi_setup_running or self.phone_setup_running:
            return
        self.wifi_setup_running = True
        self.status_text.set("正在启用手机 WiFi 连接…")
        self._write_log("请保持数据线连接：正在切换 ADB 到 WiFi 模式")
        self._on_mode_changed()
        threading.Thread(target=self._setup_wifi_worker, daemon=True).start()

    def _setup_wifi_worker(self) -> None:
        try:
            address = configure_adb_wifi()
        except Exception as exc:
            self.events.put(("wifi_setup_error", str(exc)))
        else:
            self.events.put(("wifi_setup_done", address))

    def _setup_phone(self) -> None:
        if self.running or self.wifi_setup_running or self.phone_setup_running:
            return
        self.phone_setup_running = True
        self.start_button.configure(state="disabled")
        self.mode_combo.configure(state="disabled")
        self.status_text.set("正在配置新手机…")
        self._write_log(
            "请保持数据线连接并解锁手机：正在检查架构、Root 和 Frida Server"
        )
        self._on_mode_changed()
        threading.Thread(target=self._setup_phone_worker, daemon=True).start()

    def _setup_phone_worker(self) -> None:
        try:
            result = setup_frida_server_via_usb(
                log=lambda message: self.events.put(("log", message))
            )
        except Exception as exc:
            self.events.put(("phone_setup_error", str(exc)))
        else:
            self.events.put(("phone_setup_done", result))

    def _select_output_store(self) -> bool:
        if self.running or self._stopping:
            return False
        export_thread = getattr(self, "_export_thread", None)
        if export_thread is not None:
            export_thread.join()
        self.active_mode = self.mode_text.get()
        raw_output_dir = self.output_path_text.get().strip()
        if not raw_output_dir:
            messagebox.showwarning("缺少保存位置", "请先选择本批数据保存文件夹。")
            return False
        output_dir = Path(raw_output_dir).expanduser()
        try:
            self.store = ProductStore(output_dir)
        except OSError as exc:
            messagebox.showerror(
                "保存位置不可用",
                f"无法使用这个文件夹，请重新选择：\n{output_dir}\n\n{exc}",
            )
            return False
        self._checkpoint_store = KeywordCheckpointStore(
            self.store.output_dir / "keyword_checkpoint.json"
        )
        for item_id in self.table.get_children():
            self.table.delete(item_id)
        self._load_existing()
        self.count_text.set(f"已保存 {len(self.store)} 个商品")
        return True

    def _choose_output_directory(self) -> None:
        if self.running or self.wifi_setup_running or self.phone_setup_running:
            return
        current = Path(self.output_path_text.get().strip() or DATA_DIR)
        initial = current if current.is_dir() else current.parent
        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择本批数据保存文件夹",
            initialdir=str(initial),
            mustexist=True,
        )
        if not selected:
            return
        self._output_path_customized = True
        self.output_path_text.set(str(Path(selected).resolve()))
        if self._select_output_store():
            self._remember_output_directory()
            self._write_log(f"本批数据将保存到：{self.store.output_dir}")
            if is_keyword_mode(self.active_mode):
                self._restore_keyword_session()

    def _remember_output_directory(self) -> None:
        try:
            path = DATA_DIR / "last-output.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({
                "directory": self.output_path_text.get().strip() if self._output_path_customized else ""
            }, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            self._write_log("保存位置偏好暂未记住；商品数据和断点不受影响")

    def _use_default_output_directory(self) -> None:
        if self.running or self.wifi_setup_running or self.phone_setup_running:
            return
        self._output_path_customized = False
        self._remember_output_directory()
        keyword_mode = is_keyword_mode(self.mode_text.get())
        self.output_path_text.set(
            str(default_output_directory(DATA_DIR, keyword_mode=keyword_mode))
        )
        if self._select_output_store():
            self._write_log(f"已恢复默认保存位置：{self.store.output_dir}")
            if is_keyword_mode(self.active_mode):
                self._restore_keyword_session()

    def _keyword_input_value(self) -> str:
        return self.keyword_entry.get("1.0", "end-1c")

    def _start(self) -> None:
        if self.running or self._stopping or getattr(self, "_recovery_test_running", False):
            return
        if not self._select_output_store():
            return
        if (
            self.active_mode == MODE_KEYWORD
            and self.connection_text.get() == CONNECTION_WIFI
            and not self.wifi_address_text.get().strip()
        ):
            messagebox.showwarning(
                "缺少 WiFi 地址",
                "请先连接数据线并点击“通过 USB 启用 WiFi”，或手动填写手机地址。",
            )
            return
        if is_keyword_mode(self.active_mode):
            self._pending_keywords = parse_keyword_list(self._keyword_input_value())
            if not self._pending_keywords:
                messagebox.showwarning(
                    "缺少关键词",
                    "请在关键词列表中填写至少一个完整关键词，每行一个。",
                )
                return
            try:
                (
                    self._pending_min_price,
                    self._pending_max_price,
                ) = parse_price_range(
                    self.min_price_text.get(),
                    self.max_price_text.get(),
                )
            except ValueError as exc:
                messagebox.showwarning("价格区间有误", str(exc))
                return
            try:
                self._pending_restart_interval = parse_restart_interval(
                    self.restart_interval_text.get()
                )
            except ValueError as exc:
                messagebox.showwarning("微信重启间隔有误", str(exc))
                return
            self._pending_connection = self.connection_text.get()
            self._pending_wifi_address = self.wifi_address_text.get().strip()
            self._resume_index = self._checkpoint_store.resume_index(
                self._pending_keywords,
                min_price=self._pending_min_price,
                max_price=self._pending_max_price,
                mode=self.active_mode,
            )
            try:
                self._checkpoint_store.save_position(
                    self._pending_keywords,
                    self._resume_index,
                    min_price=self._pending_min_price,
                    max_price=self._pending_max_price,
                    restart_keyword_interval=self._pending_restart_interval,
                    mode=self.active_mode,
                )
            except OSError as exc:
                messagebox.showerror(
                    "无法保存采集进度",
                    f"无法写入断点文件，请检查数据目录权限：\n{exc}",
                )
                return
        self.running = True
        self._storage_failed = False
        self._final_export_failed = False
        self._last_export_at = time.monotonic()
        self._batch_finished = False
        self._automation_pause_reason = None
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.mode_combo.configure(state="disabled")
        self.keyword_entry.configure(state="disabled")
        self.min_price_entry.configure(state="disabled")
        self.max_price_entry.configure(state="disabled")
        self.restart_interval_entry.configure(state="disabled")
        self.connection_combo.configure(state="disabled")
        self.wifi_address_entry.configure(state="disabled")
        self.wifi_setup_button.configure(state="disabled")
        self.phone_setup_button.configure(state="disabled")
        self.output_choose_button.configure(state="disabled")
        self.output_default_button.configure(state="disabled")
        self.cache_button.configure(state="disabled")
        self.status_text.set("正在连接微信…")
        if is_keyword_mode(self.active_mode):
            if self._pending_min_price is None and self._pending_max_price is None:
                price_note = "价格不限"
            elif self._pending_min_price is None:
                price_note = f"最高价 {self._pending_max_price} 元"
            elif self._pending_max_price is None:
                price_note = f"最低价 {self._pending_min_price} 元"
            else:
                price_note = (
                    f"价格 {self._pending_min_price}～{self._pending_max_price} 元"
                )
            if self.active_mode == MODE_DESKTOP_KEYWORD:
                channel_note = "开始连接电脑版微信内部小店搜索页"
                restart_note = "完整重开小程序（不退出微信）"
            else:
                channel_note = "开始连接安卓微信搜索页"
                restart_note = "完整重启微信"
            self._write_log(
                f"{channel_note}；待采集关键词共 "
                f"{len(self._pending_keywords)} 个；{price_note}；"
                + (
                    f"每 {self._pending_restart_interval} 个关键词{restart_note}"
                    if self._pending_restart_interval > 0
                    else f"已关闭定时{restart_note}"
                )
            )
            if self._resume_index:
                resume_keyword = self._pending_keywords[self._resume_index]
                self.current_keyword_text.set(
                    f"将从 {self._resume_index + 1}/{len(self._pending_keywords)}："
                    f"{resume_keyword} 继续"
                )
                self._write_log(
                    f"检测到上次采集断点：从第 {self._resume_index + 1}/"
                    f"{len(self._pending_keywords)} 个关键词“{resume_keyword}”继续"
                )
        else:
            self._write_log("开始连接；请保持电脑微信小店商品列表处于打开状态")
        self._start_thread = threading.Thread(target=self._start_worker, daemon=True)
        self._start_thread.start()

    def _start_worker(self) -> None:
        if not self.running:
            return
        keyword_arguments = dict(
            keyword="",
            keywords=self._pending_keywords,
            on_products=lambda cards: self.events.put(("products", cards)),
            log=lambda message: self.events.put(("log", message)),
            on_ready=lambda: self.events.put(("ready", None)),
            on_keyword=lambda keyword: self.events.put(("keyword", keyword)),
            on_progress=lambda keyword, index, total, stage: self.events.put(
                ("keyword_progress", (keyword, index, total, stage))
            ),
            on_complete=lambda: self.events.put(("batch_complete", None)),
            on_error=lambda message: self.events.put(("automation_error", message)),
            on_keyword_finished=lambda keyword, index, total, outcome: (
                self.events.put(
                    (
                        "keyword_finished",
                        (keyword, index, total, outcome),
                    )
                )
            ),
            start_index=self._resume_index,
            min_price=self._pending_min_price,
            max_price=self._pending_max_price,
            restart_keyword_interval=self._pending_restart_interval,
        )
        if self.active_mode == MODE_KEYWORD:
            listener: (
                FridaShopListener | MobileKeywordListener | DesktopKeywordListener
            ) = MobileKeywordListener(
                **keyword_arguments,
                connection=self._pending_connection,
                device_address=self._pending_wifi_address,
            )
        elif self.active_mode == MODE_DESKTOP_KEYWORD:
            if self._desktop_backend is None:
                from .desktop_builtin import ResilientShopOpener
                self._desktop_backend = DesktopWechatBackend(
                    keyword_arguments["log"], host_state_path=CACHE_DIR / "desktop-host.json",
                    normal_shop_opener=ResilientShopOpener(),
                    on_recovery_status=lambda message: self.events.put(("recovery_status", message)),
                )
            listener = DesktopKeywordListener(**keyword_arguments, backend=self._desktop_backend)
        else:
            listener = FridaShopListener(
                on_products=lambda cards: self.events.put(("products", cards)),
                log=lambda message: self.events.put(("log", message)),
                on_ready=lambda: self.events.put(("ready", None)),
                debug_dir=CACHE_DIR / "debug",
            )
        self.listener = listener
        if not self.running:
            listener.stop()
            return
        try:
            listener.start()
        except Exception as exc:
            listener.stop()
            if self.running:
                self.events.put(("start_error", str(exc)))

    def _test_desktop_recovery(self) -> None:
        if self.running or self._stopping or getattr(self, "_recovery_test_running", False):
            return
        self._recovery_test_running = True
        self._recovery_test_cancel = threading.Event()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.mode_combo.configure(state="disabled")
        self._on_mode_changed()
        self.status_text.set("正在测试：关闭小程序→内置入口独立重开→关键词搜索页")
        self._write_log("自动恢复测试不搜索商品，不修改正式数据或断点；不退出微信、不移动鼠标")

        def worker() -> None:
            try:
                if self._desktop_backend is None:
                    from .desktop_builtin import ResilientShopOpener
                    self._desktop_backend = DesktopWechatBackend(
                        lambda message: self.events.put(("log", message)),
                        host_state_path=CACHE_DIR / "desktop-host.json",
                        normal_shop_opener=ResilientShopOpener(),
                        on_recovery_status=lambda message: self.events.put(("recovery_status", message)),
                    )
                self._desktop_backend.cancel_event = self._recovery_test_cancel
                self._desktop_backend.recover_search_runtime()
                self.events.put(("recovery_test_done", "自动恢复测试通过：已返回关键词搜索页；正式数据未修改"))
            except Exception as exc:
                self.events.put(("recovery_test_done", f"自动恢复测试未完成：{exc}"))
        self._start_thread = threading.Thread(target=worker, daemon=True)
        self._start_thread.start()

    def _stop(self) -> None:
        if getattr(self, "_recovery_test_running", False):
            self._recovery_test_cancel.set()
            if self._desktop_backend is not None:
                self._desktop_backend.pause()
            self.stop_button.configure(state="disabled")
            self.status_text.set("正在停止自动恢复测试…")
            return
        if self._stopping:
            return
        if not self.running and self.listener is None:
            return
        self._stopping = True
        self.running = False
        self.status_text.set("正在停止…")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self._stop_thread = threading.Thread(target=self._stop_worker, daemon=True)
        self._stop_thread.start()

    def _stop_worker(self) -> None:
        # A stop can arrive before the startup thread has published its listener.
        # Never enable Start until startup has exited and its resources are closed.
        if self.listener is not None:
            self.listener.stop()
        if self._start_thread is not None:
            self._start_thread.join()
            self._start_thread = None
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        # FIFO barrier: the GUI must commit all pages already received before
        # taking the final export snapshot and enabling Start again.
        self.events.put(("finalize_store", None))

    def _launch_store_export(self, *, final: bool = False) -> None:
        previous = getattr(self, "_export_thread", None)
        if previous is not None and previous.is_alive() and not final:
            return
        store = self.store
        self._last_export_at = time.monotonic()
        def export() -> None:
            if previous is not None:
                previous.join()
            succeeded = store.export_csv()
            self.events.put(("store_exported", (store, succeeded, store.csv_export_error, final)))
            if final:
                self.events.put(("stopped", None))
        self._export_thread = threading.Thread(target=export, daemon=True)
        self._export_thread.start()

    def _maybe_export_store(self) -> None:
        if (self.running and not self._stopping and self.store.needs_export
                and time.monotonic() - getattr(self, "_last_export_at", 0) >= 15):
            self._launch_store_export()

    def _storage_error(self, exc: OSError) -> None:
        if getattr(self, "_storage_failed", False):
            return
        self._storage_failed = True
        self._automation_pause_reason = f"数据保存失败，已保留原断点，未跳过关键词：{exc}"
        self._write_log(self._automation_pause_reason)
        if not getattr(self, "_closing", False):
            self._stop()

    def _process_storage_event(self, kind: str, payload: object) -> bool:
        if kind == "products":
            self._handle_products(payload if isinstance(payload, list) else [])
        elif kind == "keyword_finished":
            if isinstance(payload, tuple) and len(payload) == 4:
                keyword, index, total, outcome = payload
                try:
                    self._record_keyword_finished(str(keyword), int(index), int(total), str(outcome))
                except OSError as exc:
                    self._storage_error(exc)
        elif kind == "batch_complete":
            if not getattr(self, "_storage_failed", False):
                try:
                    self._checkpoint_store.clear()
                    self._batch_finished = True
                except OSError as exc:
                    self._storage_error(exc)
            if not getattr(self, "_closing", False):
                if self._batch_finished:
                    self.status_text.set("全部关键词完成，正在导出数据…")
                    self._write_log("关键词列表全部完成，正在保存并导出 CSV")
                self._stop()
        else:
            return False
        return True

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if self._process_storage_event(kind, payload):
                    continue
                if kind == "log":
                    self._write_log(str(payload))
                elif kind == "recovery_status":
                    if (self.running or getattr(self, "_recovery_test_running", False)) and not self._stopping:
                        self.status_text.set(str(payload))
                elif kind == "recovery_test_done":
                    self._recovery_test_running = False
                    self.stop_button.configure(state="disabled")
                    self.start_button.configure(state="normal")
                    self.mode_combo.configure(state="readonly")
                    self._on_mode_changed()
                    self.status_text.set(str(payload))
                    self._write_log(str(payload))
                elif kind == "ready":
                    if not self.running:
                        continue
                    if self.active_mode == MODE_KEYWORD:
                        self.status_text.set("自动采集中：准备第一个关键词")
                        self._write_log(
                            "手机自动化已就绪；将逐个搜索、切到“商品”、筛选并滚动到底"
                        )
                    elif self.active_mode == MODE_DESKTOP_KEYWORD:
                        self.status_text.set("电脑版微信自动采集中：准备第一个关键词")
                        self._write_log(
                            "电脑版微信通道已就绪；将逐个搜索、切到“商品”、"
                            "选择“好评多”并读取全部分页"
                        )
                    else:
                        self.status_text.set("监听中：请在微信点销量并手动滚动")
                        self._write_log(
                            "监听已开始：现在请在电脑微信点击“销量”，再手动滚动到底"
                        )
                elif kind == "keyword":
                    self._activate_keyword_store(str(payload))
                elif kind == "keyword_progress":
                    if isinstance(payload, tuple) and len(payload) == 4:
                        keyword, index, total, stage = payload
                        self.current_keyword_text.set(
                            f"当前 {index}/{total}：{keyword}（{stage}）"
                        )
                        self.status_text.set(
                            f"关键词 {index}/{total}：{stage}"
                        )
                elif kind == "automation_error":
                    if not self.running:
                        continue
                    message = str(payload)
                    self._automation_pause_reason = message
                    self.status_text.set("自动采集已暂停")
                    self._write_log("自动采集已暂停：" + message)
                    messagebox.showerror("自动采集已暂停", message)
                    self._stop()
                elif kind == "csv_export":
                    self._report_csv_export(bool(payload))
                elif kind == "finalize_store":
                    self._launch_store_export(final=True)
                elif kind == "store_exported":
                    store, succeeded, error, final = payload
                    if store is self.store:
                        self._report_csv_export(succeeded, error)
                        if final:
                            self._final_export_failed = not succeeded
                elif kind == "start_error":
                    if not self.running:
                        continue
                    self.running = False
                    self.listener = None
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.mode_combo.configure(state="readonly")
                    self._on_mode_changed()
                    self.status_text.set("启动失败")
                    self._write_log("启动失败：" + str(payload))
                    messagebox.showerror("启动失败", str(payload))
                elif kind == "wifi_setup_done":
                    self.wifi_setup_running = False
                    self.connection_text.set(CONNECTION_WIFI)
                    self.wifi_address_text.set(str(payload))
                    self.status_text.set("WiFi 连接已就绪")
                    self._write_log(
                        f"手机 WiFi 连接已就绪：{payload}；现在可以拔掉数据线"
                    )
                    self._on_mode_changed()
                    messagebox.showinfo(
                        "WiFi 连接成功",
                        f"手机地址：{payload}\n现在可以拔掉数据线，再点击“开始采集”。",
                    )
                elif kind == "wifi_setup_error":
                    self.wifi_setup_running = False
                    self.status_text.set("WiFi 启用失败")
                    self._write_log("WiFi 启用失败：" + str(payload))
                    self._on_mode_changed()
                    messagebox.showerror("WiFi 启用失败", str(payload))
                elif kind == "phone_setup_done":
                    self.phone_setup_running = False
                    self.start_button.configure(state="normal")
                    self.mode_combo.configure(state="readonly")
                    self.status_text.set("新手机配置完成")
                    self._write_log("一键配置完成：" + str(payload))
                    self._on_mode_changed()
                    messagebox.showinfo(
                        "新手机配置完成",
                        f"{payload}\n\n现在可以打开微信搜索结果页并开始采集。",
                    )
                elif kind == "phone_setup_error":
                    self.phone_setup_running = False
                    self.start_button.configure(state="normal")
                    self.mode_combo.configure(state="readonly")
                    self.status_text.set("新手机配置失败")
                    self._write_log("一键配置失败：" + str(payload))
                    self._on_mode_changed()
                    messagebox.showerror("新手机配置失败", str(payload))
                elif kind == "stopped":
                    self._stopping = False
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.mode_combo.configure(state="readonly")
                    self._on_mode_changed()
                    status, log_message = stopped_status(
                        self._batch_finished,
                        self._automation_pause_reason,
                    )
                    if getattr(self, "_final_export_failed", False):
                        status += "（商品已增量保存，JSON/CSV待更新）"
                        log_message = "采集已停止；商品增量文件已保存，JSON/CSV尚未全部更新，请保留整个数据目录"
                    self.status_text.set(status)
                    self._write_log(log_message)
        except queue.Empty:
            pass
        self._maybe_export_store()
        if self.root.winfo_exists():
            self.root.after(150, self._drain_events)

    def _activate_keyword_store(self, keyword: str) -> None:
        keyword = keyword.strip()
        if not keyword:
            return
        self.current_keyword_text.set(f"当前关键词：{keyword}")
        self._write_log(f"当前已确认关键词：{keyword}")

    def _record_keyword_finished(
        self,
        keyword: str,
        index: int,
        total: int,
        outcome: str,
    ) -> None:
        if getattr(self, "_storage_failed", False):
            return
        completed_index = index - 1
        self._checkpoint_store.mark_finished(
            self._pending_keywords,
            completed_index=completed_index,
            min_price=self._pending_min_price,
            max_price=self._pending_max_price,
            restart_keyword_interval=self._pending_restart_interval,
            mode=getattr(self, "active_mode", MODE_KEYWORD),
        )
        self._write_log(
            f"关键词进度已保存：{index}/{total} “{keyword}”（{outcome}）"
        )

    def _handle_products(self, cards: list[ProductCard]) -> None:
        try:
            changed = self.store.upsert_many(cards)
        except OSError as exc:
            self._storage_error(exc)
            return
        else:
            suffix = "（CSV待更新）" if self.store.csv_export_error else ""
            self.count_text.set(f"已保存 {len(self.store)} 个商品{suffix}")
        for card in changed:
            self._upsert_table(card)
        self._show_latest(changed)

    def _report_csv_export(self, succeeded: bool, error: str | None = None) -> None:
        error = error or self.store.csv_export_error
        if succeeded:
            if self._last_csv_export_error:
                self._write_log("CSV 已恢复更新")
            self._last_csv_export_error = None
            return
        if error and error != self._last_csv_export_error:
            self._write_log(
                "JSON/CSV 暂时无法更新，请检查磁盘空间及文件是否被 WPS/Excel 占用；"
                "商品已增量落盘，重启可恢复。请保留 products.pending.sqlite3；"
                f"定期导出或停止时会重试。详情：{error}"
            )
        self._last_csv_export_error = error
        if self.running:
            self.status_text.set("采集中：商品已增量保存，JSON/CSV待更新")

    def _upsert_table(self, card: ProductCard) -> None:
        values = (
            card.product_id,
            card.keyword,
            card.shop_name,
            card.title,
            card.display_price,
            card.display_sales,
            card.main_image_url,
        )
        if self.table.exists(card.product_id):
            self.table.item(card.product_id, values=values)
        else:
            self.table.insert("", "end", iid=card.product_id, values=values)

    def _show_latest(self, cards: list[ProductCard]) -> None:
        if not cards:
            return
        latest_id = cards[-1].product_id
        if self.table.exists(latest_id):
            self.table.see(latest_id)

    def _write_log(self, message: str) -> None:
        runtime_log = getattr(self, "_runtime_log", None)
        if runtime_log is not None:
            runtime_log.info(message)
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message.rstrip() + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _open_data_dir(self) -> None:
        output_dir = Path(self.output_path_text.get().strip() or self.store.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(output_dir)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc))

    def _clear_cache(self) -> None:
        if self.running or self.wifi_setup_running or self.phone_setup_running:
            messagebox.showwarning("正在运行", "请先停止采集，再清理软件缓存。")
            return
        file_count, total_bytes = cache_usage(CACHE_DIR)
        if file_count == 0:
            messagebox.showinfo(
                "无需清理",
                "软件缓存已经是空的。商品数据、断点、卡密和保存位置均未改动。",
            )
            return
        confirmed = messagebox.askyesno(
            "确认清理软件缓存",
            f"将删除 {file_count} 个调试缓存文件，共 {format_bytes(total_bytes)}。\n\n"
            "不会删除商品 CSV/JSON、关键词断点、卡密或你选择的保存位置。\n"
            "确定继续吗？",
        )
        if not confirmed:
            return
        try:
            removed_files, removed_bytes = clear_runtime_cache(CACHE_DIR)
        except OSError as exc:
            messagebox.showerror("缓存清理失败", str(exc))
            return
        self._write_log(
            f"软件缓存已清理：{removed_files} 个文件，{format_bytes(removed_bytes)}"
        )
        messagebox.showinfo(
            "清理完成",
            f"已清理 {removed_files} 个缓存文件，共 {format_bytes(removed_bytes)}。\n"
            "商品数据和采集断点未删除。",
        )

    def _on_close(self) -> None:
        self._closing = True
        if getattr(self, "_recovery_test_running", False):
            self._recovery_test_cancel.set()
            if self._desktop_backend is not None:
                self._desktop_backend.pause()
        self.running = False
        if self.listener is not None:
            self.listener.stop()
        if self._start_thread is not None:
            self._start_thread.join()
        if self.listener is not None:
            self.listener.stop()
        if self._desktop_backend is not None:
            self._desktop_backend.stop()
        # Producers are stopped. Do not discard pages still queued for Tk, or
        # checkpoint a keyword before its preceding product events are durable.
        stop_thread = getattr(self, "_stop_thread", None)
        if stop_thread is not None:
            stop_thread.join()
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._process_storage_event(kind, payload)
        except queue.Empty:
            pass
        export_thread = getattr(self, "_export_thread", None)
        if export_thread is not None:
            export_thread.join()
        if not self.store.export_csv():
            self._report_csv_export(False)
        runtime_log = getattr(self, "_runtime_log", None)
        if runtime_log is not None:
            for handler in runtime_log.handlers[:]:
                handler.close()
                runtime_log.removeHandler(handler)
        self.root.destroy()


def run_gui() -> None:
    prepare_frozen_tk_runtime()
    root = tk.Tk()

    def open_scraper(authorization: LicenseAuthorization) -> None:
        ShopScraperApp(root, authorization)

    LicenseGate(root, open_scraper)
    root.mainloop()
