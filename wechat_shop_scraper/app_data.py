from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


CACHE_DIRECTORY_NAME = ".cache"


def prepare_frozen_tk_runtime(
    internal_dir: str | Path | None = None,
    storage_root: str | Path | None = None,
) -> Path | None:
    """Put Tcl/Tk scripts on a stable path that works from Chinese folders."""

    if internal_dir is None:
        if not getattr(sys, "frozen", False):
            return None
        internal_dir = getattr(sys, "_MEIPASS", Path(sys.executable).parent)

    source_root = Path(internal_dir)
    source_tcl = source_root / "_tcl_data"
    source_tk = source_root / "_tk_data"
    if not (source_tcl / "init.tcl").is_file() or not (
        source_tk / "tk.tcl"
    ).is_file():
        return None

    if storage_root is None:
        local_app_data = os.environ.get("LOCALAPPDATA")
        storage_root = (
            Path(local_app_data) / "WeChatShopScraper"
            if local_app_data
            else Path(tempfile.gettempdir()) / "WeChatShopScraper"
        )
    target_root = Path(storage_root) / "tk-runtime-8.6"
    target_tcl = target_root / "tcl"
    target_tk = target_root / "tk"
    marker = target_root / ".complete"
    signature = (
        f"{(source_tcl / 'init.tcl').stat().st_size}:"
        f"{(source_tk / 'tk.tcl').stat().st_size}"
    )
    try:
        complete = marker.read_text(encoding="ascii") == signature
    except (OSError, UnicodeError):
        complete = False
    if not complete:
        target_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_tcl, target_tcl, dirs_exist_ok=True)
        shutil.copytree(source_tk, target_tk, dirs_exist_ok=True)
        marker.write_text(signature, encoding="ascii")

    os.environ["TCL_LIBRARY"] = str(target_tcl)
    os.environ["TK_LIBRARY"] = str(target_tk)
    return target_root


def default_output_directory(data_dir: str | Path, *, keyword_mode: bool) -> Path:
    root = Path(data_dir)
    return root / "关键词采集汇总" if keyword_mode else root


def cache_usage(cache_dir: str | Path) -> tuple[int, int]:
    path = Path(cache_dir)
    if not path.is_dir():
        return 0, 0

    file_count = 0
    total_bytes = 0
    for root, directory_names, file_names in os.walk(path, followlinks=False):
        root_path = Path(root)
        for directory_name in directory_names:
            child = root_path / directory_name
            if child.is_symlink():
                file_count += 1
                try:
                    total_bytes += child.lstat().st_size
                except OSError:
                    pass
        for file_name in file_names:
            child = root_path / file_name
            file_count += 1
            try:
                total_bytes += child.stat().st_size
            except OSError:
                pass
    return file_count, total_bytes


def clear_runtime_cache(cache_dir: str | Path) -> tuple[int, int]:
    """Clear only the contents of the app-owned ``.cache`` directory."""

    path = Path(cache_dir)
    if path.name != CACHE_DIRECTORY_NAME:
        raise ValueError("只允许清理软件专用的 .cache 目录")

    usage = cache_usage(path)
    if not path.is_dir():
        return usage

    for child in path.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            shutil.rmtree(child)
    return usage


def format_bytes(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
