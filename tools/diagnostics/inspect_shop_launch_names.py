"""Read-only PE strings for shop/applet launch command discovery.

Never loads a DLL, reads chat/account files, attaches, or changes native state.
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
data = Path(sys.argv[1]).read_bytes()
seen = set()
for match in re.finditer(rb"[\x20-\x7e]{8,240}\x00", data):
    value = match.group()[:-1].decode("ascii")
    lower = value.lower()
    relevant = ("wechat_call_xweb" in lower or
                (any(word in lower for word in ("shop", "applet", "wxa")) and
                 any(word in lower for word in ("launch", "shortcut", "open_shop"))))
    if relevant and value not in seen:
        print(hex(match.start()), value)
        seen.add(value)
