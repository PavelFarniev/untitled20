"""Доменные сущности системы управления воротами.

Слой ДОМЕНА чистой архитектуры. Модуль намеренно не импортирует ничего
внешнего (OpenCV, FastAPI, Telegram и т.п.) — только стандартную библиотеку.
Это гарантирует, что бизнес-данные не зависят от инфраструктуры.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class OpenSource(str, Enum):
    """Источник команды на открытие ворот."""

    AUTO = "auto"          # автоматически, по распознанному номеру
    TELEGRAM = "telegram"  # команда из Telegram
    MANUAL = "manual"      # вручную (веб-интерфейс / кнопка)


class CheckResult(str, Enum):
    """Результат проверки номера по белому списку."""

    GRANTED = "granted"    # номер найден, доступ разрешён
    DENIED = "denied"      # номер отсутствует в белом списке
    UNREADABLE = "unreadable"  # номер не удалось распознать


@dataclass(frozen=True)
class BBox:
    """Ограничивающая рамка объекта на кадре (в пикселях)."""

    x1: int
    y1: int
    x2: int
    y2: int
    confidence: float = 0.0

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1


# Латинские буквы, визуально совпадающие с кириллическими в номерах РФ.
# Приводим их к кириллице, чтобы «T198TT197» (латиница) и «Т198ТТ197» (кириллица)
# считались одним номером — иначе распознанный и записанный вручную не совпадут.
_PLATE_LAT_TO_CYR = str.maketrans({
    "A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н",
    "O": "О", "P": "Р", "C": "С", "T": "Т", "Y": "У", "X": "Х",
})


@dataclass(frozen=True)
class PlateNumber:
    """Нормализованный автомобильный номер.

    `value` хранится в канонической форме: верхний регистр, без пробелов, а
    латинские буквы-двойники приведены к кириллице. Благодаря этому номера
    из белого списка и распознанные с камеры сравниваются корректно независимо
    от того, какими буквами (латиница/кириллица) их набрали.
    """

    value: str

    def __post_init__(self) -> None:
        canonical = self.value.strip().upper().replace(" ", "").translate(_PLATE_LAT_TO_CYR)
        object.__setattr__(self, "value", canonical)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class OcrResult:
    """Результат распознавания текста номера."""

    plate: Optional[PlateNumber]
    confidence: float
    raw_text: str = ""

    @property
    def is_readable(self) -> bool:
        return self.plate is not None


@dataclass
class Vehicle:
    """Обнаруженный автомобиль на кадре."""

    vehicle_box: BBox
    plate_box: Optional[BBox] = None
    ocr: Optional[OcrResult] = None


@dataclass
class Event:
    """Событие журнала истории.

    Хранит всё, что требует ТЗ: время, номер, путь к фото, результат проверки,
    была ли команда открытия и её источник.
    """

    timestamp: datetime
    plate: Optional[PlateNumber]
    result: CheckResult
    photo_path: Optional[str] = None
    gate_opened: bool = False
    source: Optional[OpenSource] = None
    note: str = ""

    @staticmethod
    def now(
        plate: Optional[PlateNumber],
        result: CheckResult,
        *,
        photo_path: Optional[str] = None,
        gate_opened: bool = False,
        source: Optional[OpenSource] = None,
        note: str = "",
    ) -> "Event":
        """Фабрика события с текущей меткой времени (UTC)."""
        return Event(
            timestamp=datetime.now(timezone.utc),
            plate=plate,
            result=result,
            photo_path=photo_path,
            gate_opened=gate_opened,
            source=source,
            note=note,
        )


@dataclass
class AccessResult:
    """Итог обработки одного распознанного номера ядром AccessDecision."""

    result: CheckResult
    gate_opened: bool
    event: Event
    suppressed_by_antireplay: bool = False


@dataclass
class HealthStatus:
    """Снимок состояния системы для /status, веб-интерфейса и диагностики."""

    camera_ok: bool = False
    ocr_ok: bool = False
    telegram_online: bool = False
    memory_percent: float = 0.0
    cpu_temp_c: Optional[float] = None
    disk_free_percent: float = 0.0
    last_plate: Optional[PlateNumber] = None
    last_event_ts: Optional[datetime] = None
    extra: dict = field(default_factory=dict)
