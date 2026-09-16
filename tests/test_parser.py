import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wechat_shop_scraper.model import ProductCard
from wechat_shop_scraper.parser import ProductCardParser
from wechat_shop_scraper.storage import ProductStore


class ProductCardParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = ProductCardParser()

    def test_parses_json_product_cards(self) -> None:
        payload = {
            "data": {
                "product_cards": [
                    {
                        "product_id": "10000730292747",
                        "title": "时尚手提包",
                        "headimg_url": "https://example.com/a.jpg?token=1",
                        "selling_price": 14900,
                        "total_sold_num": 926,
                        "platform_name": "测试箱包店",
                    }
                ]
            }
        }
        cards = self.parser.parse(json.dumps(payload, ensure_ascii=False).encode())
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card.product_id, "10000730292747")
        self.assertEqual(card.title, "时尚手提包")
        self.assertEqual(card.main_image_url, "https://example.com/a.jpg?token=1")
        self.assertEqual(card.price_fen, 14900)
        self.assertEqual(card.sales, 926)
        self.assertEqual(card.shop_name, "测试箱包店")
        self.assertTrue(card.sales_exact)

    def test_parses_native_product_cards_and_hex_id(self) -> None:
        decimal_id = 10000762739750
        hex_id = hex(decimal_id)
        text = (
            "response:{product_cards:["
            "{product_id:%s,title:\"条纹托特包\","
            "cover_url:\"https://img.example.com/b.webp\","
            "selling_price:13410,sales_wording:\"已售1.2万+\"}]}" % hex_id
        )
        cards = self.parser.parse(text)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card.product_id, str(decimal_id))
        self.assertEqual(card.price_fen, 13410)
        self.assertEqual(card.sales, 12000)
        self.assertFalse(card.sales_exact)
        self.assertEqual(card.sales_text, "已售1.2万+")

    def test_price_with_yuan_symbol_is_converted_to_fen(self) -> None:
        text = (
            '{"product_cards":[{"product_id":"10000123456789",'
            '"title":"测试商品","img_url":"https://x.test/a.jpg",'
            '"selling_price":"¥19.90","sold":12}]}'
        )
        card = self.parser.parse(text)[0]
        self.assertEqual(card.price_fen, 1990)
        self.assertEqual(card.display_price, "¥19.90")

    def test_parses_multiple_native_cards(self) -> None:
        text = (
            "product_cards:["
            '{product_id:10000123456789,title:"商品一",img_url:"https://x/1.jpg",'
            'selling_price:1000,sold:10},'
            '{product_id:10000123456790,title:"商品二",img_url:"https://x/2.jpg",'
            "selling_price:2000,sold:20}]"
        )
        cards = self.parser.parse(text)
        self.assertEqual({card.product_id for card in cards}, {"10000123456789", "10000123456790"})

    def test_parses_lz4_wrapped_stringified_listing_response(self) -> None:
        listing = {
            "rid": "fixture",
            "msg": {
                "product_cards": [
                    {
                        "product_id": "10000732092276",
                        "title": "红色尼龙托特包",
                        "img_urls": ["https://img.example.com/main.jpg"],
                        "selling_price": 5900,
                        "extra_data": json.dumps({"sales": 1}),
                        "platform_name": "叫我小唐ZT",
                    }
                ]
            },
        }
        outer = json.dumps(
            {"data": json.dumps(listing, ensure_ascii=False)}, ensure_ascii=False
        ).encode("utf-8")
        compressed = self._lz4_literal_block(b"ok\x1a" + outer)
        payload = bytes(0x4E) + compressed

        cards = self.parser.parse(payload, source="GetStoreWindowProducts")

        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card.product_id, "10000732092276")
        self.assertEqual(card.title, "红色尼龙托特包")
        self.assertEqual(card.main_image_url, "https://img.example.com/main.jpg")
        self.assertEqual(card.price_fen, 5900)
        self.assertEqual(card.sales, 1)
        self.assertEqual(card.shop_name, "叫我小唐ZT")

    def test_zero_sales_in_product_extra_data_wins_over_outer_statistic(self) -> None:
        """A real mobile ECS payload may expose unrelated sales on its wrapper."""
        payload = {
            "product_id": "10001412263523",
            "sales": 55851,
            "product_card": {
                "product_id": "10001412263523",
                "title": "已卖完的测试商品",
                "img_urls": ["https://img.example.com/sold-out.jpg"],
                "selling_price": 8990,
                "platform_name": "雅鹿令梭鞋业",
                "extra_data": json.dumps({"sales": 0}),
            },
        }

        cards = self.parser.parse(json.dumps(payload, ensure_ascii=False))

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].sales, 0)
        self.assertTrue(cards[0].sales_exact)

    def test_nested_shop_sales_is_not_used_as_product_sales(self) -> None:
        payload = {
            "product_cards": [
                {
                    "product_id": "10001412263523",
                    "title": "无商品销量字段",
                    "img_urls": ["https://img.example.com/no-sales.jpg"],
                    "selling_price": 8990,
                    "platform_name": "雅鹿令梭鞋业",
                    "shop_sales": {"sales": 55851},
                }
            ]
        }

        card = self.parser.parse(json.dumps(payload, ensure_ascii=False))[0]

        self.assertEqual(card.sales, 0)
        self.assertNotEqual(card.sales, 55851)

    @staticmethod
    def _lz4_literal_block(payload: bytes) -> bytes:
        literal_length = len(payload)
        token = min(literal_length, 15) << 4
        encoded = bytearray([token])
        if literal_length >= 15:
            remaining = literal_length - 15
            while remaining >= 255:
                encoded.append(255)
                remaining -= 255
            encoded.append(remaining)
        encoded.extend(payload)
        return bytes(encoded)


