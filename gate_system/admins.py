"""Хранилище дополнительных админов Telegram (роль ниже владельца).

Владелец задаётся вручную в config.json (telegram.owner_id) и может ВСЁ.
Админы — это список Telegram user_id в отдельном JSON-файле; их добавляет и
удаляет владелец командами бота. Админы могут открывать ворота и смотреть
статус/фото/историю, но не редактируют настройки, белый список, логи и список
админов.

Отдельный файл (а не config.json) выбран сознательно: список админов меняется
из бота в рантайме и логически отделён от статической конфигурации.
"""
from __future__ import annotations

import logging
import threading
from typing import List

import database

log = logging.getLogger("gate_system.admins")


class AdminStore:
    """Потокобезопасный список Telegram-ID админов в JSON-файле."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()

    def _load(self) -> List[int]:
        data = database.read_json(self._path, {"admins": []})
        return [int(x) for x in data.get("admins", [])]

    def _save(self, ids: List[int]) -> None:
        database.write_json_atomic(self._path, {"admins": ids})

    def list_ids(self) -> List[int]:
        with self._lock:
            return self._load()

    def contains(self, user_id: int) -> bool:
        with self._lock:
            return int(user_id) in self._load()

    def add(self, user_id: int) -> None:
        with self._lock:
            ids = self._load()
            if int(user_id) not in ids:
                ids.append(int(user_id))
                self._save(ids)
                log.info("Добавлен админ: %s", user_id)

    def remove(self, user_id: int) -> None:
        with self._lock:
            ids = [i for i in self._load() if i != int(user_id)]
            self._save(ids)
            log.info("Удалён админ: %s", user_id)
