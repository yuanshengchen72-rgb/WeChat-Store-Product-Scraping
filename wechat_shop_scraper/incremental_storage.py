"""Durable per-page after-images, with compatible JSON/CSV snapshots.

Only one collector may own a batch directory. Keep the pending database with
products.json when copying a running/interrupted batch. A clean stop exports
everything, making that snapshot readable by older releases as well.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Iterable

from .model import ProductCard
from .storage import EXPORT_STAMP_PATTERN, ProductStore


class IncrementalProductStore(ProductStore):
    def __init__(self, output_dir: str | Path) -> None:
        # The legacy loader tolerates corrupt JSON. Never allow an incomplete
        # snapshot to replace a user's existing batch in incremental mode.
        path = Path(output_dir) / 'products.json'
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
                rows = raw.get('products') if isinstance(raw, dict) else raw
                if not isinstance(rows, list):
                    raise ValueError('商品列表格式不正确')
                for row in rows:
                    if not isinstance(row, dict) or not ProductCard.from_dict(row).product_id:
                        raise ValueError('商品记录格式不正确')
            except (ValueError, TypeError) as exc:
                raise OSError('原有 products.json 无法读取；已保留原文件，请检查后重试') from exc
        super().__init__(output_dir, flush_existing=False)
        self.pending_path = self.output_dir / 'products.pending.sqlite3'
        self._export_lock = threading.Lock()
        self._revision = 0
        self._exported_revision = 0
        with self._database() as db:
            db.execute('CREATE TABLE IF NOT EXISTS pending '
                       '(product_id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            meta = dict(db.execute('SELECT key, value FROM metadata'))
            stamp = meta.get('export_stamp', '')
            if EXPORT_STAMP_PATTERN.fullmatch(stamp):
                self._export_stamp = stamp
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', ('export_stamp', self._export_stamp))
            try:
                self._revision = int(meta.get('revision', '0'))
                pending = list(db.execute('SELECT product_id, payload, revision FROM pending'))
                for product_id, payload, revision in pending:
                    row = json.loads(payload)
                    if not isinstance(row, dict):
                        raise ValueError('增量商品格式不正确')
                    card = ProductCard.from_dict(row)
                    if card.product_id != product_id:
                        raise ValueError('增量商品 ID 不匹配')
                    self._items[product_id] = card
                    self._revision = max(self._revision, revision)
            except (ValueError, TypeError) as exc:
                raise OSError('商品增量数据无法读取；已保留文件，请检查后重试') from exc
            self._exported_revision = -1 if pending else self._revision
        self.csv_path = self.output_dir / f'products_001_{self._export_stamp}.csv'

    @contextmanager
    def _database(self):
        db = None
        try:
            db = sqlite3.connect(self.pending_path, timeout=5)
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        except sqlite3.Error as exc:
            raise OSError(f'商品增量保存失败：{exc}') from exc
        finally:
            if db is not None:
                db.close()

    @property
    def needs_export(self) -> bool:
        with self._lock:
            return self._revision != self._exported_revision

    def _commit_delta(self, cards: list[ProductCard], revision: int) -> None:
        with self._database() as db:
            db.executemany('INSERT OR REPLACE INTO pending VALUES (?, ?, ?)',
                           [(card.product_id, json.dumps(card.to_dict(), ensure_ascii=False,
                                                        separators=(',', ':')), revision)
                            for card in cards])
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', ('revision', str(revision)))

    def upsert_many(self, cards: Iterable[ProductCard]) -> list[ProductCard]:
        with self._lock:
            candidates: dict[str, ProductCard] = {}
            for card in cards:
                if not card.product_id:
                    continue
                old = candidates.get(card.product_id, self._items.get(card.product_id))
                merged = replace(old) if old is not None else replace(card)
                if old is not None:
                    self._merge(merged, card)
                candidates[card.product_id] = merged
            changed = [card for key, card in candidates.items()
                       if key not in self._items or card != self._items[key]]
            if not changed:
                return []
            revision = self._revision + 1
            # Commit before publishing rows or allowing the GUI to checkpoint.
            # After-images replay idempotently even after a snapshot/clear crash.
            self._commit_delta(changed, revision)
            self._items.update((card.product_id, card) for card in changed)
            self._revision = revision
            return [replace(card) for card in changed]

    def snapshot(self) -> list[ProductCard]:
        with self._lock:
            return [replace(card) for card in self._items.values()]

    def _clear_exported(self, revision: int) -> None:
        with self._database() as db:
            db.execute('DELETE FROM pending WHERE revision <= ?', (revision,))

    def export_csv(self) -> bool:
        # Called on an export worker, not the Tk thread. Writers never mutate
        # the published objects, so serialization needs no long-held item lock.
        with self._export_lock:
            with self._lock:
                revision = self._revision
                ordered = list(self._items.values())
            ordered.sort(key=lambda card: (card.title, card.product_id))
            self.csv_export_error = None
            try:
                payload = {'count': len(ordered), 'export_stamp': self._export_stamp,
                           'products': [card.to_dict() for card in ordered]}
                temporary = self.json_path.with_suffix('.json.tmp')
                with temporary.open('w', encoding='utf-8') as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.json_path)
                if not self._export_csv_locked(ordered):
                    return False
                # Preserve every newer page committed while exporting. The
                # replacement snapshot is durable before removing any delta.
                with self._lock:
                    self._clear_exported(revision)
                    self._exported_revision = revision
            except OSError as exc:
                self.csv_export_error = f'{type(exc).__name__}: {exc}'
                return False
            return True
