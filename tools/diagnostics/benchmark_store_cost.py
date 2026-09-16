"""Isolated synthetic-data timing. Never opens WeChat or the formal batch."""
from pathlib import Path
import json
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from wechat_shop_scraper.model import ProductCard
from wechat_shop_scraper.storage import ProductStore
from wechat_shop_scraper.incremental_storage import IncrementalProductStore


def card(index):
    return ProductCard(str(1000000000000 + index),
        keyword='｜'.join(f'家纺测试关键词{number}' for number in range(35)),
        title=f'隔离测试全棉家纺四件套 {index:06d}',
        main_image_url='https://example.test/' + 'image' * 30 + '.jpg',
        price_fen=9900, sales=123, sales_text='已售123', shop_name='隔离测试店铺',
        captured_at='2026-09-10T00:00:00', source='synthetic-benchmark')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    # Keep diagnostic outputs in a clearly separate, newly allocated workspace
    # directory. Do not load/overwrite/delete any pre-existing user file.
    target = Path(tempfile.mkdtemp(prefix='store-cost-', dir=ROOT/'build'))
    for size in (1000, 6600, 15000):
        store = ProductStore(target/str(size))
        store.upsert_many(card(i) for i in range(size))
        elapsed = []
        csv_time = []
        original = store._export_csv_locked
        def timed_export(ordered):
            started = time.perf_counter()
            result = original(ordered)
            csv_time.append(time.perf_counter()-started)
            return result
        store._export_csv_locked = timed_export
        for iteration in range(3):
            started = time.perf_counter()
            changed = store.upsert_many(card(size+iteration*10+i) for i in range(10))
            elapsed.append(time.perf_counter()-started)
            assert len(changed) == 10
        incremental = IncrementalProductStore(store.output_dir)
        faster = []
        for iteration in range(10):
            started = time.perf_counter()
            changed = incremental.upsert_many(card(size+30+iteration*10+i) for i in range(10))
            faster.append(time.perf_counter()-started)
            assert len(changed) == 10
        export_started = time.perf_counter()
        assert incremental.export_csv()
        export_elapsed = time.perf_counter() - export_started
        assert len(IncrementalProductStore(store.output_dir)) == size+130
        print(json.dumps({'existing_products':size, 'added_per_page':10,
            'median_save_seconds':round(statistics.median(elapsed),3),
            'median_csv_seconds':round(statistics.median(csv_time),3),
            'incremental_median_save_seconds':round(statistics.median(faster),4),
            'incremental_full_export_seconds':round(export_elapsed,3),
            'json_bytes':store.json_path.stat().st_size,
            'csv_files_rewritten_per_page':len(list(store.output_dir.glob('*.csv')))},
            ensure_ascii=False), flush=True)
    print('SYNTHETIC_ONLY', target, flush=True)


if __name__ == '__main__':
    main()
