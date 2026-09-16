"""Read only static launch/shortcut strings; never load the inspected DLL."""
from pathlib import Path
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
path = Path(sys.argv[1])
data = path.read_bytes()
needles = (
    b'launchapplet', b'applet_launch_appid', b'createShortcut', b'CreateShortcut',
    b'addShortcut', b'AddShortcut', b'DesktopShortcut', b'desktopShortcut',
    b'CreateDesktop', b'create_desktop', b'add_to_desktop', b'AddToDesktop',
    b'shortcut_arguments', b'--applet', b'-applet',
)
found = set()
for needle in needles:
    offset = 0
    while (offset := data.find(needle, offset)) >= 0:
        left = max(data.rfind(b'\0', max(0, offset - 180), offset) + 1, offset - 180)
        end = data.find(b'\0', offset, offset + 360)
        if end < 0:
            end = offset + 180
        raw = data[left:end]
        strings = re.findall(rb'[\x20-\x7e]{5,}', raw)
        for value in strings:
            if needle in value and value not in found:
                print(hex(left), value.decode('ascii'), flush=True)
                found.add(value)
        offset += len(needle)