class ProductStoreTests(unittest.TestCase):
    def test_legacy_batch_consolidates_timestamped_csv_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            card = ProductCard("10000123456789", title="历史商品")
            (output_dir / "products.json").write_text(
                json.dumps(
                    {"count": 1, "products": [card.to_dict()]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (output_dir / "products_001_20260904_100632.csv").write_text(
                "旧文件一", encoding="utf-8"
            )
            (output_dir / "products_001_20260904_124446.csv").write_text(
                "旧文件二", encoding="utf-8"
            )

            store = ProductStore(output_dir)

            self.assertEqual(
                [path.name for path in output_dir.glob("products_*.csv")],
                ["products_001_20260904_100632.csv"],
            )
            saved = json.loads(store.json_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["export_stamp"], "20260904_100632")

    def test_reopening_same_batch_reuses_one_csv_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("wechat_shop_scraper.storage.datetime") as clock:
                clock.now.return_value.strftime.return_value = "20260904_100632"
                first = ProductStore(Path(directory))
                first.upsert_many(
                    [ProductCard("10000123456789", title="商品一")]
                )

                clock.now.return_value.strftime.return_value = "20260904_124446"
                resumed = ProductStore(Path(directory))
                resumed.upsert_many(
                    [ProductCard("10000123456790", title="商品二")]
                )

            csv_files = sorted(Path(directory).glob("products_*.csv"))
            self.assertEqual(
                [path.name for path in csv_files],
                ["products_001_20260904_100632.csv"],
            )
            with csv_files[0].open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 2)

    def test_same_product_merges_all_matched_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProductStore(Path(directory))
            store.upsert_many(
                [
                    ProductCard(
                        "10000123456789",
                        keyword="男士开车专用鞋",
                        title="软底男鞋",
                    )
                ]
            )
            store.upsert_many(
                [
                    ProductCard(
                        "10000123456789",
                        keyword="商务休闲鞋",
                        title="软底男鞋",
                        sales=20,
                    ),
                    ProductCard(
                        "10000123456789",
                        keyword="男士开车专用鞋",
                        title="软底男鞋",
                    ),
                ]
            )

            self.assertEqual(len(store), 1)
            self.assertEqual(
                store.snapshot()[0].keyword,
                "男士开车专用鞋｜商务休闲鞋",
            )

    def test_upsert_merges_partial_rows_and_writes_csv_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProductStore(Path(directory))
            store.upsert_many(
                [
                    ProductCard(
                        "10000123456789",
                        keyword="测试词",
                        title="标题",
                        price_fen=1000,
                        shop_name="测试店铺",
                    )
                ]
            )
            store.upsert_many(
                [
                    ProductCard(
                        "10000123456789",
                        main_image_url="https://x.test/main.jpg",
                        sales=20,
                    )
                ]
            )
            snapshot = store.snapshot()
            self.assertEqual(len(snapshot), 1)
            self.assertEqual(snapshot[0].title, "标题")
            self.assertEqual(snapshot[0].shop_name, "测试店铺")
            self.assertEqual(snapshot[0].sales, 20)
            self.assertTrue(store.json_path.is_file())
            self.assertTrue(store.csv_path.is_file())
            self.assertRegex(
                store.csv_path.name,
                r"^products_001_\d{8}_\d{6}\.csv$",
            )
            saved = json.loads(store.json_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["count"], 1)
            with store.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual(csv_rows[0]["商品ID"], "10000123456789")
            self.assertEqual(csv_rows[0]["关键词"], "测试词")
            self.assertEqual(csv_rows[0]["店铺名"], "测试店铺")
            self.assertEqual(csv_rows[0]["标题"], "标题")
            self.assertEqual(csv_rows[0]["销量"], "20")

    def test_csv_is_split_every_1000_products(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProductStore(Path(directory))
            cards = [
                ProductCard(
                    str(10000000000000 + index),
                    title=f"商品{index}",
                    main_image_url=f"https://img.example.com/{index}.jpg",
                    price_fen=1000 + index,
                    sales=index,
                    shop_name="分卷测试店",
                )
                for index in range(2001)
            ]

            store.upsert_many(cards)

            stamp = store.csv_path.stem.removeprefix("products_001_")

            for index, expected_count in enumerate((1000, 1000, 1), start=1):
                path = Path(directory) / f"products_{index:03d}_{stamp}.csv"
                self.assertTrue(path.is_file())
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), expected_count)
                self.assertEqual(rows[0]["店铺名"], "分卷测试店")
                self.assertIn("关键词", rows[0])
                self.assertIn("主图链接", rows[0])
            self.assertFalse(
                (Path(directory) / f"products_004_{stamp}.csv").exists()
            )

    def test_locked_csv_does_not_abort_captured_products(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProductStore(Path(directory))
            cards = [
                ProductCard(
                    str(10000000000000 + index),
                    title=f"商品{index}",
                    shop_name="占用测试店",
                )
                for index in range(10)
            ]
            real_replace = __import__("os").replace

            def replace_with_locked_csv(source: object, destination: object) -> None:
                if Path(destination).suffix == ".csv":
                    raise PermissionError("CSV is open in WPS")
                real_replace(source, destination)

            with mock.patch(
                "wechat_shop_scraper.storage.os.replace",
                side_effect=replace_with_locked_csv,
            ):
                changed = store.upsert_many(cards)

            self.assertEqual(len(changed), 10)
            self.assertEqual(len(store), 10)
            saved = json.loads(store.json_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["count"], 10)
            self.assertIsNotNone(store.csv_export_error)


class ModuleImportTests(unittest.TestCase):
    def test_windows_modules_import_without_frida_installed(self) -> None:
        from wechat_shop_scraper import frida_bridge, gui, mobile_keyword, scroller

        self.assertTrue(frida_bridge.LISTENER_JS)
        self.assertTrue(hasattr(gui, "ShopScraperApp"))
        self.assertTrue(mobile_keyword.PRODUCTS_EXPRESSION)
        self.assertTrue(hasattr(scroller, "AutoScroller"))

    def test_keyword_output_directory_is_windows_safe(self) -> None:
        from wechat_shop_scraper.gui import safe_keyword_directory

        self.assertEqual(safe_keyword_directory('马丁靴:男/皮鞋*'), "马丁靴_男_皮鞋")

    def test_frozen_app_writes_beside_executable(self) -> None:
        from wechat_shop_scraper import gui

        executable = Path("C:/portable/微信小店商品列表采集.exe")
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
            sys, "executable", str(executable)
        ):
            self.assertEqual(gui.application_dir(), executable.parent)

    def test_portable_runtime_self_test(self) -> None:
        from wechat_shop_scraper.selftest import run_self_test

        self.assertEqual(run_self_test(), 0)

    def test_gui_table_follows_latest_captured_product(self) -> None:
        from wechat_shop_scraper.gui import ShopScraperApp

        class FakeTable:
            def __init__(self) -> None:
                self.seen = ""

            def exists(self, item_id: str) -> bool:
                return bool(item_id)

            def see(self, item_id: str) -> None:
                self.seen = item_id

        app = object.__new__(ShopScraperApp)
        app.table = FakeTable()
        cards = [ProductCard("10000123456789"), ProductCard("10000123456790")]

        app._show_latest(cards)

        self.assertEqual(app.table.seen, "10000123456790")

    def test_gui_displays_products_while_csv_is_locked(self) -> None:
        from wechat_shop_scraper.gui import ShopScraperApp

        class FakeVariable:
            def __init__(self) -> None:
                self.value = ""

            def set(self, value: str) -> None:
                self.value = value

        class FakeTable:
            def __init__(self) -> None:
                self.rows: dict[str, tuple[object, ...]] = {}
                self.seen = ""

            def exists(self, item_id: str) -> bool:
                return item_id in self.rows

            def insert(
                self,
                _parent: str,
                _index: str,
                *,
                iid: str,
                values: tuple[object, ...],
            ) -> None:
                self.rows[iid] = values

            def item(self, item_id: str, *, values: tuple[object, ...]) -> None:
                self.rows[item_id] = values

            def see(self, item_id: str) -> None:
                self.seen = item_id

        with tempfile.TemporaryDirectory() as directory:
            app = object.__new__(ShopScraperApp)
            app.store = ProductStore(Path(directory))
            app.table = FakeTable()
            app.status_text = FakeVariable()
            app.count_text = FakeVariable()
            app._last_csv_export_error = None
            app.running = True
            logs: list[str] = []
            app._write_log = logs.append
            cards = [
                ProductCard(str(10000000000000 + index), title=f"商品{index}")
                for index in range(10)
            ]
            real_replace = __import__("os").replace

            def replace_with_locked_csv(source: object, destination: object) -> None:
                if Path(destination).suffix == ".csv":
                    raise PermissionError("CSV is open in WPS")
                real_replace(source, destination)

            with mock.patch(
                "wechat_shop_scraper.storage.os.replace",
                side_effect=replace_with_locked_csv,
            ):
                app._handle_products(cards)
                # Export notifications are now delivered by the background
                # export worker, not by each page's product event.
                app._report_csv_export(app.store.csv_export_error is None)

            self.assertEqual(len(app.table.rows), 10)
            self.assertEqual(app.table.seen, cards[-1].product_id)
            self.assertIn("已保存 10 个商品", app.count_text.value)
            self.assertTrue(any("WPS/Excel" in message for message in logs))


if __name__ == "__main__":
    unittest.main()
