"""Репозитории белого списка (порт WhitelistRepository).

JsonWhitelistRepository используется на первом этапе. SqliteWhitelistRepository
— заготовка на будущее; переключение выполняется одной строкой в main.py,
бизнес-логика не меняется.
"""
from __future__ import annotations

import logging
import threading
from typing import List

import database
from interfaces import WhitelistRepository
from models import PlateNumber

log = logging.getLogger("gate_system.whitelist")


class JsonWhitelistRepository(WhitelistRepository):
    """Белый список в JSON-файле формата {"allowed": ["А123ВС777", ...]}."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()

    def _load(self) -> List[str]:
        data = database.read_json(self._path, {"allowed": []})
        # Терпимость к битому файлу: допускаем и объект {"allowed":[...]}, и
        # голый список [...]. Всё прочее трактуем как пустой список (не падаем).
        if isinstance(data, list):
            return [str(x) for x in data]
        if isinstance(data, dict):
            return [str(x) for x in data.get("allowed", [])]
        log.warning("allowed.json имеет неожиданный формат — считаю список пустым")
        return []

    def _save(self, plates: List[str]) -> None:
        database.write_json_atomic(self._path, {"allowed": plates})

    def is_allowed(self, plate: PlateNumber) -> bool:
        with self._lock:
            # Канонизируем записи файла через PlateNumber: номера, введённые
            # латиницей вручную, корректно сравниваются с распознанными (кириллица).
            allowed = {PlateNumber(v).value for v in self._load()}
            return plate.value in allowed

    def list_all(self) -> List[PlateNumber]:
        with self._lock:
            return [PlateNumber(v) for v in self._load()]

    def add(self, plate: PlateNumber) -> None:
        with self._lock:
            plates = self._load()
            if plate.value not in plates:
                plates.append(plate.value)
                self._save(plates)
                log.info("Добавлен номер в белый список: %s", plate)

    def remove(self, plate: PlateNumber) -> None:
        with self._lock:
            plates = [p for p in self._load() if p != plate.value]
            self._save(plates)
            log.info("Удалён номер из белого списка: %s", plate)


class SqliteWhitelistRepository(WhitelistRepository):
    """Реализация на SQLite. Планируется на следующем этапе (тот же контракт)."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        raise NotImplementedError("SqliteWhitelistRepository — следующий этап")

    def is_allowed(self, plate: PlateNumber) -> bool: ...  # pragma: no cover
    def list_all(self) -> List[PlateNumber]: ...  # pragma: no cover
    def add(self, plate: PlateNumber) -> None: ...  # pragma: no cover
    def remove(self, plate: PlateNumber) -> None: ...  # pragma: no cover
