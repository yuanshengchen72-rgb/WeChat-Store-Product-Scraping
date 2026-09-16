from __future__ import annotations

import csv
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .model import ProductCard


CSV_COLUMNS = (
    ("商品ID", "product_id"),
    ("关键词", "keyword"),
    ("店铺名", "shop_name"),
    ("标题", "title"),
    ("主图链接", "main_image_url"),
    ("价格（分）", "price_fen"),
    ("价格", "display_price"),
    ("销量", "sales"),
    ("销量文本", "sales_text"),
    ("销量是否精确", "sales_exact"),
    ("采集时间", "captured_at"),
    ("来源", "source"),
)
CSV_ROWS_PER_FILE = 1000
KEYWORD_SEPARATOR = "｜"
CSV_EXPORT_PATTERN = re.compile(
    r"^products_(\d{3})_(\d{8}_\d{6})\.csv$"
)
EXPORT_STAMP_PATTERN = re.compile(r"^\d{8}_\d{6}$")


class ProductStore:
    def __init__(self, output_dir: str | Path, *, flush_existing: bool = True) -> None:
        self.output_dir = Path(output_dir)
        self.json_path = self.output_dir / "products.json"
        self._export_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_path = self.output_dir / f"products_001_{self._export_stamp}.csv"
        self.csv_export_error: str | None = None
        self._lock = threading.RLock()
        self._items: dict[str, ProductCard] = {}
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.load()
        self.csv_path = self.output_dir / f"products_001_{self._export_stamp}.csv"
        if self._items and flush_existing:
            with self._lock:
                self._flush_locked()

    def load(self) -> None:
        with self._lock:
            if not self.json_path.is_file():
                return
            try:
                raw = json.loads(self.json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return
            if isinstance(raw, dict):
                saved_stamp = str(raw.get("export_stamp") or "").strip()
                if EXPORT_STAMP_PATTERN.fullmatch(saved_stamp):
                    self._export_stamp = saved_stamp
                else:
                    legacy_stamps = sorted(
                        match.group(2)
                        for path in self.output_dir.glob("products_*.csv")
                        if (match := CSV_EXPORT_PATTERN.fullmatch(path.name))
                    )
                    if legacy_stamps:
                        self._export_stamp = legacy_stamps[0]
            rows = raw.get("products", []) if isinstance(raw, dict) else raw
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                try:
                    card = ProductCard.from_dict(row)
                except (TypeError, ValueError):
                    continue
                if card.product_id:
                    self._items[card.product_id] = card

    def upsert_many(self, cards: Iterable[ProductCard]) -> list[ProductCard]:
        changed: list[ProductCard] = []
        with self._lock:
            for card in cards:
                if not card.product_id:
                    continue
                old = self._items.get(card.product_id)
                if old is None:
                    self._items[card.product_id] = card
                    changed.append(card)
                    continue
                before = old.to_dict()
                self._merge(old, card)
                if old.to_dict() != before:
                    changed.append(old)
            if changed:
                self._flush_locked()
        return changed

    @staticmethod
    def _merge(old: ProductCard, new: ProductCard) -> None:
        if new.keyword:
            existing_keywords = [
                item.strip()
                for item in old.keyword.split(KEYWORD_SEPARATOR)
                if item.strip()
            ]
            for keyword in (
                item.strip()
                for item in new.keyword.split(KEYWORD_SEPARATOR)
                if item.strip()
            ):
                if keyword not in existing_keywords:
                    existing_keywords.append(keyword)
            old.keyword = KEYWORD_SEPARATOR.join(existing_keywords)
        for field in (
            "title",
            "price_text",
            "sales_text",
            "shop_name",
            "source",
        ):
            value = getattr(new, field)
            if value:
                setattr(old, field, value)
        # A product may appear under many keywords. Search-card responses can
        # expose a secondary/thumbnail image on later appearances; keep the
        # first explicit main image instead of replacing it with that image.
        if new.main_image_url and not old.main_image_url:
            old.main_image_url = new.main_image_url
        if new.price_fen is not None:
            old.price_fen = new.price_fen
        if new.sales is not None:
            if old.sales is None or new.sales_exact or not old.sales_exact or new.sales >= old.sales:
                old.sales = new.sales
                old.sales_exact = new.sales_exact
        if new.captured_at:
            old.captured_at = new.captured_at

    def _flush_locked(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ordered = sorted(self._items.values(), key=lambda card: (card.title, card.product_id))
        payload = {
            "count": len(ordered),
            "export_stamp": self._export_stamp,
            "products": [card.to_dict() for card in ordered],
        }
        json_tmp = self.json_path.with_suffix(".json.tmp")
        json_tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(json_tmp, self.json_path)

        self._export_csv_locked(ordered)

    def export_csv(self) -> bool:
        with self._lock:
            ordered = sorted(
                self._items.values(), key=lambda card: (card.title, card.product_id)
            )
            return self._export_csv_locked(ordered)

    def _export_csv_locked(self, ordered: list[ProductCard]) -> bool:
        self.csv_export_error = None
        expected_paths: set[Path] = set()

        for chunk_index, start in enumerate(
            range(0, len(ordered), CSV_ROWS_PER_FILE), start=1
        ):
            csv_path = self.output_dir / (
                f"products_{chunk_index:03d}_{self._export_stamp}.csv"
            )
            expected_paths.add(csv_path)
            csv_tmp = csv_path.with_suffix(".csv.tmp")
            try:
                with csv_tmp.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(
                        handle,
                        fieldnames=tuple(label for label, _key in CSV_COLUMNS),
                    )
                    writer.writeheader()
                    for card in ordered[start : start + CSV_ROWS_PER_FILE]:
                        row = card.to_dict()
                        writer.writerow(
                            {label: row.get(key, "") for label, key in CSV_COLUMNS}
                        )
                os.replace(csv_tmp, csv_path)
            except OSError as exc:
                self.csv_export_error = f"{type(exc).__name__}: {exc}"
                try:
                    csv_tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return False

        if ordered:
            for old_path in self.output_dir.glob("products_*.csv"):
                if (
                    CSV_EXPORT_PATTERN.fullmatch(old_path.name)
                    and old_path not in expected_paths
                ):
                    try:
                        old_path.unlink()
                    except OSError as exc:
                        self.csv_export_error = f"{type(exc).__name__}: {exc}"
                        return False
        return True

    def snapshot(self) -> list[ProductCard]:
        with self._lock:
            return list(self._items.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
