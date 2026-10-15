"""Журнал истории событий (порт HistoryRepository) и хранилище фото.

Для каждого события хранится: время, номер, путь к фото, результат проверки,
факт открытия ворот и источник команды.

* SqliteHistoryRepository — история в SQLite (data/gate.db), старые записи удаляются.
* PhotoStorage — фото событий в photos/ с автоочисткой старых.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from gate.storage.database import SqliteDatabase
from gate.core.interfaces import HistoryRepository
from gate.core.models import CheckResult, Event, OpenSource, PlateNumber

log = logging.getLogger("gate_system.history")


def _default_writer(path: str, frame) -> bool:
    """Сохранение кадра на диск через OpenCV (ленивый импорт)."""
    import cv2

    return bool(cv2.imwrite(path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85]))


class PhotoStorage:
    """Сохраняет фото события в каталог photos/ с информативным именем файла.

    Имя: ``<UTC-время>_<номер|unknown>.jpg``. Writer инъектируется, чтобы
    тестировать логику именования без OpenCV и без записи реальных изображений.

    Каталог автоматически ограничивается по числу файлов (`max_files`): после
    каждого сохранения самые старые фото удаляются, поэтому диск не переполняется.
    """

    def __init__(
        self,
        photos_dir: str,
        max_files: int = 500,
        writer: Callable[[str, object], bool] = _default_writer,
    ) -> None:
        self._dir = Path(photos_dir)
        self._max_files = max_files
        self._writer = writer
        self._lock = threading.Lock()

    def save(self, frame, plate: Optional[PlateNumber] = None) -> Optional[str]:
        """Сохранить кадр. Возвращает путь к файлу или None при неудаче."""
        if frame is None:
            return None
        self._dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        name = f"{ts}_{plate.value if plate else 'unknown'}.jpg"
        path = str(self._dir / name)
        try:
            if self._writer(path, frame):
                self._prune()
                return path
            log.warning("Не удалось сохранить фото: %s", path)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка сохранения фото")
        return None

    def _prune(self) -> None:
        """Оставить в каталоге не более max_files самых свежих фото."""
        if self._max_files <= 0:
            return
        with self._lock:
            try:
                files = sorted(self._dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
                for old in files[: len(files) - self._max_files]:
                    old.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                log.debug("Не удалось почистить старые фото", exc_info=True)


def _serialize_ts(e: Event) -> str:
    ts = e.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


class SqliteHistoryRepository(HistoryRepository):
    """История событий в SQLite. Хранит не больше max_records последних записей."""

    _PRUNE_EVERY = 50  # чистить старые записи раз в N вставок (а не на каждой)

    def __init__(self, db: SqliteDatabase, max_records: int = 5000) -> None:
        self._db = db
        self._max = max(1, max_records)
        self._inserts = 0

    def append(self, event: Event) -> None:
        self._db.execute(
            "INSERT INTO events(ts, plate, result, photo_path, gate_opened, source, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _serialize_ts(event),
                event.plate.value if event.plate else None,
                event.result.value,
                event.photo_path,
                1 if event.gate_opened else 0,
                event.source.value if event.source else None,
                event.note or "",
            ),
        )
        self._inserts += 1
        if self._inserts % self._PRUNE_EVERY == 0:
            self.prune()
        log.debug("Событие записано в журнал: %s", event.result.value)

    def prune(self) -> None:
        """Удалить всё, что старше последних max_records событий."""
        self._db.execute(
            "DELETE FROM events WHERE id <= ("
            "  SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (self._max,),
        )

    def recent(self, limit: int = 20) -> List[Event]:
        rows = self._db.query(
            "SELECT ts, plate, result, photo_path, gate_opened, source, note "
            "FROM events ORDER BY id DESC LIMIT ?",
            (max(0, int(limit)),),
        )
        out: List[Event] = []
        for r in rows:
            try:
                out.append(Event(
                    timestamp=datetime.fromisoformat(r["ts"]),
                    plate=PlateNumber(r["plate"]) if r["plate"] else None,
                    result=CheckResult(r["result"]),
                    photo_path=r["photo_path"],
                    gate_opened=bool(r["gate_opened"]),
                    source=OpenSource(r["source"]) if r["source"] else None,
                    note=r["note"] or "",
                ))
            except Exception:  # noqa: BLE001 - битую запись пропускаем, не роняем «Историю»
                log.warning("Пропущена повреждённая запись истории", exc_info=True)
        return out

    def count(self) -> int:
        return int(self._db.query("SELECT COUNT(*) AS n FROM events")[0]["n"])
