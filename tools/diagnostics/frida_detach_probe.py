"""Reproduce agent detach in our own disposable Python child, never WeChat."""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import typing

from typing_extensions import NotRequired

if not hasattr(typing, "NotRequired"):
    typing.NotRequired = NotRequired
import frida


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", choices=["qjs", "v8"], default="qjs")
    parser.add_argument("--wait", type=float, default=45)
    parser.add_argument("--client-exit", action="store_true")
    parser.add_argument("--worker-pid", type=int)
    args = parser.parse_args()
    if args.worker_pid:
        session = frida.get_local_device().attach(args.worker_pid)
        for _ in range(2):
            script = session.create_script("rpc.exports = { ping() { return Process.id; } };", runtime=args.runtime)
            script.load()
            assert script.exports_sync.ping() == args.worker_pid
            script.unload()
        session.detach()
        return 0
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(180)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    session = None
    try:
        print(f"OWN_CHILD pid={child.pid} frida={frida.__version__} runtime={args.runtime}", flush=True)
        if args.client_exit:
            subprocess.run([sys.executable, __file__, "--worker-pid", str(child.pid),
                            "--runtime", args.runtime], check=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            print("CONTROLLER EXITED", flush=True)
        else:
            session = frida.get_local_device().attach(child.pid)
            script = session.create_script("rpc.exports = { ping() { return Process.id; } };", runtime=args.runtime)
            script.load()
            assert script.exports_sync.ping() == child.pid
            script.unload()
            session.detach()
            session = None
        print("DETACHED; checking for delayed crash", flush=True)
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            code = child.poll()
            if code is not None:
                print(f"FAIL own child exited code={code} hex=0x{code & 0xffffffff:08x}", flush=True)
                return 1
            time.sleep(0.25)
        print(f"PASS own child survived {args.wait:g}s after detach", flush=True)
        return 0
    finally:
        if session is not None:
            session.detach()
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
