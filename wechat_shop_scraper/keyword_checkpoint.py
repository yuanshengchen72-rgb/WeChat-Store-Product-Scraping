from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def _normalized_keywords(keywords: Iterable[str]) -> list[str]:
    return [str(keyword).strip() for keyword in keywords if str(keyword).strip()]


def _keyword_fingerprint(keywords: list[str]) -> str:
    payload = json.dumps(keywords, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KeywordCheckpointSession:
    keywords: tuple[str, ...]
    next_index: int
    min_price: int | None
    max_price: int | None
    restart_keyword_interval: int
    mode: str = ""


class KeywordCheckpointStore:
    """Persist the next keyword index for one stable keyword queue."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read_payload(self) -> dict[str, object] | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def saved_session(self) -> KeywordCheckpointSession | None:
        payload = self._read_payload()
        if payload is None:
            return None
        raw_keywords = payload.get("keywords")
        if not isinstance(raw_keywords, list):
            return None
        normalized = _normalized_keywords(raw_keywords)
        if not normalized or len(normalized) != len(raw_keywords):
            return None
        if payload.get("keyword_fingerprint") != _keyword_fingerprint(normalized):
            return None
        try:
            next_index = int(payload.get("next_index"))
        except (TypeError, ValueError):
            return None
        if not 0 <= next_index < len(normalized):
            return None
        if payload.get("current_keyword") != normalized[next_index]:
            return None

        def optional_price(name: str) -> int | None:
            value = payload.get(name)
            if value is None:
                return None
            if isinstance(value, bool):
                raise ValueError
            parsed = int(value)
            if parsed < 0:
                raise ValueError
            return parsed

        try:
            min_price = optional_price("min_price")
            max_price = optional_price("max_price")
            raw_restart_interval = payload.get("restart_keyword_interval", 30)
            if isinstance(raw_restart_interval, bool):
                raise ValueError
            restart_keyword_interval = int(raw_restart_interval)
            if restart_keyword_interval < 0:
                raise ValueError
        except (TypeError, ValueError):
            return None
        if (
            min_price is not None
            and max_price is not None
            and min_price > max_price
        ):
            return None
        return KeywordCheckpointSession(
            keywords=tuple(normalized),
            next_index=next_index,
            min_price=min_price,
            max_price=max_price,
            restart_keyword_interval=restart_keyword_interval,
            mode=str(payload.get("mode") or "").strip(),
        )

    def resume_index(
        self,
        keywords: Iterable[str],
        min_price: int | None = None,
        max_price: int | None = None,
        mode: str = "",
    ) -> int:
        normalized = _normalized_keywords(keywords)
        if not normalized:
            return 0
        session = self.saved_session()
        if session is None:
            payload = self._read_payload()
            if payload is None or payload.get("version") != 0:
                return 0
            try:
                next_index = int(payload.get("next_index"))
                total = int(payload.get("total"))
            except (TypeError, ValueError):
                return 0
            if total != len(normalized) or not 0 <= next_index < total:
                return 0
            if payload.get("current_keyword") != normalized[next_index]:
                return 0
            if payload.get("min_price") != min_price:
                return 0
            if payload.get("max_price") != max_price:
                return 0
            return next_index
        if session.keywords != tuple(normalized):
            return 0
        if mode:
            if session.mode and session.mode != mode:
                return 0
            if not session.mode and "安卓手机" not in mode:
                return 0
        if session.min_price != min_price or session.max_price != max_price:
            return 0
        return session.next_index

    def save_position(
        self,
        keywords: Iterable[str],
        next_index: int,
        min_price: int | None = None,
        max_price: int | None = None,
        restart_keyword_interval: int = 30,
        mode: str = "",
    ) -> None:
        normalized = _normalized_keywords(keywords)
        if not normalized:
            self.clear()
            return
        if not 0 <= next_index < len(normalized):
            raise ValueError("关键词断点位置超出列表范围")
        if isinstance(restart_keyword_interval, bool) or restart_keyword_interval < 0:
            raise ValueError("微信重启间隔必须是非负整数")
        payload = {
            "version": 3,
            "keyword_fingerprint": _keyword_fingerprint(normalized),
            "keywords": normalized,
            "next_index": next_index,
            "total": len(normalized),
            "current_keyword": normalized[next_index],
            "min_price": min_price,
            "max_price": max_price,
            "restart_keyword_interval": restart_keyword_interval,
            "mode": str(mode or "").strip(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_path, self.path)

    def mark_finished(
        self,
        keywords: Iterable[str],
        completed_index: int,
        min_price: int | None = None,
        max_price: int | None = None,
        restart_keyword_interval: int = 30,
        mode: str = "",
    ) -> None:
        normalized = _normalized_keywords(keywords)
        next_index = completed_index + 1
        if next_index >= len(normalized):
            self.clear()
            return
        self.save_position(
            normalized,
            next_index,
            min_price=min_price,
            max_price=max_price,
            restart_keyword_interval=restart_keyword_interval,
            mode=mode,
        )

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            return
