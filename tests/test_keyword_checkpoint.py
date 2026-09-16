from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wechat_shop_scraper.keyword_checkpoint import KeywordCheckpointStore
from wechat_shop_scraper.gui import ShopScraperApp, stopped_status


class KeywordCheckpointStoreTests(unittest.TestCase):
    def test_resume_returns_saved_current_keyword_index(self) -> None:
        keywords = ["关键词一", "关键词二", "关键词三"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keyword_checkpoint.json"
            store = KeywordCheckpointStore(path)

            store.save_position(keywords, 1)

            reloaded = KeywordCheckpointStore(path)
            self.assertEqual(reloaded.resume_index(keywords), 1)

    def test_saved_session_restores_keyword_queue_and_price_range(self) -> None:
        keywords = ["关键词一", "关键词二", "关键词三"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keyword_checkpoint.json"
            store = KeywordCheckpointStore(path)

            store.save_position(
                keywords,
                1,
                min_price=50,
                max_price=1000,
                restart_keyword_interval=20,
            )

            session = KeywordCheckpointStore(path).saved_session()
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.keywords, tuple(keywords))
            self.assertEqual(session.next_index, 1)
            self.assertEqual(session.min_price, 50)
            self.assertEqual(session.max_price, 1000)
            self.assertEqual(session.restart_keyword_interval, 20)
            self.assertEqual(
                store.resume_index(keywords, min_price=50, max_price=1000),
                1,
            )
            self.assertEqual(
                store.resume_index(keywords, min_price=30, max_price=1000),
                0,
            )

    def test_checkpoint_keeps_desktop_keyword_mode_separate(self) -> None:
        keywords = ["关键词一", "关键词二"]
        with tempfile.TemporaryDirectory() as directory:
            store = KeywordCheckpointStore(
                Path(directory) / "keyword_checkpoint.json"
            )
            store.save_position(
                keywords,
                1,
                mode="电脑版微信关键词采集",
            )

            session = store.saved_session()
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.mode, "电脑版微信关键词采集")
            self.assertEqual(
                store.resume_index(
                    keywords, mode="电脑版微信关键词采集"
                ),
                1,
            )
            self.assertEqual(
                store.resume_index(
                    keywords, mode="安卓手机关键词采集"
                ),
                0,
            )

    def test_legacy_checkpoint_only_resumes_in_android_mode(self) -> None:
        keywords = ["关键词一", "关键词二"]
        with tempfile.TemporaryDirectory() as directory:
            store = KeywordCheckpointStore(
                Path(directory) / "keyword_checkpoint.json"
            )
            store.save_position(keywords, 1)

            self.assertEqual(
                store.resume_index(keywords, mode="安卓手机关键词采集"),
                1,
            )
            self.assertEqual(
                store.resume_index(keywords, mode="电脑版微信关键词采集"),
                0,
            )

    def test_completed_keyword_advances_resume_position(self) -> None:
        keywords = ["关键词一", "关键词二", "关键词三"]
        with tempfile.TemporaryDirectory() as directory:
            store = KeywordCheckpointStore(
                Path(directory) / "keyword_checkpoint.json"
            )

            store.mark_finished(keywords, completed_index=1)

            self.assertEqual(store.resume_index(keywords), 2)

    def test_changed_keyword_list_does_not_reuse_stale_position(self) -> None:
        keywords = ["关键词一", "关键词二", "关键词三"]
        with tempfile.TemporaryDirectory() as directory:
            store = KeywordCheckpointStore(
                Path(directory) / "keyword_checkpoint.json"
            )
            store.save_position(keywords, 1)

            self.assertEqual(
                store.resume_index(["另一个词", "关键词二", "关键词三"]),
                0,
            )

    def test_legacy_position_can_resume_old_running_version_after_list_is_reentered(
        self,
    ) -> None:
        keywords = ["关键词一", "暂停关键词", "关键词三"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keyword_checkpoint.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 0,
                        "next_index": 1,
                        "total": 3,
                        "current_keyword": "暂停关键词",
                        "min_price": 30,
                        "max_price": 1000,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            store = KeywordCheckpointStore(path)

            self.assertEqual(
                store.resume_index(keywords, min_price=30, max_price=1000),
                1,
            )
            self.assertEqual(
                store.resume_index(keywords, min_price=50, max_price=1000),
                0,
            )
            self.assertIsNone(store.saved_session())

    def test_clear_removes_completed_batch_checkpoint(self) -> None:
        keywords = ["关键词一", "关键词二"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keyword_checkpoint.json"
            store = KeywordCheckpointStore(path)
            store.save_position(keywords, 1)

            store.clear()

            self.assertFalse(path.exists())
            self.assertEqual(store.resume_index(keywords), 0)

    def test_gui_records_next_position_after_keyword_finishes(self) -> None:
        keywords = ["关键词一", "关键词二", "关键词三"]
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = KeywordCheckpointStore(
                Path(directory) / "keyword_checkpoint.json"
            )
            app = object.__new__(ShopScraperApp)
            app._pending_keywords = keywords
            app._pending_min_price = None
            app._pending_max_price = None
            app._pending_restart_interval = 20
            app._checkpoint_store = checkpoint
            app._write_log = lambda _message: None

            app._record_keyword_finished("关键词一", 1, 3, "已完成")

            self.assertEqual(checkpoint.resume_index(keywords), 1)

    def test_paused_stop_status_keeps_actual_failure_reason(self) -> None:
        self.assertEqual(
            stopped_status(False, "筛选面板没有打开"),
            (
                "自动采集已暂停：筛选面板没有打开",
                "自动采集保持暂停，可修复后从断点继续",
            ),
        )
        self.assertEqual(
            stopped_status(False, None),
            ("已停止", "采集已停止"),
        )

    def test_gui_restores_saved_queue_prices_and_position(self) -> None:
        class FakeVariable:
            def __init__(self, value: str = "") -> None:
                self.value = value

            def set(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

        class FakeText:
            def __init__(self) -> None:
                self.value = ""
                self.state = "disabled"

            def configure(self, *, state: str) -> None:
                self.state = state

            def delete(self, _start: str, _end: str) -> None:
                self.value = ""

            def insert(self, _start: str, value: str) -> None:
                self.value = value

        keywords = ["关键词一", "关键词二", "关键词三"]
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = KeywordCheckpointStore(
                Path(directory) / "keyword_checkpoint.json"
            )
            checkpoint.save_position(
                keywords,
                1,
                min_price=50,
                max_price=1000,
                restart_keyword_interval=20,
            )
            app = object.__new__(ShopScraperApp)
            app._checkpoint_store = checkpoint
            app.mode_text = FakeVariable()
            app.min_price_text = FakeVariable()
            app.max_price_text = FakeVariable()
            app.restart_interval_text = FakeVariable()
            app.current_keyword_text = FakeVariable()
            app.keyword_entry = FakeText()
            app._resume_index = 0
            app._on_mode_changed = lambda: None
            logs: list[str] = []
            app._write_log = logs.append

            app._restore_keyword_session()

            self.assertEqual(app.mode_text.value, "安卓手机关键词采集")
            self.assertEqual(app.keyword_entry.value, "关键词一\n关键词二\n关键词三")
            self.assertEqual(app.min_price_text.value, "50")
            self.assertEqual(app.max_price_text.value, "1000")
            self.assertEqual(app.restart_interval_text.value, "20")
            self.assertEqual(app._resume_index, 1)
            self.assertIn("第 2/3 个关键词", logs[0])


if __name__ == "__main__":
    unittest.main()
