"""Static PE comparison; never load or execute the inspected DLLs."""
from pathlib import Path
import hashlib
import json
import struct
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / '.tools/diagnostic-libs'))
import pefile
import capstone
from wechat_shop_scraper.desktop_keyword import SUPPORTED_PROFILE


def digest(data):
    return hashlib.sha256(data).hexdigest()


def functions(pe):
    data = next(s.get_data() for s in pe.sections if s.Name.startswith(b'.pdata'))
    return list(struct.iter_unpack('<III', data[:len(data) // 12 * 12]))


def function_shape(decoder, data, start):
    """Mask relocated external targets, not constants/fields/internal branches."""
    normalized = bytearray(data)
    refs = []
    for instruction in decoder.disasm(data, start):
        position = instruction.address - start
        for operand in instruction.operands:
            if operand.type == capstone.x86.X86_OP_MEM and operand.mem.base == capstone.x86.X86_REG_RIP:
                at = position + instruction.disp_offset
                normalized[at:at + instruction.disp_size] = b'\0' * instruction.disp_size
                refs.append(('rip', position, instruction.address + instruction.size + operand.mem.disp))
            if (operand.type == capstone.x86.X86_OP_IMM
                    and (instruction.group(capstone.CS_GRP_CALL) or instruction.group(capstone.CS_GRP_JUMP))
                    and not start <= operand.imm < start + len(data)):
                at = position + instruction.imm_offset
                normalized[at:at + instruction.imm_size] = b'\0' * instruction.imm_size
                refs.append(('branch', position, operand.imm))
    return bytes(normalized), refs


def inspect_pair(old_path, new_path, offsets):
    old = pefile.PE(str(old_path), fast_load=True)
    new = pefile.PE(str(new_path), fast_load=True)
    result = {'old': str(old_path), 'new': str(new_path), 'sections': [], 'interfaces': []}
    for section in old.sections:
        peer = next((s for s in new.sections if s.Name == section.Name), None)
        before = section.get_data()
        after = peer.get_data() if peer else b''
        result['sections'].append({
            'name': section.Name.rstrip(b'\0').decode(),
            'old_rva': hex(section.VirtualAddress), 'new_rva': hex(peer.VirtualAddress) if peer else None,
            'old_size': len(before), 'new_size': len(after),
            'identical': before == after, 'old_sha256': digest(before), 'new_sha256': digest(after),
        })
    old_functions = functions(old)
    new_functions = functions(new)
    old_bounds = {row[0]: row[1] for row in old_functions}
    new_bounds = {row[0]: row[1] for row in new_functions}
    decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    decoder.detail = True
    for name, address in offsets.items():
        start, end, _ = next(row for row in old_functions if row[0] <= address < row[1])
        data = old.get_data(start, end - start)
        shape, refs = function_shape(decoder, data, start)
        shapes = []
        for candidate_start, candidate_end, _ in new_functions:
            if candidate_end - candidate_start != end - start:
                continue
            candidate_data = new.get_data(candidate_start, candidate_end - candidate_start)
            # Exact ABI/prologue anchor cheaply excludes unrelated functions.
            if candidate_data[:16] != data[:16]:
                continue
            candidate_shape, new_refs = function_shape(decoder, candidate_data, candidate_start)
            if candidate_shape == shape:
                ref_evidence = []
                for old_ref, new_ref in zip(refs, new_refs):
                    old_content = old.get_data(old_ref[2], 64)
                    new_content = new.get_data(new_ref[2], 64)
                    item = {'kind': old_ref[0], 'instruction_offset': old_ref[1],
                                         'old_target': hex(old_ref[2]), 'new_target': hex(new_ref[2]),
                                         'target_64_bytes_equal': old_content == new_content}
                    if old_ref[0] == 'branch' and old_ref[2] in old_bounds and new_ref[2] in new_bounds:
                        old_code = old.get_data(old_ref[2], old_bounds[old_ref[2]] - old_ref[2])
                        new_code = new.get_data(new_ref[2], new_bounds[new_ref[2]] - new_ref[2])
                        item['callee_shape_equal'] = (function_shape(decoder, old_code, old_ref[2])[0]
                                                      == function_shape(decoder, new_code, new_ref[2])[0])
                    if old_ref[0] == 'rip':
                        before = old_content.split(b'\0')[0]
                        after = new_content.split(b'\0')[0]
                        if before and all(32 <= c < 127 for c in before):
                            item['old_string'] = before.decode('ascii')
                            item['new_string_equal'] = before == after
                    ref_evidence.append(item)
                shapes.append({'rva': hex(candidate_start), 'function_sha256': digest(candidate_data),
                               'normalized_sha256': digest(shape), 'refs': ref_evidence})
        peer = next((row for row in new_functions if row[0] <= address < row[1]), None)
        matches = []
        for section in new.sections:
            if not section.Characteristics & 0x20000000:
                continue
            buffer = section.get_data()
            at = buffer.find(data)
            while at >= 0:
                matches.append(hex(section.VirtualAddress + at))
                at = buffer.find(data, at + 1)
        result['interfaces'].append({
            'name': name, 'old_rva': hex(address), 'old_function': [hex(start), hex(end)],
            'new_function_at_old_rva': [hex(peer[0]), hex(peer[1])] if peer else None,
            'same_bytes_at_old_rva': new.get_data(start, end - start) == data,
            'exact_function_matches': matches,
            'normalized_function_matches': shapes,
            'instructions': [f'{i.address:x}: {i.mnemonic} {i.op_str}' for i in decoder.disasm(data, start)][:18],
        })
    return result


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('old_weixin', type=Path)
    parser.add_argument('new_weixin', type=Path)
    parser.add_argument('old_flue', type=Path)
    parser.add_argument('new_flue', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = [inspect_pair(args.old_weixin, args.new_weixin, {
        'ipc_task': SUPPORTED_PROFILE.weixin_ipc_task_offset,
    }), inspect_pair(args.old_flue, args.new_flue, {
        'on_command': SUPPORTED_PROFILE.wmpf_on_command_offset,
        'reply': SUPPORTED_PROFILE.wmpf_reply_offset,
        'load_start': SUPPORTED_PROFILE.wmpf_load_start_offset,
        'cdp_filter': SUPPORTED_PROFILE.wmpf_cdp_filter_offset,
    })]
    if args.report:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps([{'module': Path(item['new']).name, 'interfaces': [
        {'name': interface['name'], 'old_rva': interface['old_rva'], 'matches': [
            {k: v for k, v in match.items() if k != 'refs'} | {
                'unmatched_callees': [ref for ref in match['refs'] if ref.get('callee_shape_equal') is False],
                'strings': [ref for ref in match['refs'] if 'old_string' in ref],
            } for match in interface['normalized_function_matches']
        ]} for interface in item['interfaces']
    ]} for item in result], ensure_ascii=False, indent=2))
