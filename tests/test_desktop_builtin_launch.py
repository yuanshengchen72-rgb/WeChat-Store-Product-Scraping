"""Independent bootstrap must not need File Helper or trust an empty window."""
import itertools
import threading
import unittest
from unittest import mock

from wechat_shop_scraper import desktop_builtin as builtin
from wechat_shop_scraper.desktop_bootstrap import (
    ShopBootstrapError, ShopBootstrapTimeout, ShopBootstrapEntryUnavailable,
)
from wechat_shop_scraper.desktop_bootstrap import _ShopUI
from wechat_shop_scraper.desktop_keyword import DesktopWechatBackend


class BuiltinLaunchTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.kw = dict(weixin_pid=11, get_host_pid=lambda: 22,
                       cancel_event=self.cancel, log=mock.Mock())
        self.ui = mock.Mock()
        self.ui.shop_windows.return_value = []
        self.ui.search_rendered.return_value = True
        self.native = mock.MagicMock()
        self.native.__enter__.return_value = self.native
        self.native.status.return_value = 'dispatched'

    def invoke(self):
        with mock.patch.object(builtin, '_validate_main', return_value=(33, 0x7866f0)), \
                mock.patch.object(builtin, '_shop_ui', return_value=mock.MagicMock(
                    __enter__=mock.Mock(return_value=self.ui), __exit__=mock.Mock(return_value=False))), \
                mock.patch.object(builtin, '_NativeLaunch', return_value=self.native), \
                mock.patch.object(self.cancel, 'wait', return_value=False), \
                mock.patch.object(builtin.time, 'monotonic', side_effect=itertools.count(0, 0.25)):
            builtin.BuiltinShopOpener()(**self.kw)

    def test_missing_filehelper_does_not_block_normal_launch(self):
        self.ui.require_card.side_effect = AssertionError('Must never read chat')
        self.invoke()
        self.ui.close_shop.assert_not_called()
        self.native.begin.assert_called_once_with(33)
        self.ui.require_card.assert_not_called()
        self.ui.open_card.assert_not_called()
        self.assertGreaterEqual(self.ui.search_rendered.call_count, 3)

    def test_empty_shop_window_is_not_initialized(self):
        self.ui.search_rendered.return_value = False
        with self.assertRaises(ShopBootstrapTimeout):
            self.invoke()
        self.native.__exit__.assert_called_once()

    def test_stop_before_launch_prevents_launch(self):
        self.cancel.set()
        with self.assertRaisesRegex(ShopBootstrapError, '停止'):
            self.invoke()
        self.native.begin.assert_not_called()

    def test_visible_shop_search_window_is_reused_without_native_relaunch(self):
        self.ui.shop_windows.return_value = [42]
        self.invoke()
        self.native.begin.assert_not_called()

    def test_visible_search_window_is_not_relaunched_during_recovery(self):
        self.ui.shop_windows.return_value = [42]
        self.ui.search_rendered.return_value = True
        with mock.patch.object(builtin, '_validate_main', return_value=(33, 0x7866f0)), \
                mock.patch.object(builtin, '_shop_ui', return_value=mock.MagicMock(
                    __enter__=mock.Mock(return_value=self.ui), __exit__=mock.Mock(return_value=False))), \
                mock.patch.object(builtin, '_NativeLaunch', return_value=self.native), \
                mock.patch.object(self.cancel, 'wait', return_value=False):
            builtin.BuiltinShopOpener()(**self.kw)
        self.native.begin.assert_not_called()

    def test_forced_recovery_relaunches_visible_search_window(self):
        """An unbound session must get a fresh page, not reuse the old one."""
        self.ui.shop_windows.side_effect = [[42], [42], []]
        self.ui.search_rendered.return_value = True
        with mock.patch.object(builtin, '_validate_main', return_value=(33, 0x7866f0)), \
                mock.patch.object(builtin, '_shop_ui', return_value=mock.MagicMock(
                    __enter__=mock.Mock(return_value=self.ui), __exit__=mock.Mock(return_value=False))), \
                mock.patch.object(builtin, '_NativeLaunch', return_value=self.native), \
                mock.patch.object(self.cancel, 'wait', return_value=False), \
                mock.patch.object(builtin.time, 'monotonic', side_effect=itertools.count(0, 0.25)):
            builtin.BuiltinShopOpener()(**self.kw, force_relaunch=True)
        self.ui.close_shop.assert_called_once()
        self.native.begin.assert_called_once_with(33)

    def test_async_dispatch_pending_is_not_readiness(self):
        self.native.status.return_value = 'pending'
        with self.assertRaises(ShopBootstrapTimeout):
            self.invoke()
        self.ui.search_rendered.assert_not_called()

    def test_preflight_validates_binary_without_opening_or_chat(self):
        with mock.patch.object(builtin, '_validate_main', return_value=(33, 0x7866f0)) as validate, \
                mock.patch.object(builtin, '_shop_ui') as ui, \
                mock.patch.object(builtin, '_NativeLaunch') as native:
            builtin.BuiltinShopOpener().check_entry(**self.kw)
        validate.assert_called_once_with(11)
        ui.assert_not_called()
        native.assert_not_called()

    def test_missing_filehelper_does_not_replace_primary_host_failure(self):
        primary = mock.Mock(side_effect=ShopBootstrapTimeout('内置入口已调用，但宿主进程已退出'))
        fallback = mock.Mock(side_effect=ShopBootstrapEntryUnavailable('识别数=0'))
        logs = []
        opener = builtin.ResilientShopOpener(primary=primary, fallback=fallback)
        with self.assertRaises(ShopBootstrapTimeout):
            opener(**dict(self.kw, log=logs.append))
        self.assertTrue(any('不再等待文件传输助手' in item for item in logs))

    def test_unsupported_native_entry_checks_designated_card_fallback(self):
        primary = mock.Mock()
        primary.check_entry.side_effect = ShopBootstrapError('版本不支持内置入口')
        fallback = mock.Mock()
        opener = builtin.ResilientShopOpener(primary=primary, fallback=fallback)
        opener.check_entry(**self.kw)
        primary.check_entry.assert_called_once_with(**self.kw)
        fallback.check_entry.assert_called_once_with(**self.kw)

    def test_unsupported_binary_fails_before_window_close(self):
        with mock.patch.object(builtin, '_validate_main', side_effect=ShopBootstrapError('版本不支持')), \
                mock.patch.object(builtin, '_shop_ui') as ui:
            with self.assertRaises(ShopBootstrapError):
                builtin.BuiltinShopOpener()(**self.kw)
        ui.assert_not_called()

    def test_recovery_chain_detaches_before_builtin_then_rebuilds(self):
        backend = DesktopWechatBackend(normal_shop_opener=builtin.BuiltinShopOpener())
        steps = []
        backend.stop = mock.Mock(side_effect=lambda: steps.append('detach'))
        backend.start = mock.Mock(side_effect=lambda: steps.append('guarded-attach'))
        backend._launch_applet = mock.Mock(side_effect=lambda _: steps.append('assistant-search'))
        with mock.patch.object(builtin.BuiltinShopOpener, '__call__', side_effect=lambda **_: steps.append('normal-entry')), \
                mock.patch('wechat_shop_scraper.desktop_keyword._visible_weixin_identity', return_value=(11,33)), \
                mock.patch('wechat_shop_scraper.desktop_keyword._wmpf_root_pid', return_value=0):
            backend._restore_normal_shop(30)
        self.assertEqual(steps, ['detach', 'normal-entry', 'guarded-attach'])
        backend._launch_applet.assert_not_called()

    def test_close_uses_applet_close_button_instead_of_merely_window_message(self):
        ui = _ShopUI.__new__(_ShopUI)
        ui.cancel_event = threading.Event()
        ui.shop_windows = mock.Mock(return_value=[42])
        ui.client = mock.Mock()
        ui.core = mock.Mock()
        ui.user32 = mock.Mock()
        button = mock.Mock()
        ui._find = mock.Mock(return_value=[button])
        ui.close_shop()
        button.GetCurrentPattern.assert_called_once_with(10000)
        button.GetCurrentPattern.return_value.QueryInterface.return_value.Invoke.assert_called_once()
        ui.user32.PostMessageW.assert_not_called()


if __name__ == '__main__':
    unittest.main()
