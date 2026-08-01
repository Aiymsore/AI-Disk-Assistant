from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import Advice


class AdviceCache:
    """Small SQLite cache keyed by model, privacy mode and file snapshot payload."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS advice_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    advice_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    @staticmethod
    def make_key(model: str, privacy_mode: str, payload: dict[str, Any]) -> str:
        material = json.dumps(
            {"model": model, "privacy_mode": privacy_mode, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Advice | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT advice_json FROM advice_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0])
            return Advice(**data)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def set(self, key: str, payload: dict[str, Any], advice: Advice) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO advice_cache(cache_key, payload_json, advice_json)
                VALUES (?, ?, ?)
                """,
                (
                    key,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(advice.to_dict(), ensure_ascii=False, sort_keys=True),
                ),
            )

    def clear(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM advice_cache")
