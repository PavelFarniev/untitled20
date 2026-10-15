"""Белый список номеров в SQLite (реализация порта WhitelistRepository)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Tuple

from gate.storage.database import SqliteDatabase
from gate.core.interfaces import WhitelistRepository
from gate.core.models import PlateNumber

log = logging.getLogger("gate_system.whitelist")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteWhitelistRepository(WhitelistRepository):
    """Белый список в SQLite. Номер хранится в каноническом виде (PlateNumber)."""

    def __init__(self, db: SqliteDatabase) -> None:
        self._db = db

    def is_allowed(self, plate: PlateNumber) -> bool:
        rows = self._db.query("SELECT 1 FROM whitelist WHERE plate = ?", (plate.value,))
        return bool(rows)

    def list_all(self) -> List[PlateNumber]:
        return [p for p, _ in self.list_entries()]

    def list_entries(self) -> List[Tuple[PlateNumber, str]]:
        rows = self._db.query("SELECT plate, note FROM whitelist ORDER BY added_at, plate")
        return [(PlateNumber(r["plate"]), r["note"] or "") for r in rows]

    def add(self, plate: PlateNumber, note: str = "") -> None:
        """Добавить номер. Если он уже есть — обновить подпись (если задана)."""
        note = (note or "").strip()
        with self._db.transaction() as c:
            exists = c.execute("SELECT 1 FROM whitelist WHERE plate = ?", (plate.value,)).fetchone()
            if exists:
                if note:
                    c.execute("UPDATE whitelist SET note = ? WHERE plate = ?", (note, plate.value))
                return
            c.execute("INSERT INTO whitelist(plate, note, added_at) VALUES (?, ?, ?)",
                      (plate.value, note, _now_iso()))
        log.info("Добавлен номер в белый список: %s", plate)

    def remove(self, plate: PlateNumber) -> None:
        self._db.execute("DELETE FROM whitelist WHERE plate = ?", (plate.value,))
        log.info("Удалён номер из белого списка: %s", plate)

    def rename(self, old: PlateNumber, new: PlateNumber) -> bool:
        """Исправить номер, сохранив подпись и дату добавления."""
        with self._db.transaction() as c:
            row = c.execute("SELECT note, added_at FROM whitelist WHERE plate = ?",
                            (old.value,)).fetchone()
            if row is None:
                return False
            if old.value == new.value:
                return True
            c.execute("DELETE FROM whitelist WHERE plate = ?", (old.value,))
            c.execute("INSERT OR REPLACE INTO whitelist(plate, note, added_at) VALUES (?, ?, ?)",
                      (new.value, row["note"], row["added_at"]))
        log.info("Номер в белом списке исправлен: %s -> %s", old, new)
        return True
