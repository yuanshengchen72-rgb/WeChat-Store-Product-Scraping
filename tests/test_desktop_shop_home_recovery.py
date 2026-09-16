"""Blank search recovery must visit a real shop before retrying the word."""
import unittest
import tempfile
from pathlib import Path
from unittest import mock

from wechat_shop_scraper.desktop_keyword import (
    DesktopWechatBackend, DesktopWechatError, SHOP_HOME_ROUTE, SHOP_APP_ID,
    DesktopKeywordListener,
)


class ShopHomeRecoveryTests(unittest.TestCase):
    def test_home_to_search_handover_waits_for_replacement_connection(self):
        backend = DesktopWechatBackend()
        backend.cdp_bridge = mock.Mock(connection_generation=1, runtime_ready=True)
        backend.evaluate = mock.Mock(return_value={'route': 'globalSearch'})
        backend._launch_applet = mock.Mock()
        turns = []
        def wait(_seconds):
            turns.append(1)
            if len(turns) == 1:
                backend.cdp_bridge.runtime_ready = False
            elif len(turns) == 2:
                backend.cdp_bridge.runtime_ready = True
                backend.cdp_bridge.connection_generation = 2
            return False
        with mock.patch.object(backend.cancel_event, 'wait', side_effect=wait):
            backend._wait_search_handover(timeout=2)
        self.assertGreaterEqual(len(turns), 4)
        self.assertEqual(backend.cdp_bridge.connection_generation, 2)
        backend._launch_applet.assert_not_called()

    def test_real_wechat_lite_home_redirect_is_a_valid_loaded_home(self):
        backend = DesktopWechatBackend()
        backend._remember_recovery_shops([{'appid': 'wx677b05c4ba7096ed'}])
        backend._launch_applet = mock.Mock()
        backend.evaluate = mock.Mock(return_value={
            'route': 'subpackages/shopWindow/pages/wxShopHomeLite/index',
            'appid': 'wx677b05c4ba7096ed', 'ready': True,
        })
        with mock.patch('wechat_shop_scraper.desktop_keyword.time.monotonic', side_effect=[0, 0, 31]), \
                mock.patch.object(backend.cancel_event, 'wait', return_value=False):
            backend._restore_shop_home()
        self.assertEqual(backend.evaluate.call_count, 1)

    def test_blank_launch_recovers_via_home_before_search(self):
        backend = DesktopWechatBackend()
        backend.cdp_bridge = mock.Mock(runtime_ready=False)
        backend._launch_applet = mock.Mock(side_effect=DesktopWechatError(
            '内部采集通道未就绪（在线=1，页面数=0，初始化=False）'))
        backend._restore_shop_home = mock.Mock()
        backend._wait_search_handover = mock.Mock()
        backend.evaluate = mock.Mock(side_effect=[{'ok': True}, {'route': 'globalSearch'}])
        backend.open_search_page(force=True)
        backend._restore_shop_home.assert_called_once()
        backend._wait_search_handover.assert_called_once()
        self.assertEqual(backend._launch_applet.call_count, 1)
        self.assertEqual(backend.evaluate.call_count, 2)

    def test_failed_home_recovery_is_bounded_without_claiming_search_ready(self):
        backend = DesktopWechatBackend()
        backend.cdp_bridge = mock.Mock(runtime_ready=False)
        backend._launch_applet = mock.Mock(side_effect=DesktopWechatError('采集通道未就绪'))
        backend._restore_shop_home = mock.Mock(side_effect=DesktopWechatError('小店首页恢复失败'))
        backend.evaluate = mock.Mock()
        with self.assertRaisesRegex(DesktopWechatError, '小店首页恢复失败'):
            backend.open_search_page(force=True)
        backend._restore_shop_home.assert_called_once()
        backend.evaluate.assert_not_called()

    def test_home_uses_recorded_shop_and_verifies_identity_before_return(self):
        backend = DesktopWechatBackend()
        backend._remember_recovery_shops([{'appid': 'wx0123456789abcdef', 'shop': '已采集店铺'}])
        backend._launch_applet = mock.Mock()
        backend.evaluate = mock.Mock(side_effect=[
            {'route': SHOP_HOME_ROUTE, 'appid': 'wxffffffffffffffff', 'ready': True},
            {'route': SHOP_HOME_ROUTE, 'appid': 'wx0123456789abcdef', 'ready': False},
            {'route': SHOP_HOME_ROUTE, 'appid': 'wx0123456789abcdef', 'ready': True},
        ])
        with mock.patch.object(backend.cancel_event, 'wait', return_value=False):
            backend._restore_shop_home()
        self.assertEqual(backend.evaluate.call_count, 3)
        route = backend._launch_applet.call_args.kwargs['entry_url']
        self.assertTrue(route.startswith(SHOP_HOME_ROUTE + '?'))
        self.assertTrue(route.endswith('realappid=wx0123456789abcdef'))

    def test_home_timeout_tries_only_recorded_candidates(self):
        backend = DesktopWechatBackend()
        backend._remember_recovery_shops([
            {'appid': 'wx0123456789abcdef'}, {'appid': 'wx1123456789abcdef'},
        ])
        backend._launch_applet = mock.Mock(side_effect=DesktopWechatError('通道连接超时'))
        backend.evaluate = mock.Mock()
        with self.assertRaisesRegex(DesktopWechatError, '首页恢复失败'):
            backend._restore_shop_home()
        self.assertEqual(backend._launch_applet.call_count, 2)
        backend.evaluate.assert_not_called()

    def test_stop_during_home_launch_cannot_evaluate_or_try_another_shop(self):
        backend = DesktopWechatBackend()
        backend._remember_recovery_shops([{'appid': 'wx0123456789abcdef'}])
        backend._launch_applet = mock.Mock(side_effect=lambda *_a, **_k: backend.pause())
        backend.evaluate = mock.Mock()
        with self.assertRaisesRegex(DesktopWechatError, '已停止'):
            backend._restore_shop_home()
        backend.evaluate.assert_not_called()
        self.assertEqual(backend._launch_applet.call_count, 1)

    def test_safety_prompt_never_triggers_home_recovery(self):
        backend = DesktopWechatBackend()
        backend._open_search_page_once = mock.Mock(side_effect=DesktopWechatError('请完成验证'))
        backend._restore_shop_home = mock.Mock()
        with self.assertRaisesRegex(DesktopWechatError, '请完成验证'):
            backend.open_search_page()
        backend._restore_shop_home.assert_not_called()

    def test_home_verification_failure_is_not_retried_on_different_shop(self):
        backend = DesktopWechatBackend()
        backend._remember_recovery_shops([{'appid': 'wx0123456789abcdef'}])
        backend._launch_applet = mock.Mock()
        backend.evaluate = mock.Mock(side_effect=DesktopWechatError('请完成验证'))
        with self.assertRaisesRegex(DesktopWechatError, '请完成验证'):
            backend._restore_shop_home()
        self.assertEqual(backend.evaluate.call_count, 1)

    def test_cache_is_bounded_public_shop_ids_and_survives_new_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'desktop-host.json'
            backend = DesktopWechatBackend(host_state_path=marker)
            backend._remember_recovery_shops([
                None, {'appid': SHOP_APP_ID}, {'appid': 'wx123&redirect=wrong'},
                *[{'appid': f'wx{i:016d}', 'shop': f'店{i}', 'token': 'must-not-save'}
                  for i in range(8)],
            ])
            self.assertEqual(len(backend._recovery_shops), 3)
            saved = backend._shop_state_path.read_text(encoding='utf-8')
            self.assertNotIn('must-not-save', saved)
            fresh = DesktopWechatBackend(host_state_path=marker)
            self.assertEqual(fresh._recovery_shops, backend._recovery_shops)
            self.assertFalse(marker.exists())

    def test_missing_shop_entry_never_dispatches_unknown_shop(self):
        backend = DesktopWechatBackend()
        backend._launch_applet = mock.Mock()
        with self.assertRaisesRegex(DesktopWechatError, '还没有可用的店铺入口'):
            backend._restore_shop_home()
        backend._launch_applet.assert_not_called()

    def test_recovery_finishes_same_word_and_does_not_commit_home_products(self):
        backend = DesktopWechatBackend()
        backend.start = mock.Mock()
        backend.cdp_bridge = mock.Mock(runtime_ready=False)
        backend._launch_applet = mock.Mock(side_effect=DesktopWechatError('采集通道未就绪'))
        backend._restore_shop_home = mock.Mock()
        backend.evaluate = mock.Mock(side_effect=[{'ok': True}, {'route': 'globalSearch'}])
        backend._wait_search_handover = mock.Mock()
        backend.collect_keyword = mock.Mock(side_effect=[DesktopWechatError('页面连接超时'), '已完成'])
        finished, products, errors = mock.Mock(), mock.Mock(), []
        listener = DesktopKeywordListener(
            keyword='', keywords=['当前词'], backend=backend, on_products=products,
            on_keyword_finished=finished, on_error=errors.append, retry_seconds=0,
        )
        listener._automation_loop()
        self.assertEqual(errors, [])
        self.assertEqual([call.args[0] for call in backend.collect_keyword.call_args_list], ['当前词'] * 2)
        products.assert_not_called()
        backend._restore_shop_home.assert_called_once()
        finished.assert_called_once_with('当前词', 1, 1, '已完成')


if __name__ == '__main__':
    unittest.main()
