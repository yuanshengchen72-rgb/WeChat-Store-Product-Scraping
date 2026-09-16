from __future__ import annotations

import queue
import threading
import tempfile
import unittest
from unittest import mock

from wechat_shop_scraper.gui import ShopScraperApp, MODE_DESKTOP_KEYWORD
from wechat_shop_scraper.incremental_storage import IncrementalProductStore
from wechat_shop_scraper.model import ProductCard


class GuiLifecycleTests(unittest.TestCase):
    def make_app(self):
        app = ShopScraperApp.__new__(ShopScraperApp)
        app.events = queue.Queue()
        app.running = False
        app.listener = None
        app._start_thread = None
        app._desktop_backend = None
        app.store = mock.Mock()
        app.active_mode = MODE_DESKTOP_KEYWORD
        app._pending_keywords = ["床垫"]
        app._resume_index = 0
        app._pending_min_price = 50
        app._pending_max_price = 2000
        app._pending_restart_interval = 0
        return app

    def test_stop_waits_for_startup_then_closes_late_listener(self):
        app = self.make_app()
        listener = mock.Mock()
        startup = mock.Mock()
        startup.join.side_effect = lambda: setattr(app, "listener", listener)
        app._start_thread = startup
        app._stop_worker()
        startup.join.assert_called_once_with()
        listener.stop.assert_called_once()
        self.assertIsNone(app.listener)
        self.assertEqual(list(app.events.queue)[-1], ("finalize_store", None))
        app.store.export_csv.assert_not_called()

    def test_stop_enqueues_export_barrier_after_received_products(self):
        app = self.make_app()
        app.events.put(('products', []))
        app._stop_worker()
        self.assertEqual([kind for kind, _ in app.events.queue], ['products', 'finalize_store'])
        app.store.export_csv.assert_not_called()

    def test_close_saves_queued_pages_before_checkpoint_and_export(self):
        app = self.make_app()
        order = []
        app.root = mock.Mock()
        app._handle_products = lambda cards: order.append('products')
        app._record_keyword_finished = lambda *args: order.append('checkpoint')
        app.store.export_csv.side_effect = lambda: order.append('export') or True
        app.events.put(('products', []))
        app.events.put(('keyword_finished', ('床垫', 1, 1, '完成')))
        app._on_close()
        self.assertEqual(order, ['products', 'checkpoint', 'export'])

    def test_failed_storage_cannot_advance_keyword_checkpoint(self):
        app = self.make_app()
        app._storage_failed = True
        app._checkpoint_store = mock.Mock()
        app._record_keyword_finished('床垫', 1, 1, '完成')
        app._checkpoint_store.mark_finished.assert_not_called()

    def test_real_page_durable_before_checkpoint_and_final_export_before_restart(self):
        app = self.make_app()
        app._stopping = True
        app._batch_finished = False
        app._automation_pause_reason = None
        app._last_csv_export_error = None
        for name in ('root', 'start_button', 'stop_button', 'mode_combo', 'status_text',
                     'count_text', '_write_log', '_upsert_table', '_show_latest',
                     '_on_mode_changed', '_checkpoint_store'):
            setattr(app, name, mock.Mock())
        with tempfile.TemporaryDirectory() as folder:
            app.store = IncrementalProductStore(folder)
            app._checkpoint_store.mark_finished.side_effect = lambda *a, **k: self.assertEqual(
                len(IncrementalProductStore(folder)), 1)
            exporting, release = threading.Event(), threading.Event()
            original = app.store.export_csv
            def blocked_export():
                exporting.set()
                if not release.wait(5):
                    raise AssertionError('export was not released')
                return original()
            with mock.patch.object(app.store, 'export_csv', side_effect=blocked_export):
                app.events.put(('products', [ProductCard('1', keyword='床垫')]))
                app.events.put(('keyword_finished', ('床垫', 1, 1, '完成')))
                app.events.put(('finalize_store', None))
                app._drain_events()
                self.assertTrue(exporting.wait(5))
                try:
                    app.start_button.configure.assert_not_called()
                    app._checkpoint_store.mark_finished.assert_called_once()
                finally:
                    release.set()
                    app._export_thread.join(5)
                app._drain_events()
            self.assertFalse(app._stopping)
            self.assertFalse(app.store.needs_export)
            app.start_button.configure.assert_called_once_with(state='normal')

    def test_save_failure_keeps_batch_checkpoint_and_does_not_claim_completion(self):
        app = self.make_app()
        app._batch_finished = False
        app._checkpoint_store = mock.Mock()
        app._write_log = mock.Mock()
        app._stop = mock.Mock()
        app.store.upsert_many.side_effect = OSError('disk full')
        app._process_storage_event('products', [ProductCard('1')])
        app._process_storage_event('keyword_finished', ('床垫', 1, 1, '完成'))
        app._process_storage_event('batch_complete', None)
        app._checkpoint_store.mark_finished.assert_not_called()
        app._checkpoint_store.clear.assert_not_called()
        self.assertFalse(app._batch_finished)

    def test_stop_before_startup_does_not_create_listener(self):
        app = self.make_app()
        with mock.patch("wechat_shop_scraper.gui.DesktopKeywordListener") as factory:
            app._start_worker()
        factory.assert_not_called()

    def test_restart_reuses_backend_after_old_listener_has_stopped(self):
        app = self.make_app()
        app.running = True
        backend = mock.Mock()
        with mock.patch("wechat_shop_scraper.gui.DesktopWechatBackend", return_value=backend) as factory, mock.patch(
            "wechat_shop_scraper.gui.DesktopKeywordListener"
        ) as listeners:
            app._start_worker()
            app.running = False
            app._stop_worker()
            app.running = True
            app._start_worker()
        factory.assert_called_once()
        self.assertEqual(listeners.call_count, 2)
        self.assertTrue(all(call.kwargs["backend"] is backend for call in listeners.call_args_list))
        backend.stop.assert_not_called()

    def test_close_drains_startup_before_final_backend_release(self):
        app = self.make_app()
        sequence = []
        app.root = mock.Mock()
        app.listener = mock.Mock()
        app.listener.stop.side_effect = lambda: sequence.append("pause")
        app._start_thread = mock.Mock()
        app._start_thread.join.side_effect = lambda: sequence.append("join-start")
        app._desktop_backend = mock.Mock()
        app._desktop_backend.stop.side_effect = lambda: sequence.append("detach")
        app._on_close()
        self.assertEqual(sequence, ["pause", "join-start", "pause", "detach"])
        app.root.destroy.assert_called_once()

    def test_user_stop_during_startup_does_not_report_start_error(self):
        app = self.make_app()
        app.running = True
        listener = mock.Mock()
        def cancel_and_fail():
            app.running = False
            raise RuntimeError("backend stopped")
        listener.start.side_effect = cancel_and_fail
        with mock.patch("wechat_shop_scraper.gui.DesktopKeywordListener", return_value=listener):
            app._start_worker()
        self.assertFalse(any(kind == "start_error" for kind, _ in app.events.queue))
        listener.stop.assert_called()

    def test_stop_cancels_recovery_test_even_before_backend_is_created(self):
        app = self.make_app()
        app._stopping = False
        app._recovery_test_running = True
        app._recovery_test_cancel = threading.Event()
        app.stop_button = mock.Mock()
        app.status_text = mock.Mock()
        app._stop()
        self.assertTrue(app._recovery_test_cancel.is_set())
        self.assertTrue(app._recovery_test_running, '等待测试线程退出后才能重新开始')
        app.store.export_csv.assert_not_called()

    def test_recovery_test_keeps_stop_enabled_without_writing_products(self):
        app = self.make_app()
        app._stopping = False
        app.start_button = mock.Mock()
        app.stop_button = mock.Mock()
        app.mode_combo = mock.Mock()
        app.status_text = mock.Mock()
        app._on_mode_changed = mock.Mock()
        app._write_log = mock.Mock()
        with mock.patch('wechat_shop_scraper.gui.threading.Thread'):
            app._test_desktop_recovery()
        app.stop_button.configure.assert_called_with(state='normal')
        app.store.export_csv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
