from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wechat_shop_scraper.app_data import (
    cache_usage,
    clear_runtime_cache,
    default_output_directory,
    format_bytes,
    prepare_frozen_tk_runtime,
)


class AppDataTests(unittest.TestCase):
    def test_default_output_directory_depends_on_mode(self) -> None:
        root = Path("C:/portable/data")

        self.assertEqual(
            default_output_directory(root, keyword_mode=False),
            root,
        )
        self.assertEqual(
            default_output_directory(root, keyword_mode=True),
            root / "关键词采集汇总",
        )

    def test_clear_runtime_cache_preserves_neighboring_product_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            cache_dir = data_dir / ".cache"
            debug_dir = cache_dir / "debug"
            debug_dir.mkdir(parents=True)
            (debug_dir / "capture.bin").write_bytes(b"debug-data")
            (cache_dir / "session.tmp").write_bytes(b"123")
            product_file = data_dir / "products.json"
            product_file.write_text('{"count": 1}', encoding="utf-8")
            checkpoint_file = data_dir / "keyword_checkpoint.json"
            checkpoint_file.write_text('{"next_index": 2}', encoding="utf-8")

            self.assertEqual(cache_usage(cache_dir), (2, 13))
            removed = clear_runtime_cache(cache_dir)

            self.assertEqual(removed, (2, 13))
            self.assertEqual(cache_usage(cache_dir), (0, 0))
            self.assertTrue(product_file.is_file())
            self.assertTrue(checkpoint_file.is_file())

    def test_clear_runtime_cache_rejects_non_cache_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "\\.cache"):
                clear_runtime_cache(Path(directory) / "data")

    def test_format_bytes_is_readable(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1536), "1.5 KB")

    def test_prepare_frozen_tk_runtime_copies_scripts_and_sets_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            internal = root / "中文便携目录" / "_internal"
            (internal / "_tcl_data").mkdir(parents=True)
            (internal / "_tk_data").mkdir(parents=True)
            (internal / "_tcl_data" / "init.tcl").write_text(
                "tcl", encoding="utf-8"
            )
            (internal / "_tk_data" / "tk.tcl").write_text(
                "tk", encoding="utf-8"
            )
            storage = root / "runtime"

            with mock.patch.dict(os.environ, {}, clear=False):
                prepared = prepare_frozen_tk_runtime(internal, storage)

                self.assertEqual(prepared, storage / "tk-runtime-8.6")
                self.assertTrue((prepared / "tcl" / "init.tcl").is_file())
                self.assertTrue((prepared / "tk" / "tk.tcl").is_file())
                self.assertEqual(
                    os.environ["TCL_LIBRARY"], str(prepared / "tcl")
                )
                self.assertEqual(os.environ["TK_LIBRARY"], str(prepared / "tk"))


if __name__ == "__main__":
    unittest.main()
