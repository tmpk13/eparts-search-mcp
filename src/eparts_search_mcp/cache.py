"""SQLite response cache and daily-usage store.

Caching is not a nicety here: both distributors grant on the order of a
thousand calls per day, and an agent exploring a parts question will repeat
the same query many times in one session.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS response_cache (
    key        TEXT PRIMARY KEY,
    provider   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    stored_at  REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_expiry ON response_cache (expires_at);

CREATE TABLE IF NOT EXISTS rate_usage (
    provider TEXT NOT NULL,
    day      TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, day)
);
"""


def make_key(provider: str, operation: str, payload: Any) -> str:
    """Stable cache key for a provider call and its arguments."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{provider}:{operation}:{blob}".encode()).hexdigest()
    return digest[:32]


class Cache:
    """Thread-safe key/value store with per-entry expiry.

    A single connection guarded by a lock is enough: every statement here is
    a point lookup or a small write, so contention never outweighs the
    simplicity of not managing a pool.
    """

    def __init__(self, path: Path, default_ttl: int = 3600) -> None:
        self.path = path
        self.default_ttl = default_ttl
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def get(self, key: str) -> Any | None:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, expires_at FROM response_cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] < now:
                self._conn.execute("DELETE FROM response_cache WHERE key = ?", (key,))
                self._conn.commit()
                return None
            return json.loads(row["payload"])

    def set(self, key: str, provider: str, value: Any, ttl: int | None = None) -> None:
        ttl = self.default_ttl if ttl is None else ttl
        if ttl <= 0:
            return
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO response_cache "
                "(key, provider, payload, stored_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (key, provider, json.dumps(value, default=str), now, now + ttl),
            )
            self._conn.commit()

    def purge_expired(self) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM response_cache WHERE expires_at < ?", (time.time(),)
            )
            self._conn.commit()
            return cursor.rowcount

    def clear(self, provider: str | None = None) -> int:
        with self._lock:
            if provider is None:
                cursor = self._conn.execute("DELETE FROM response_cache")
            else:
                cursor = self._conn.execute(
                    "DELETE FROM response_cache WHERE provider = ?", (provider,)
                )
            self._conn.commit()
            return cursor.rowcount

    def get_daily_usage(self, provider: str, day: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM rate_usage WHERE provider = ? AND day = ?", (provider, day)
            ).fetchone()
            return int(row["count"]) if row else 0

    def increment_daily_usage(self, provider: str, day: str) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rate_usage (provider, day, count) VALUES (?, ?, 1) "
                "ON CONFLICT (provider, day) DO UPDATE SET count = count + 1",
                (provider, day),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT count FROM rate_usage WHERE provider = ? AND day = ?", (provider, day)
            ).fetchone()
            return int(row["count"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
