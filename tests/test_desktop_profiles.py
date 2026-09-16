"""Version registry selects offsets by BOTH exact DLL identities, not a label."""
import hashlib
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from wechat_shop_scraper import desktop_keyword as desktop


class DesktopProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.weixin = Path(self.directory.name) / 'Weixin.dll'
        self.flue = Path(self.directory.name) / 'flue.dll'
        desktop._PROFILE_CACHE.clear()
        self.addCleanup(desktop._PROFILE_CACHE.clear)

    def profile(self, weixin, flue, offset):
        return replace(desktop.SUPPORTED_PROFILE,
                       weixin_size=len(weixin), flue_size=len(flue),
                       weixin_sha256=hashlib.sha256(weixin).hexdigest(),
                       flue_sha256=hashlib.sha256(flue).hexdigest(),
                       weixin_ipc_task_offset=offset, wmpf_load_start_offset=offset + 1,
                       source=f'fixture-{offset}')

    def test_selects_both_components_and_preserves_old_version(self):
        cases = [(b'old-main', b'old-plugin', 100), (b'new-main!', b'new-plugin!', 200),
                 (b'new-main!', b'old-plugin', 300), (b'old-main', b'new-plugin!', 400)]
        profiles = tuple(self.profile(*case) for case in cases)
        with mock.patch.object(desktop, 'SUPPORTED_PROFILES', profiles, create=True):
            for (main, plugin, offset), expected in zip(cases, profiles):
                with self.subTest(offset=offset):
                    self.weixin.write_bytes(main)
                    self.flue.write_bytes(plugin)
                    selected = desktop.validate_desktop_profile(self.weixin, self.flue)
                    self.assertIs(selected, expected)

    def test_same_size_unknown_bytes_still_rejected(self):
        self.weixin.write_bytes(b'impostor')
        self.flue.write_bytes(b'plugin')
        profile = self.profile(b'original', b'plugin', 100)
        with mock.patch.object(desktop, 'SUPPORTED_PROFILES', (profile,), create=True):
            with self.assertRaises(desktop.DesktopWechatError):
                desktop.validate_desktop_profile(self.weixin, self.flue)
        self.assertFalse(desktop._PROFILE_CACHE)

    def test_unknown_pair_cannot_mix_unrelated_offsets(self):
        self.weixin.write_bytes(b'old-main')
        self.flue.write_bytes(b'new-plugin')
        profiles = (self.profile(b'old-main', b'old-plugin', 100),
                    self.profile(b'new-main', b'new-plugin', 200))
        with mock.patch.object(desktop, 'SUPPORTED_PROFILES', profiles, create=True):
            with self.assertRaises(desktop.DesktopWechatError):
                desktop.validate_desktop_profile(self.weixin, self.flue)

    def test_cache_rehashes_after_file_change(self):
        self.weixin.write_bytes(b'main')
        self.flue.write_bytes(b'plugin')
        profile = self.profile(b'main', b'plugin', 100)
        with mock.patch.object(desktop, 'SUPPORTED_PROFILES', (profile,), create=True):
            self.assertIs(desktop.validate_desktop_profile(self.weixin, self.flue), profile)
            self.weixin.write_bytes(b'changed')
            with self.assertRaises(desktop.DesktopWechatError):
                desktop.validate_desktop_profile(self.weixin, self.flue)

    def test_registered_65_25560_uses_safe_runtime_entrypoints(self):
        profiles = getattr(desktop, 'SUPPORTED_PROFILES', (desktop.SUPPORTED_PROFILE,))
        matched = [p for p in profiles if '4.1.13.65' in p.source and '25560' in p.source]
        self.assertEqual(len(matched), 1)
        profile = matched[0]
        self.assertEqual(profile.weixin_ipc_task_offset, 0x5444400)
        self.assertEqual(profile.wmpf_load_start_offset, 0x2ce7cf0)
        self.assertEqual(profile.wmpf_cdp_filter_offset, 0)
        self.assertEqual(profile.wmpf_on_command_offset, 0x26a7de0)
        self.assertEqual(profile.wmpf_reply_offset, 0x26a9a10)
        self.assertIn(str(0x5444400), desktop._build_weixin_script(profile.weixin_ipc_task_offset))
        script = desktop._build_wmpf_script(profile)
        self.assertIn(f'loadStartOffset={0x2ce7cf0}', script)
        self.assertIn('cdpFilterOffset=0', script)


if __name__ == '__main__':
    unittest.main()
