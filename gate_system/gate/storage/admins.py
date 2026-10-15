"""Хранилище дополнительных админов Telegram (роль ниже владельца).

Владелец задаётся вручную в config.json (telegram.owner_id) и может ВСЁ.
Админы — Telegram user_id; их добавляет, исправляет и удаляет владелец через
бота. Админы могут открывать ворота и смотреть статус/фото/историю/список, но не
редактируют настройки, белый список, журнал и список админов.

Хранятся в SQLite (data/gate.db).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Tuple

from gate.storage.database import SqliteDatabase

log = logging.getLogger("gate_system.admins")


class SqliteAdminRepository:
    """Список админов в SQLite (потокобезопасность обеспечивает SqliteDatabase)."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    def list_ids(self) -> List[int]:
        return [i for i, _ in self.list_entries()]

    def list_entries(self) -> List[Tuple[int, str]]:
        rows = self._db.query("SELECT user_id, note FROM admins ORDER BY added_at, user_id")
        return [(int(r["user_id"]), r["note"] or "") for r in rows]

    def contains(self, user_id: int) -> bool:
        return bool(self._db.query("SELECT 1 FROM admins WHERE user_id = ?", (int(user_id),)))

    def add(self, user_id: int, note: str = "") -> None:
        note = (note or "").strip()
        with self._db.transaction() as c:
            exists = c.execute("SELECT 1 FROM admins WHERE user_id = ?", (int(user_id),)).fetchone()
            if exists:
                if note:
                    c.execute("UPDATE admins SET note = ? WHERE user_id = ?", (note, int(user_id)))
                return
            c.execute("INSERT INTO admins(user_id, note, added_at) VALUES (?, ?, ?)",
                      (int(user_id), note, datetime.now(timezone.utc).isoformat()))
        log.info("Добавлен админ: %s", user_id)

    def remove(self, user_id: int) -> None:
        self._db.execute("DELETE FROM admins WHERE user_id = ?", (int(user_id),))
        log.info("Удалён админ: %s", user_id)

    def rename(self, old_id: int, new_id: int) -> bool:
        """Исправить ID админа (сохранив подпись). False — такого админа нет."""
        with self._db.transaction() as c:
            row = c.execute("SELECT note, added_at FROM admins WHERE user_id = ?",
                            (int(old_id),)).fetchone()
            if row is None:
                return False
            if int(old_id) == int(new_id):
                return True
            c.execute("DELETE FROM admins WHERE user_id = ?", (int(old_id),))
            c.execute("INSERT OR REPLACE INTO admins(user_id, note, added_at) VALUES (?, ?, ?)",
                      (int(new_id), row["note"], row["added_at"]))
        log.info("ID админа исправлен: %s -> %s", old_id, new_id)
        return True
