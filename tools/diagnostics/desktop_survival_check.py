"""Supervise the smoke test and check original processes AFTER controller exit."""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wechat_shop_scraper.desktop_keyword import _visible_weixin_identity, _wmpf_root_pid


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    main_pid, _ = _visible_weixin_identity()
    wmpf_pid = _wmpf_root_pid(main_pid)
    if not main_pid or not wmpf_pid:
        raise RuntimeError("Need existing logged-in WeChat and mini-app host")
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handles = {pid: kernel.OpenProcess(0x100000, False, pid) for pid in (main_pid, wmpf_pid)}
    if not all(handles.values()):
        raise RuntimeError("Cannot monitor original process handles")
    print(f"MONITOR_ORIGINAL weixin={main_pid} wmpf={wmpf_pid}", flush=True)
    try:
        script_name = ("desktop_v7_smoke.py" if "--v7" in sys.argv else
                       "desktop_v6_smoke.py" if "--v6" in sys.argv else "desktop_restart_smoke.py")
        child_args = [arg for arg in sys.argv[1:] if arg not in {"--v6", "--v7"}]
        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name(script_name)), *child_args],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace",
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in child.stdout:
            print(line, end="", flush=True)
        child.wait()
        print(f"COLLECTOR_EXITED code={child.returncode}; checking 60s for delayed crash", flush=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            for pid, handle in handles.items():
                if kernel.WaitForSingleObject(handle, 0) != 258:
                    print(f"FAIL original pid={pid} exited", flush=True)
                    return 1
            time.sleep(0.5)
        print("PASS original WeChat and mini-app host survived collector exit for 60s", flush=True)
        from wechat_shop_scraper.desktop_keyword import _powershell_json
        rows = _powershell_json(
            f"Get-Process -Id {main_pid},{wmpf_pid} -ErrorAction SilentlyContinue | Select-Object Id,Responding"
        )
        if not isinstance(rows, list) or len(rows) != 2 or not all(row.get("Responding") for row in rows):
            print("FAIL original processes survived but are not responding", flush=True)
            return 1
        print("PASS both original processes still respond after the 60s delay", flush=True)
        return child.returncode
    finally:
        for handle in handles.values():
            kernel.CloseHandle(handle)


if __name__ == "__main__":
    raise SystemExit(main())
