"""Журнал истории событий (порт HistoryRepository).

Хранит для каждого события: время, номер, путь к фото, результат проверки,
факт открытия ворот и источник. Сейчас — JSON; позже прозрачно заменяется на
SQLite без изменения ядра.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

import database
from interfaces import HistoryRepository
from models import CheckResult, Event, OpenSource, PlateNumber

log = logging.getLogger("gate_system.history")


def _default_writer(path: str, frame) -> bool:
    """Сохранение кадра на диск через OpenCV (ленивый импорт)."""
    import cv2

    return bool(cv2.imwrite(path, frame))


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
        try:
            files = sorted(self._dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
            for old in files[: len(files) - self._max_files]:
                old.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            log.debug("Не удалось почистить старые фото", exc_info=True)


class HistoryStore(HistoryRepository):
    """Хранилище событий в JSON-файле (список записей)."""

    def __init__(self, path: str, max_records: int = 5000) -> None:
        self._path = path
        self._max = max_records
        self._lock = threading.RLock()

    def append(self, event: Event) -> None:
        with self._lock:
            records = database.read_json(self._path, [])
            # Файл валидного JSON, но не список (ручная правка/старый формат) —
            # не падаем: начинаем новый журнал, чтобы открытие ворот не срывалось.
            if not isinstance(records, list):
                log.warning("history.json имел неверный формат — начинаю журнал заново")
                records = []
            records.append(self._serialize(event))
            if len(records) > self._max:
                records = records[-self._max:]
            database.write_json_atomic(self._path, records)
            log.debug("Событие записано в журнал: %s", event.result.value)

    def recent(self, limit: int = 20) -> List[Event]:
        with self._lock:
            records = database.read_json(self._path, [])
            if not isinstance(records, list):
                return []
            out: List[Event] = []
            for r in records[-limit:][::-1]:
                try:
                    out.append(self._deserialize(r))
                except Exception:  # noqa: BLE001 - битую запись пропускаем, не роняем /history
                    log.warning("Пропущена повреждённая запись истории", exc_info=True)
            return out

    @staticmethod
    def _serialize(e: Event) -> dict:
        return {
            "timestamp": e.timestamp.isoformat(),
            "plate": e.plate.value if e.plate else None,
            "result": e.result.value,
            "photo_path": e.photo_path,
            "gate_opened": e.gate_opened,
            "source": e.source.value if e.source else None,
            "note": e.note,
        }

    @staticmethod
    def _deserialize(r: dict) -> Event:
        return Event(
            timestamp=datetime.fromisoformat(r["timestamp"]),
            plate=PlateNumber(r["plate"]) if r.get("plate") else None,
            result=CheckResult(r["result"]),
            photo_path=r.get("photo_path"),
            gate_opened=r.get("gate_opened", False),
            source=OpenSource(r["source"]) if r.get("source") else None,
            note=r.get("note", ""),
        )
