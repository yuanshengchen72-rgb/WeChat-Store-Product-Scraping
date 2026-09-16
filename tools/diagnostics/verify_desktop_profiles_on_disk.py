"""Validate registered profiles against real signed DLLs, without executing them."""
import argparse
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pefile
from wechat_shop_scraper.desktop_keyword import validate_desktop_profile


def entries(path):
    pe = pefile.PE(str(path), fast_load=True)
    pdata = next(section.get_data() for section in pe.sections if section.Name.startswith(b'.pdata'))
    return {row[0] for row in struct.iter_unpack('<III', pdata[:len(pdata) // 12 * 12])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('old_weixin', type=Path)
    parser.add_argument('new_weixin', type=Path)
    parser.add_argument('old_flue', type=Path)
    parser.add_argument('new_flue', type=Path)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    rows = []
    for main in (args.old_weixin, args.new_weixin):
        for flue in (args.old_flue, args.new_flue):
            profile = validate_desktop_profile(main, flue)
            assert profile.weixin_ipc_task_offset in entries(main), 'IPC must point to a function start'
            starts = entries(flue)
            for attribute in ('wmpf_on_command_offset', 'wmpf_reply_offset',
                              'wmpf_load_start_offset', 'wmpf_cdp_filter_offset'):
                assert getattr(profile, attribute) in starts, attribute
            rows.append({'source': profile.source, 'hashes_match': True,
                         'all_five_offsets_are_function_starts': True})
    print(json.dumps(rows, ensure_ascii=False, indent=2))
