"""Print specific public-page contracts from extracted bytecode; never execute it."""
import dis
import marshal
from pathlib import Path
import sys

sys.stdout.reconfigure(encoding='utf-8')
root = Path(__file__).resolve().parents[2] / '.tmp-zgn-modules'
for filename, names in (
    ('wechat_xweb_backend.pyc', {'SHOP_STATE_JS'}),
    ('auto_collect.pyc', {'DEFAULT_BOOTSTRAP_TOKEN'}),
):
    code = marshal.loads((root / filename).read_bytes())
    previous = None
    for instruction in dis.get_instructions(code):
        if instruction.opname in ('STORE_NAME', 'STORE_GLOBAL') and instruction.argval in names:
            if previous and previous.opname == 'LOAD_CONST':
                print(instruction.argval, previous.argval)
        if instruction.opname not in ('CACHE', 'NOP', 'NOT_TAKEN'):
            previous = instruction
