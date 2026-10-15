"""Низкоуровневый доступ к хранилищу.

* SqliteDatabase — основная база (data/gate.db): белый список, админы, история.
  Режим WAL + synchronous=NORMAL: база не портится при внезапном отключении
  питания и бережёт SD-карту (не переписывается целиком на каждое событие).
* read_json / write_json_atomic — для config.json и состояния прокси.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, List, Sequence

log = logging.getLogger("gate_system.database")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS whitelist (
    plate     TEXT PRIMARY KEY,          -- канонический номер (кириллица, без пробелов)
    note      TEXT NOT NULL DEFAULT '',  -- подпись: «Папа», «Курьер» и т.п.
    added_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admins (
    user_id   INTEGER PRIMARY KEY,       -- Telegram user_id
    note      TEXT NOT NULL DEFAULT '',
    added_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,           -- ISO-время UTC
    plate       TEXT,
    result      TEXT NOT NULL,           -- granted | denied | unreadable
    photo_path  TEXT,
    gate_opened INTEGER NOT NULL DEFAULT 0,
    source      TEXT,                    -- auto | telegram | manual
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class SqliteDatabase:
    """Потокобезопасное соединение с SQLite (одно на процесс, под замком)."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # isolation_level=None — автокоммит; транзакции открываем явно (BEGIN).
        self._conn = sqlite3.connect(self._path, check_same_thread=False,
                                     timeout=10.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self._path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        log.info("База данных открыта: %s", self._path)

    @property
    def path(self) -> str:
        return self._path

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Атомарная группа операций: всё или ничего."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- служебные флаги (напр. «старый JSON уже импортирован») ---
    def get_meta(self, key: str) -> str | None:
        rows = self.query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass


# ------------------------------- JSON -------------------------------
def read_json(path: str | Path, default: Any) -> Any:
    """Прочитать JSON-файл, вернуть default при отсутствии/повреждении."""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return default


def write_json_atomic(path: str | Path, data: Any) -> None:
    """Атомарно записать JSON (через временный файл + os.replace).

    Защищает от повреждения файла при внезапном отключении питания посреди
    записи — критично для системы, работающей 24/7.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
