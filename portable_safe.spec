# -*- mode: python ; coding: utf-8 -*-

# V15 stable rebuild: same portable payload as portable.spec, but explicitly
# keep Tk because the bundled runtime's PyInstaller hook cannot discover it
# when its base interpreter is the Codex Python 3.12 runtime.
import typing
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all
from typing_extensions import NotRequired

if not hasattr(typing, "NotRequired"):
    typing.NotRequired = NotRequired

bootstrap_vendor = str(Path(SPECPATH) / ".tools" / "native-bootstrap")
if Path(bootstrap_vendor).is_dir():
    sys.path.insert(0, bootstrap_vendor)
frida_datas, frida_binaries, frida_hiddenimports = collect_all("frida")
ui_datas, ui_binaries, ui_hiddenimports = collect_all("uiautomation")
com_datas, com_binaries, com_hiddenimports = collect_all("comtypes")
mobile_datas = [
    ("wechat_shop_scraper/assets/mobile_search_automation.bundle.js", "wechat_shop_scraper/assets"),
    ("wechat_shop_scraper/assets/launcher_user_switch.bundle.js", "wechat_shop_scraper/assets"),
    ("wechat_shop_scraper/assets/native_ui_locator.bundle.js", "wechat_shop_scraper/assets"),
    ("wechat_shop_scraper/assets/frida-server-17.17.0-android-arm64", "wechat_shop_scraper/assets"),
    ("wechat_shop_scraper/assets/platform-tools/adb.exe", "wechat_shop_scraper/assets/platform-tools"),
    ("wechat_shop_scraper/assets/platform-tools/AdbWinApi.dll", "wechat_shop_scraper/assets/platform-tools"),
    ("wechat_shop_scraper/assets/platform-tools/AdbWinUsbApi.dll", "wechat_shop_scraper/assets/platform-tools"),
]
python_root = Path(sys.base_prefix)
tk_tcl = python_root / "tcl" / "tcl8.6"
tk_tk = python_root / "tcl" / "tk8.6"
if not tk_tcl.is_dir() or not tk_tk.is_dir():
    raise SystemExit(f"Python Tcl/Tk runtime not found under {python_root / 'tcl'}")
tk_datas = [(str(tk_tcl), "_tcl_data"), (str(tk_tk), "_tk_data")]
if (python_root / "_tcl_data").is_dir():
    tk_datas.append((str(python_root / "_tcl_data"), "_tcl_data"))
if (python_root / "_tk_data").is_dir():
    tk_datas.append((str(python_root / "_tk_data"), "_tk_data"))
tk_package = python_root / "Lib" / "tkinter"
if tk_package.is_dir():
    tk_datas.append((str(tk_package), "tkinter"))

stdlib_root = python_root / "Lib"
a = Analysis(
    ["main.py"],
    pathex=[bootstrap_vendor, str(stdlib_root)],
    binaries=frida_binaries + ui_binaries + com_binaries,
    datas=frida_datas + mobile_datas + ui_datas + com_datas + tk_datas,
    hiddenimports=(frida_hiddenimports + ui_hiddenimports + com_hiddenimports +
                   ["tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox", "_tkinter"]),
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="微信小店商品列表采集", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, upx_exclude=[], console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[],
    name="微信小店商品列表采集",
)
