"""Replay native host death seen at 08:36:30, without attaching to WeChat."""
import unittest
import threading
from unittest import mock

from wechat_shop_scraper.desktop_keyword import DesktopKeywordListener, DesktopWechatBackend, DesktopWechatError
from wechat_shop_scraper.desktop_proto import WmpfCdpBridge
from wechat_shop_scraper.desktop_bootstrap import FileHelperShopOpener


class HostCrashTests(unittest.TestCase):
    def backend(self):
        backend = DesktopWechatBackend()
        backend.weixin_session = mock.Mock(is_detached=False)
        backend.wmpf_session = mock.Mock(is_detached=False)
        backend.weixin_script = mock.Mock()
        backend.wmpf_script = mock.Mock()
        backend.cdp_bridge = mock.Mock(connection_generation=34)
        backend.cdp_bridge.wait_runtime_refresh.return_value = True
        backend.start = mock.Mock()
        return backend

    def test_host_death_during_close_cannot_auto_attach_mid_launch(self):
        backend = self.backend()

        def close(*_args, **_kwargs):
            backend.wmpf_session.is_detached = True
            raise DesktopWechatError('电脑版微信后台命令超时：XWeb.CloseApplet')

        backend.call = mock.Mock(side_effect=close)
        backend.wmpf_script.exports_sync.reset_scene_patch.side_effect = RuntimeError('script has been destroyed')
        with self.assertRaises(DesktopWechatError):
            backend._launch_applet(timeout=0.01)
        backend.start.assert_not_called()
        backend.weixin_script.exports_sync.dispatch.assert_not_called()
        backend.cdp_bridge.wait_runtime_refresh.assert_not_called()

    def test_destroyed_script_before_detached_callback_cannot_launch(self):
        backend = self.backend()
        backend.call = mock.Mock()
        backend.wmpf_script.exports_sync.reset_scene_patch.side_effect = RuntimeError('script has been destroyed')
        with self.assertRaises(DesktopWechatError):
            backend._launch_applet(timeout=0.01)
        backend.weixin_script.exports_sync.dispatch.assert_not_called()

    def test_unconfirmed_close_on_live_host_does_not_double_launch(self):
        backend = self.backend()
        backend.call = mock.Mock(side_effect=DesktopWechatError('后台命令超时：XWeb.CloseApplet'))
        with self.assertRaises(DesktopWechatError):
            backend._launch_applet()
        backend.weixin_script.exports_sync.dispatch.assert_not_called()
        self.assertFalse(backend.active)

    def test_unbound_new_session_is_allowed_but_still_validates_new_page(self):
        backend = self.backend()
        backend.call = mock.Mock(side_effect=DesktopWechatError('session未绑定小程序'))
        backend._launch_applet()
        backend.weixin_script.exports_sync.dispatch.assert_called_once()
        backend.cdp_bridge.wait_runtime_refresh.assert_called_once()

    def test_unready_host_never_receives_close_or_launch(self):
        """A visible WMPF host is not safe until its debug runtime is ready."""
        backend = self.backend()
        backend.cdp_bridge.runtime_ready = False
        backend._normal_entry_opened_search = False
        backend.call = mock.Mock()
        with self.assertRaises(DesktopWechatError):
            backend._launch_applet(timeout=0.01)
        backend.call.assert_not_called()
        backend.weixin_script.exports_sync.dispatch.assert_not_called()

    def test_release_does_not_send_close_to_unready_host(self):
        backend = self.backend()
        backend.thread_id = 123
        backend.cdp_bridge.runtime_ready = False
        backend.call_batch = mock.Mock()
        backend.stop()
        backend.call_batch.assert_not_called()

    def test_host_death_after_launch_cannot_pass_on_stale_ready_flag(self):
        backend = self.backend()
        backend.call = mock.Mock()

        def wait(*_args, **_kwargs):
            backend.wmpf_session.is_detached = True
            return True

        backend.cdp_bridge.wait_runtime_refresh.side_effect = wait
        with self.assertRaises(DesktopWechatError):
            backend._launch_applet()

    def test_detach_wakes_native_wait_and_invalidates_ready_page(self):
        backend = self.backend()
        event, holder = threading.Event(), {}
        backend._pending['in-flight'] = event, holder
        backend._on_native_detached(backend.wmpf_session, '小程序', 'process-terminated')
        self.assertTrue(event.is_set())
        self.assertIsInstance(holder['error'], DesktopWechatError)
        self.assertFalse(backend.active)
        backend.cdp_bridge.prepare_relaunch.assert_called_once()

    def test_stale_detach_cannot_invalidate_new_connection(self):
        backend = self.backend()
        backend._on_native_detached(mock.Mock(), '小程序', 'process-terminated')
        self.assertTrue(backend.active)
        backend.cdp_bridge.prepare_relaunch.assert_not_called()

    def test_native_wait_exits_immediately_when_host_crashes(self):
        bridge, dead = WmpfCdpBridge(), threading.Event()
        dead.set()
        self.assertFalse(bridge.wait_runtime_refresh(0, 30, abort_event=dead))

    def test_evaluate_and_async_dispatch_cannot_hide_host_death_by_reconnecting(self):
        for method in ('call', 'dispatch_async'):
            backend = self.backend()
            backend.wmpf_session.is_detached = True
            with self.subTest(method=method), self.assertRaises(DesktopWechatError):
                getattr(backend, method)('XWeb.EvaluateJavaScript', {})
            backend.start.assert_not_called()
            backend.weixin_script.exports_sync.dispatch.assert_not_called()

    def test_recovery_backoff_is_capped_and_no_partial_handover_can_pass(self):
        backend = DesktopWechatBackend(normal_shop_opener=mock.Mock())
        backend._restore_normal_shop = mock.Mock()
        backend._navigate_search_page = mock.Mock()
        backend._wait_search_handover = mock.Mock(side_effect=[DesktopWechatError('连接稳定检查超时')] * 5 + [None])
        backend.stop = mock.Mock()
        with mock.patch.object(backend.cancel_event, 'wait', return_value=False) as wait:
            backend.recover_search_runtime()
        self.assertEqual(backend._restore_normal_shop.call_count, 6)
        self.assertEqual([c.args[0] for c in wait.call_args_list], [15, 30, 60, 120, 120])

    def test_three_failed_home_reconnections_do_not_pause_or_skip_keyword(self):
        backend = DesktopWechatBackend(normal_shop_opener=mock.Mock())
        backend._restore_normal_shop = mock.Mock(side_effect=[
            DesktopWechatError('内部采集通道未就绪：在线=0，初始化=False'),
            DesktopWechatError('script has been destroyed'),
            DesktopWechatError('内部采集通道未就绪：页面数=0'), None,
        ])
        backend._navigate_search_page = mock.Mock()
        backend._wait_search_handover = mock.Mock()
        backend.stop = mock.Mock()
        backend.collect_keyword = mock.Mock(side_effect=[DesktopWechatError('第 7 页读取超时'), '已完成'])
        finished, errors, delays = mock.Mock(), [], []
        listener = DesktopKeywordListener(
            keyword='', keywords=['棉麻针刺织四件套'], backend=backend,
            on_products=lambda _: None, on_keyword_finished=finished,
            on_error=errors.append, retry_seconds=0,
        )
        with mock.patch.object(listener._stop, 'wait', side_effect=lambda delay: delays.append(delay) or False):
            listener._automation_loop()
        self.assertEqual(errors, [], '连接恢复失败不能消耗当前词的三次重试并弹窗暂停')
        self.assertEqual(backend._restore_normal_shop.call_count, 4)
        self.assertEqual([c.args[0] for c in backend.collect_keyword.call_args_list], ['棉麻针刺织四件套'] * 2)
        finished.assert_called_once_with('棉麻针刺织四件套', 1, 1, '已完成')
        self.assertEqual([delay for delay in delays if delay], [15.0, 30.0, 60.0])

    def test_stop_during_recovery_backoff_never_opens_again(self):
        backend = DesktopWechatBackend(normal_shop_opener=mock.Mock())
        backend._restore_normal_shop = mock.Mock(side_effect=DesktopWechatError('调试通道未就绪'))
        backend.stop = mock.Mock()

        def stop(_delay):
            backend.cancel_event.set()
            return True

        with mock.patch.object(backend.cancel_event, 'wait', side_effect=stop):
            with self.assertRaisesRegex(DesktopWechatError, '已停止'):
                backend.recover_search_runtime()
        backend._restore_normal_shop.assert_called_once()

    def test_missing_card_or_login_error_cannot_loop(self):
        for message in ('未找到可见的lining李宁卡片', '请先登录', '请完成验证'):
            with self.subTest(message=message):
                backend = DesktopWechatBackend(normal_shop_opener=mock.Mock())
                backend._restore_normal_shop = mock.Mock(side_effect=DesktopWechatError(message))
                backend.stop = mock.Mock()
                with mock.patch.object(backend.cancel_event, 'wait') as wait:
                    with self.assertRaisesRegex(DesktopWechatError, message):
                        backend.recover_search_runtime()
                backend._restore_normal_shop.assert_called_once()
                wait.assert_not_called()

    def test_startup_host_crash_uses_full_recovery_before_worker(self):
        backend = DesktopWechatBackend(normal_shop_opener=mock.Mock())
        # Entry preflight has its own tests; this fixture must not enumerate
        # the real desktop before exercising a mocked startup crash.
        backend.prepare_recovery_entry = mock.Mock()
        backend.start = mock.Mock(side_effect=DesktopWechatError('script has been destroyed'))
        backend.recover_search_runtime = mock.Mock()
        backend.collect_keyword = mock.Mock(return_value='已完成')
        completed = threading.Event()
        listener = DesktopKeywordListener(
            keyword='', keywords=['当前词'], backend=backend,
            on_products=lambda _: None, on_complete=completed.set,
        )
        with mock.patch('wechat_shop_scraper.desktop_keyword.PAGE_REENTRY_RETRY_SECONDS', 0):
            listener.start()
        self.assertTrue(completed.wait(1))
        listener.stop()
        backend.start.assert_called_once()
        backend.recover_search_runtime.assert_called_once()

    def test_detach_between_command_preflight_and_dispatch_sends_nothing(self):
        backend = self.backend()
        backend._next_call_id = lambda: (backend._native_lost.set() or 'test-call')
        for method in ('call', 'dispatch_async'):
            with self.subTest(method=method):
                backend._native_lost.clear()
                with self.assertRaises(DesktopWechatError):
                    getattr(backend, method)('XWeb.version', timeout=0.01)
        backend.weixin_script.exports_sync.dispatch.assert_not_called()

    def test_blank_normal_home_is_a_recoverable_timeout_not_missing_card(self):
        from wechat_shop_scraper.desktop_keyword import _is_transient_desktop_error
        ui = mock.Mock()
        ui.shop_windows.return_value = []
        ui.shop_rendered.return_value = False
        backend = DesktopWechatBackend(normal_shop_opener=FileHelperShopOpener())
        with mock.patch.dict('sys.modules', {'uiautomation': mock.MagicMock()}), \
                mock.patch('wechat_shop_scraper.desktop_bootstrap._ShopUI', return_value=ui), \
                mock.patch('wechat_shop_scraper.desktop_keyword._visible_weixin_identity', return_value=(11, 22)), \
                mock.patch('wechat_shop_scraper.desktop_bootstrap.time.monotonic', side_effect=[0, 0, 36]):
            with self.assertRaises(DesktopWechatError) as failure:
                backend._bootstrap_normal_shop()
        self.assertTrue(_is_transient_desktop_error(failure.exception))

    def test_wmpf_25560_process_termination_after_hook_attach_is_fatal(self):
        backend = DesktopWechatBackend()
        backend.profile = mock.Mock(source='微信 4.1.13.65 / WMPF 25560')
        backend._hooks_attached_at = 10.0
        with mock.patch('wechat_shop_scraper.desktop_keyword.time.monotonic', return_value=12.0):
            backend._on_native_detached(backend.wmpf_session, '小程序', 'process-terminated')
        self.assertIsNotNone(backend.fatal_error)
        self.assertIn('钩子', str(backend.fatal_error))

    def test_fatal_hook_error_is_not_sent_through_filehelper_recovery(self):
        backend = DesktopWechatBackend(normal_shop_opener=mock.Mock())
        backend._native_fatal_error = DesktopWechatError('WMPF 25560 钩子不兼容')
        backend.recover_search_runtime = mock.Mock()
        backend.collect_keyword = mock.Mock(return_value='已完成')
        errors = []
        listener = DesktopKeywordListener(
            keyword='', keywords=['当前词'], backend=backend,
            on_products=lambda _: None, on_error=errors.append,
        )
        listener._automation_loop()
        backend.recover_search_runtime.assert_not_called()
        backend.collect_keyword.assert_not_called()
        self.assertTrue(errors)
        self.assertIn('钩子不兼容', errors[0])


if __name__ == '__main__':
    unittest.main()
