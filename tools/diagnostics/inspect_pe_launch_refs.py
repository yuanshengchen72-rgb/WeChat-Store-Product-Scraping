"""Static-only string/function xrefs for the normal shop launch path."""
import argparse
from bisect import bisect_right
from pathlib import Path
import re
import struct
import sys

import pefile

parser = argparse.ArgumentParser()
parser.add_argument('dll', type=Path)
parser.add_argument('--strings')
parser.add_argument('--refs', nargs='*', default=[])
parser.add_argument('--calls', nargs='*', default=[])
args = parser.parse_args()
sys.stdout.reconfigure(encoding='utf-8')
pe = pefile.PE(str(args.dll), fast_load=True)
unwind = next(s.get_data() for s in pe.sections if s.Name.startswith(b'.pdata'))
entries = list(struct.iter_unpack('<III', unwind[:len(unwind)//12*12]))
starts = [row[0] for row in entries]
def owner(rva):
    index = bisect_right(starts, rva)-1
    if index >= 0 and entries[index][0] <= rva < entries[index][1]:
        return [hex(entries[index][0]), hex(entries[index][1])]
    return []

targets = {int(rva, 0): 'RVA' for rva in args.refs}
if args.strings:
    regex = re.compile(args.strings, re.I)
    for match in re.finditer(rb'[\x20-\x7e]{5,400}\x00', pe.__data__):
        value = match.group()[:-1].decode('ascii')
        if regex.search(value):
            rva = pe.get_rva_from_offset(match.start())
            print('STRING', hex(rva), value)
            targets[rva] = value
calls = {int(value, 0) for value in args.calls}
for section in pe.sections:
    if not section.Characteristics & 0x20000000:
        continue
    raw = section.get_data()
    # Candidate RIP-relative references; inspect printed owning functions with
    # capstone before using any site for a read-only runtime observation.
    for match in re.finditer(rb'(?:[\x48\x4c][\x8b\x8d\x89\x3b]|\x0f[\x10\x28])[\x05\x0d\x15\x1d\x25\x2d\x35\x3d].{4}', raw, re.S):
        rva = section.VirtualAddress + match.start()
        target = rva + 7 + struct.unpack_from('<i', raw, match.start()+3)[0]
        if target in targets:
            print('REF', hex(rva), 'TO', hex(target), 'FUNCTION', owner(rva), targets[target])
    if calls:
        for match in re.finditer(rb'(?=(\xe8.{4}))', raw, re.S):
            rva = section.VirtualAddress + match.start()
            target = rva + 5 + struct.unpack_from('<i', raw, match.start()+1)[0]
            if target in calls and owner(rva):
                print('CALL', hex(rva), 'TO', hex(target), 'FUNCTION', owner(rva))
