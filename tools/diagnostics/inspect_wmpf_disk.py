"""Static disassembly only: never load the DLL or attach to any process."""
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".tools/diagnostic-libs"))
import capstone
import pefile

sys.stdout.reconfigure(encoding="utf-8")
path = Path(sys.argv[1])
address = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x19b887b
pe = pefile.PE(str(path), fast_load=True)
unwind = next(section.get_data() for section in pe.sections if section.Name.startswith(b".pdata"))
if len(sys.argv) > 3:
    start, end = address, int(sys.argv[3], 0)
else:
    start, end, _ = next(row for row in struct.iter_unpack("<III", unwind[:len(unwind)//12*12])
                         if row[0] <= address < row[1])
print(f"FUNCTION {start:x}..{end:x} fault={address:x}")
decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
decoder.detail = True
for instruction in decoder.disasm(pe.get_data(start, end - start), start):
    notes = []
    for operand in instruction.operands:
        if operand.type == capstone.x86.X86_OP_MEM and operand.mem.base == capstone.x86.X86_REG_RIP:
            target = instruction.address + instruction.size + operand.mem.disp
            content = pe.get_data(target, 220).split(b"\0")[0]
            if len(content) > 3 and all(32 <= byte < 127 for byte in content):
                notes.append(content.decode("ascii"))
    print(f"{instruction.address:x}: {instruction.mnemonic} {instruction.op_str}", " | ".join(notes))
