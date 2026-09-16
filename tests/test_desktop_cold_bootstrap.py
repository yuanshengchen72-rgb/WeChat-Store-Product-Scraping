"""A replacement host must be normally initialized before native attachment."""
import unittest
from unittest import mock

from wechat_shop_scraper.desktop_keyword import DesktopWechatBackend, DesktopWechatError, SHOP_HOME_ROUTE


class ColdBootstrapTests(unittest.TestCase):
    def test_cold_host_gets_normal_shop_bootstrap_before_module_attach(self):
        backend = DesktopWechatBackend(normal_shop_opener=mock.Mock())
        backend._bootstrap_normal_shop = mock.Mock()
        rows = [
            {'started': 'replacement', 'MainWindowHandle': 0},
            {'started': 'replacement', 'MainWindowHandle': 12, 'MainWindowTitle': '小店'},
        ]
        with mock.patch('wechat_shop_scraper.desktop_keyword._visible_weixin_identity', return_value=(11, 22)), \
                mock.patch('wechat_shop_scraper.desktop_keyword._wmpf_root_pid', return_value=98765), \
                mock.patch('wechat_shop_scraper.desktop_keyword._powershell_json', side_effect=rows), \
                mock.patch('wechat_shop_scraper.desktop_keyword.loaded_module_path',
                           side_effect=RuntimeError('TEST_REACHED_VALIDATED_HOST')):
            with self.assertRaisesRegex(RuntimeError, 'TEST_REACHED_VALIDATED_HOST'):
                backend.start()
        backend._bootstrap_normal_shop.assert_called_once()

    def test_normal_opener_cannot_fake_initialized_host(self):
        backend = DesktopWechatBackend(normal_shop_opener=mock.Mock())
        with mock.patch('wechat_shop_scraper.desktop_keyword._visible_weixin_identity', return_value=(11, 22)), \
                mock.patch('wechat_shop_scraper.desktop_keyword._wmpf_root_pid', return_value=98765), \
                mock.patch('wechat_shop_scraper.desktop_keyword._powershell_json', return_value={
                    'started': 'still-cold', 'MainWindowHandle': 0}), \
                mock.patch('wechat_shop_scraper.desktop_keyword.loaded_module_path') as native:
            with self.assertRaisesRegex(DesktopWechatError, '尚未初始化'):
                backend.start()
        native.assert_not_called()
        backend.normal_shop_opener.assert_called_once()

    def test_recovery_opens_real_card_with_hook_then_retries_search(self):
        backend = DesktopWechatBackend(normal_shop_opener=mock.Mock())
        steps = []
        backend.stop = mock.Mock(side_effect=lambda: steps.append('release'))
        backend.start = mock.Mock(side_effect=lambda: steps.append('attach-guarded'))
        backend._bootstrap_normal_shop = mock.Mock(side_effect=lambda: steps.append('close-open-card'))
        backend._navigate_search_page = mock.Mock(side_effect=lambda _t: steps.append('search'))
        backend._wait_search_handover = mock.Mock()
        backend.cdp_bridge = mock.Mock(connection_generation=4)
        backend.cdp_bridge.wait_runtime_refresh.return_value = True
        backend.evaluate = mock.Mock(return_value={'ready': True, 'route': SHOP_HOME_ROUTE})
        backend._launch_applet = mock.Mock()
        backend.recover_search_runtime()
        self.assertEqual(steps, ['release', 'close-open-card', 'attach-guarded', 'close-open-card', 'search'])
        backend._launch_applet.assert_not_called()
        backend._wait_search_handover.assert_called_once()

    def test_cancel_during_card_bootstrap_does_not_attach(self):
        backend = DesktopWechatBackend()
        backend.normal_shop_opener = lambda **_kw: backend.pause()
        with mock.patch('wechat_shop_scraper.desktop_keyword._visible_weixin_identity', return_value=(11, 22)):
            with self.assertRaisesRegex(DesktopWechatError, '已停止'):
                backend._bootstrap_normal_shop()

    def test_missing_designated_card_does_not_fallback_to_other_shops(self):
        backend = DesktopWechatBackend(normal_shop_opener=mock.Mock())
        backend._restore_normal_shop = mock.Mock(side_effect=DesktopWechatError('李宁卡片不可见'))
        backend._launch_applet = mock.Mock()
        backend._remember_recovery_shops([{'appid': 'wx0123456789abcdef'}])
        with self.assertRaisesRegex(DesktopWechatError, '李宁卡片不可见'):
            backend._restore_shop_home()
        backend._launch_applet.assert_not_called()


if __name__ == '__main__':
    unittest.main()
