"""Replay a missing File Helper entry through real bootstrap/recovery/listener.

No real UI, native attachment, network, products or checkpoint writes.
"""
import threading
import unittest
from unittest import mock

from wechat_shop_scraper import desktop_bootstrap as bootstrap
from wechat_shop_scraper.desktop_keyword import DesktopKeywordListener, DesktopWechatBackend, DesktopWechatError


class EntryRecoveryTests(unittest.TestCase):
    def ui(self):
        ui = bootstrap._ShopUI.__new__(bootstrap._ShopUI)
        ui.weixin_pid = 11
        ui.cancel_event = threading.Event()
        ui.log = mock.Mock()
        ui.core = mock.Mock()
        ui.client = mock.Mock()
        ui.user32 = mock.Mock()
        ui.user32.IsWindowVisible.return_value = True
        ui.user32.IsIconic.return_value = False
        ui._windows = mock.Mock(return_value=[101])
        window = mock.Mock(CurrentAutomationId=bootstrap.FILEHELPER_ID, CurrentNativeWindowHandle=101)
        ui.client.ElementFromHandle.return_value = window
        return ui, window

    def test_minimized_exact_filehelper_is_restored_without_activation(self):
        ui, window = self.ui()
        ui.user32.IsIconic.side_effect = [True, False]
        card = mock.Mock()
        ui._find = mock.Mock(side_effect=[[mock.Mock()], [card]])
        with mock.patch.object(ui.cancel_event, 'wait', return_value=False):
            self.assertEqual(ui.require_card(), (window, card))
        ui.user32.ShowWindowAsync.assert_called_once_with(101, 4)
        ui._windows.assert_called_once_with(11, {'文件传输助手'}, include_hidden=True)

    def test_hidden_filehelper_is_restored_and_reacquired(self):
        ui, window = self.ui()
        ui.user32.IsWindowVisible.side_effect = [False, True]
        ui._find = mock.Mock(side_effect=[[mock.Mock()], [mock.Mock()]])
        with mock.patch.object(ui.cancel_event, 'wait', return_value=False):
            ui.require_card()
        ui.user32.ShowWindowAsync.assert_called_once_with(101, 4)
        self.assertGreaterEqual(ui.client.ElementFromHandle.call_count, 2)

    def test_offscreen_exact_card_scrolls_into_view_then_reacquires_visible_card(self):
        ui, window = self.ui()
        messages, offscreen, visible = mock.Mock(), mock.Mock(), mock.Mock()
        ui._find = mock.Mock(side_effect=[[messages], [], [offscreen], [messages], [visible]])
        with mock.patch.object(ui.cancel_event, 'wait', return_value=False):
            self.assertEqual(ui.require_card(), (window, visible))
        offscreen.GetCurrentPattern.assert_called_once_with(10017)
        offscreen.GetCurrentPattern.return_value.QueryInterface.return_value.ScrollIntoView.assert_called_once()
        self.assertEqual(ui._find.call_args_list[2], mock.call(messages, name=bootstrap.CARD_NAME, control_type=50007))
        ui.user32.PostMessageW.assert_not_called()

    def test_wrong_chat_with_same_title_is_never_restored(self):
        ui, window = self.ui()
        window.CurrentAutomationId = 'other-chat'
        with self.assertRaises(bootstrap.ShopBootstrapError):
            ui.require_card()
        ui.user32.ShowWindowAsync.assert_not_called()

    def test_stop_after_window_restore_prevents_reading_or_clicking_card(self):
        ui, _ = self.ui()
        ui.user32.IsIconic.return_value = True
        ui._find = mock.Mock()
        with mock.patch.object(ui.cancel_event, 'wait', side_effect=lambda _: ui.cancel_event.set()):
            with self.assertRaisesRegex(bootstrap.ShopBootstrapError, '停止'):
                ui.require_card()
        ui._find.assert_not_called()
        ui.user32.PostMessageW.assert_not_called()

    def test_scroll_request_is_not_treated_as_card_visible(self):
        ui, _ = self.ui()
        messages, card = mock.Mock(), mock.Mock()
        ui._find = mock.Mock(side_effect=[[messages], [], [card], [messages], []])
        with mock.patch.object(ui.cancel_event, 'wait', return_value=False):
            with self.assertRaises(bootstrap.ShopBootstrapEntryUnavailable):
                ui.require_card()
        ui.user32.PostMessageW.assert_not_called()

    def test_unsupported_scroll_pattern_waits_without_coordinate_guess(self):
        ui, _ = self.ui()
        card = mock.Mock()
        card.GetCurrentPattern.side_effect = RuntimeError('unsupported')
        ui._find = mock.Mock(side_effect=[[mock.Mock()], [], [card]])
        with self.assertRaises(bootstrap.ShopBootstrapEntryUnavailable):
            ui.require_card()
        ui.user32.PostMessageW.assert_not_called()

    def test_multiple_filehelpers_are_not_chosen_arbitrarily(self):
        ui, _ = self.ui()
        ui._windows.return_value = [101, 102]
        with self.assertRaises(bootstrap.ShopBootstrapEntryUnavailable):
            ui.require_card()
        ui.user32.ShowWindowAsync.assert_not_called()

    def test_preflight_only_checks_card_without_closing_or_launching(self):
        ui = mock.Mock()
        with mock.patch.dict('sys.modules', {'uiautomation': mock.MagicMock()}), \
                mock.patch.object(bootstrap, '_ShopUI', return_value=ui):
            bootstrap.FileHelperShopOpener().check_entry(
                weixin_pid=11, get_host_pid=lambda: 22, cancel_event=threading.Event(), log=mock.Mock())
        ui.require_card.assert_called_once()
        ui.close_shop.assert_not_called()
        ui.open_card.assert_not_called()

    def test_start_preflight_waits_for_entry_without_native_or_close(self):
        opener = mock.Mock()
        opener.check_entry.side_effect = [bootstrap.ShopBootstrapEntryUnavailable('窗口未显示')] * 3 + [None]
        states = []
        backend = DesktopWechatBackend(normal_shop_opener=opener)
        backend.on_recovery_status = states.append
        backend.start = mock.Mock()
        backend._launch_applet = mock.Mock()
        with mock.patch('wechat_shop_scraper.desktop_keyword._visible_weixin_identity', return_value=(11, 22)), \
                mock.patch.object(backend.cancel_event, 'wait', return_value=False):
            backend.prepare_recovery_entry()
        self.assertEqual(opener.check_entry.call_count, 4)
        opener.assert_not_called()
        backend.start.assert_not_called()
        backend._launch_applet.assert_not_called()
        self.assertTrue(any('等待自动恢复入口' in state for state in states))

    def test_stop_during_missing_entry_wait_never_launches(self):
        opener = mock.Mock()
        opener.check_entry.side_effect = bootstrap.ShopBootstrapEntryUnavailable('窗口未显示')
        backend = DesktopWechatBackend(normal_shop_opener=opener)
        with mock.patch('wechat_shop_scraper.desktop_keyword._visible_weixin_identity', return_value=(11, 22)), \
                mock.patch.object(backend.cancel_event, 'wait', side_effect=lambda _: backend.cancel_event.set() or True):
            with self.assertRaisesRegex(DesktopWechatError, '停止'):
                backend.prepare_recovery_entry()
        opener.check_entry.assert_called_once()
        opener.assert_not_called()

    def test_listener_checks_entry_before_starting_native_connection(self):
        backend = DesktopWechatBackend(normal_shop_opener=mock.Mock())
        backend.prepare_recovery_entry = mock.Mock(side_effect=lambda: backend.cancel_event.set())
        backend.start = mock.Mock()
        backend.open_search_page = mock.Mock()
        backend.collect_keyword = mock.Mock(return_value='已完成')
        listener = DesktopKeywordListener(keyword='', keywords=['当前词'], backend=backend, on_products=mock.Mock())
        listener.start()
        listener.stop()
        backend.prepare_recovery_entry.assert_called_once()
        backend.start.assert_not_called()

    def test_four_missing_window_probes_then_ready_retries_same_word_without_pause(self):
        missing = bootstrap._ShopUI.__new__(bootstrap._ShopUI)
        missing.weixin_pid = 11
        missing.cancel_event = threading.Event()
        missing._windows = mock.Mock(return_value=[])
        missing.client = mock.Mock()
        ui = mock.Mock()
        attempts = []

        def require():
            attempts.append(1)
            if len(attempts) <= 4:
                return missing.require_card()  # the exact screenshot error
            return mock.Mock(), mock.Mock()

        ui.require_card.side_effect = require
        ui.shop_windows.return_value = []
        ui.shop_rendered.return_value = True
        logs, states, errors, finished = [], [], [], mock.Mock()
        backend = DesktopWechatBackend(logs.append, normal_shop_opener=bootstrap.FileHelperShopOpener())
        backend.on_recovery_status = states.append
        backend.stop = mock.Mock()
        backend.start = mock.Mock()
        backend._launch_applet = mock.Mock()
        backend._navigate_search_page = mock.Mock()
        backend._wait_search_handover = mock.Mock()
        backend.collect_keyword = mock.Mock(side_effect=lambda *_: backend.recover_search_runtime() or '已完成')
        listener = DesktopKeywordListener(keyword='', keywords=['卡通母婴床盖套件'], backend=backend,
                                         on_products=mock.Mock(), on_error=errors.append,
                                         on_keyword_finished=finished, retry_seconds=0)
        with mock.patch.dict('sys.modules', {'uiautomation': mock.MagicMock()}), \
                mock.patch.object(bootstrap, '_ShopUI', return_value=ui), \
                mock.patch('wechat_shop_scraper.desktop_keyword._visible_weixin_identity', return_value=(11, 22)), \
                mock.patch.object(listener._stop, 'wait', return_value=False):
            listener._automation_loop()
        self.assertEqual(errors, [], '入口暂时不可用不能耗尽三次关键词重试并暂停')
        self.assertEqual(len(attempts), 5)
        finished.assert_called_once_with('卡通母婴床盖套件', 1, 1, '已完成')
        ui.close_shop.assert_called_once()
        ui.open_card.assert_called_once()
        self.assertTrue(any('等待自动恢复入口' in text for text in logs))
        self.assertTrue(any('等待自动恢复入口' in text for text in states))
        self.assertEqual(states[-1], '自动恢复完成，继续当前关键词')


if __name__ == '__main__':
    unittest.main()
