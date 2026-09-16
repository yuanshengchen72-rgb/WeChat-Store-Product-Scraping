import threading
import unittest
from unittest import mock

from wechat_shop_scraper.desktop_bootstrap import FileHelperShopOpener, ShopBootstrapError, _sent_card_x


class NormalCardOpenerTests(unittest.TestCase):
    def test_qt_message_row_does_not_click_empty_row_center(self):
        self.assertEqual(_sent_card_x(0, 978, 96), 878)
        self.assertNotEqual(_sent_card_x(0, 978, 96), 489)

    def test_card_target_scales_with_dpi_and_window_position(self):
        self.assertEqual(_sent_card_x(100, 1500, 192), 1300)
        self.assertEqual(_sent_card_x(100, 900, 0), 800)
        with self.assertRaisesRegex(ShopBootstrapError, '区域过窄'):
            _sent_card_x(0, 170, 96)

    def run_opener(self, ui, cancel=None):
        cancel = cancel or threading.Event()
        auto = mock.MagicMock()
        with mock.patch.dict('sys.modules', {'uiautomation': auto}), \
                mock.patch('wechat_shop_scraper.desktop_bootstrap._ShopUI', return_value=ui), \
                mock.patch.object(cancel, 'wait', return_value=False):
            FileHelperShopOpener()(weixin_pid=11, get_host_pid=lambda: 22,
                                  cancel_event=cancel, log=lambda _s: None)

    def test_close_verified_before_open_and_loaded_home_required(self):
        ui = mock.Mock()
        steps = []
        ui.close_shop.side_effect = lambda: steps.append('close')
        ui.shop_windows.side_effect = [[], ]
        ui.open_card.side_effect = lambda _log: steps.append('open')
        ui.shop_rendered.side_effect = [False, True, False, True, True, True]
        self.run_opener(ui)
        self.assertEqual(steps, ['close', 'open'])
        self.assertEqual(ui.shop_rendered.call_count, 6)

    def test_missing_entry_cannot_close_or_click_any_window(self):
        ui = mock.Mock()
        ui.require_card.side_effect = ShopBootstrapError('目标卡片不可见')
        with self.assertRaisesRegex(ShopBootstrapError, '不可见'):
            self.run_opener(ui)
        ui.close_shop.assert_not_called()
        ui.open_card.assert_not_called()

    def test_stuck_close_never_launches_duplicate(self):
        ui = mock.Mock()
        ui.shop_windows.return_value = [object()]
        with mock.patch('wechat_shop_scraper.desktop_bootstrap.time.monotonic', side_effect=[0, 9]):
            with self.assertRaisesRegex(ShopBootstrapError, '未完成关闭'):
                self.run_opener(ui)
        ui.open_card.assert_not_called()

    def test_cancel_after_close_prevents_reopen(self):
        ui = mock.Mock()
        cancel = threading.Event()
        ui.close_shop.side_effect = cancel.set
        ui.shop_windows.return_value = []
        with self.assertRaisesRegex(ShopBootstrapError, '已停止'):
            self.run_opener(ui, cancel)
        ui.open_card.assert_not_called()

    def test_blank_home_times_out_without_success(self):
        ui = mock.Mock()
        ui.shop_windows.return_value = []
        ui.shop_rendered.return_value = False
        with mock.patch('wechat_shop_scraper.desktop_bootstrap.time.monotonic', side_effect=[0, 0, 36]):
            with self.assertRaisesRegex(ShopBootstrapError, '内容仍未加载'):
                self.run_opener(ui)
