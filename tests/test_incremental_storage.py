from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest import mock

from wechat_shop_scraper.incremental_storage import IncrementalProductStore
from wechat_shop_scraper.model import ProductCard
from wechat_shop_scraper.storage import ProductStore


class IncrementalStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def test_page_is_durable_without_rewriting_exports(self):
        store = IncrementalProductStore(self.path)
        with mock.patch.object(store, '_export_csv_locked') as export:
            store.upsert_many([ProductCard('1', keyword='床单')])
        export.assert_not_called()
        self.assertFalse(store.json_path.exists())
        recovered = IncrementalProductStore(self.path)
        self.assertEqual(recovered.snapshot()[0].keyword, '床单')
        self.assertTrue(recovered.needs_export)
        self.assertTrue(recovered.export_csv())
        self.assertEqual(json.loads(recovered.json_path.read_text('utf-8'))['count'], 1)
        self.assertFalse(recovered.needs_export)

    def test_legacy_batch_keeps_stamp_dedup_merge_and_thousand_row_csv(self):
        legacy = ProductStore(self.path)
        legacy.upsert_many(ProductCard(str(i), keyword='旧词', sales=100, sales_exact=True)
                           for i in range(1001))
        before = legacy.json_path.read_bytes()
        store = IncrementalProductStore(self.path)
        self.assertEqual(store._export_stamp, legacy._export_stamp)
        store.upsert_many([ProductCard('0', keyword='新词', sales=10, sales_exact=False)])
        self.assertEqual(store.json_path.read_bytes(), before)
        store = IncrementalProductStore(self.path)
        merged = next(card for card in store.snapshot() if card.product_id == '0')
        self.assertEqual(merged.keyword, '旧词｜新词')
        self.assertEqual(merged.sales, 100)
        self.assertTrue(store.export_csv())
        rows = []
        for path in sorted(self.path.glob('*.csv')):
            with path.open(encoding='utf-8-sig', newline='') as handle:
                rows.append(list(csv.DictReader(handle)))
        self.assertEqual([len(chunk) for chunk in rows], [1000, 1])
        self.assertEqual(len(IncrementalProductStore(self.path)), 1001)

    def test_commit_failure_does_not_publish_unsaved_items(self):
        store = IncrementalProductStore(self.path)
        store.upsert_many([ProductCard('1', keyword='旧词')])
        with mock.patch.object(store, '_commit_delta', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                store.upsert_many([ProductCard('1', keyword='新词'), ProductCard('2')])
        self.assertEqual(len(store), 1)
        self.assertEqual(store.snapshot()[0].keyword, '旧词')
        self.assertEqual(IncrementalProductStore(self.path).snapshot()[0].keyword, '旧词')

    def test_replay_after_snapshot_before_pending_clear_is_idempotent(self):
        store = IncrementalProductStore(self.path)
        store.upsert_many([ProductCard('1', keyword='旧词｜新词')])
        with mock.patch.object(store, '_clear_exported', side_effect=OSError('simulated crash')):
            self.assertFalse(store.export_csv())
        recovered = IncrementalProductStore(self.path)
        self.assertEqual(recovered.snapshot()[0].keyword, '旧词｜新词')
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered.export_csv())

    def test_export_does_not_block_page_commit_or_clear_newer_revision(self):
        store = IncrementalProductStore(self.path)
        store.upsert_many([ProductCard('1', keyword='旧词')])
        exporting, release = threading.Event(), threading.Event()
        original = store._export_csv_locked
        def slow_export(cards):
            exporting.set()
            self.assertTrue(release.wait(5))
            return original(cards)
        with mock.patch.object(store, '_export_csv_locked', side_effect=slow_export):
            worker = threading.Thread(target=store.export_csv)
            worker.start()
            self.assertTrue(exporting.wait(5))
            try:
                store.upsert_many([ProductCard('1', keyword='新词'), ProductCard('2')])
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertTrue(store.needs_export)
        recovered = IncrementalProductStore(self.path)
        self.assertEqual(len(recovered), 2)
        self.assertEqual(recovered._items['1'].keyword, '旧词｜新词')
        self.assertTrue(store.export_csv())
        self.assertFalse(store.needs_export)

    def test_locked_csv_retains_pending_and_retries(self):
        store = IncrementalProductStore(self.path)
        store.upsert_many([ProductCard('1')])
        with mock.patch.object(store, '_export_csv_locked', return_value=False):
            self.assertFalse(store.export_csv())
        self.assertTrue(store.needs_export)
        self.assertEqual(len(IncrementalProductStore(self.path)), 1)
        self.assertTrue(store.export_csv())

    def test_invalid_existing_data_is_not_silently_replaced(self):
        for name, content in [('products.json', '{broken'),
                              ('products.pending.sqlite3', 'broken database')]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / name
                path.write_text(content, encoding='utf-8')
                with self.assertRaises(OSError):
                    IncrementalProductStore(folder)
                self.assertEqual(path.read_text('utf-8'), content)

    def test_same_page_ids_are_merged_once_and_unchanged_page_does_not_commit(self):
        store = IncrementalProductStore(self.path)
        stamp = '2026-09-10T00:00:00'
        store.upsert_many([ProductCard('1', keyword='甲', captured_at=stamp),
                           ProductCard('1', keyword='乙', captured_at=stamp)])
        self.assertEqual(store.snapshot()[0].keyword, '甲｜乙')
        with mock.patch.object(store, '_commit_delta') as commit:
            self.assertEqual(store.upsert_many([ProductCard('1', keyword='乙', captured_at=stamp)]), [])
        commit.assert_not_called()

    def test_later_keyword_cannot_replace_an_existing_main_image(self):
        store = IncrementalProductStore(self.path)
        store.upsert_many([ProductCard('1', keyword='甲', main_image_url='https://img/main.jpg')])
        store.upsert_many([ProductCard('1', keyword='乙', main_image_url='https://img/secondary.jpg')])
        self.assertEqual(store.snapshot()[0].main_image_url, 'https://img/main.jpg')

    def test_abrupt_process_exit_recovers_committed_page_before_checkpoint(self):
        source = '''
import os, sys
sys.path.insert(0, sys.argv[2])
from wechat_shop_scraper.incremental_storage import IncrementalProductStore
from wechat_shop_scraper.model import ProductCard
store = IncrementalProductStore(sys.argv[1])
store.upsert_many([ProductCard('crash-test', keyword='床单')])
os._exit(17)
'''
        result = subprocess.run([sys.executable, '-c', source, str(self.path),
                                 str(Path(__file__).resolve().parents[1])],
                                capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 17, result.stderr)
        recovered = IncrementalProductStore(self.path)
        self.assertEqual(recovered.snapshot()[0].product_id, 'crash-test')
        self.assertTrue(recovered.export_csv())


if __name__ == '__main__':
    unittest.main()
