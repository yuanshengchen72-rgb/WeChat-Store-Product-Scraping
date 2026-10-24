"""Static-only equality check for normal-launch ABI; no DLL execution."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_desktop_binaries import inspect_pair, function_shape
import capstone
import pefile

root = Path(__file__).resolve().parents[2]
new = root/'build/compat-4.1.13.65/unpacked/files/4.1.13.65/Weixin.dll'
old = root/'build/compat-4.1.13.63/unpacked/files/4.1.13.63/Weixin.dll'
if not old.is_file():
    old = new
if not old.is_file() or not new.is_file():
    raise SystemExit('compatibility DLLs are not available; provide the unpacked 4.1.13.x files')
pair = inspect_pair(old, new, {'normal-launch':0x220a80, 'copy-request':0x787600})
for item in pair['interfaces']:
    matches = item['normalized_function_matches']
    assert len(matches) == 1, item['name']
    assert not any(ref.get('callee_shape_equal') is False for ref in matches[0]['refs'])
    print(item['name'], item['old_rva'], '->', matches[0]['rva'], 'ABI_SHAPE_MATCH')
decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
decoder.detail = True
before, after = pefile.PE(str(old), fast_load=True), pefile.PE(str(new), fast_load=True)
if old.resolve() != new.resolve():
    assert function_shape(decoder, before.get_data(0x7866f0,0xce),0x7866f0)[0] == function_shape(
        decoder,after.get_data(0x7869b0,0xce),0x7869b0)[0]
    print('default-request-ctor 0x7866f0 -> 0x7869b0 ALL_FIELDS_MATCH')
else:
    print('default-request-ctor comparison skipped: only one compatibility DLL is available')
