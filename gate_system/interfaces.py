"""Порты (абстрактные контракты) чистой архитектуры.

Ядро приложения (use cases в pipeline.py) зависит ТОЛЬКО от этих абстракций,
а не от конкретных реализаций. Благодаря этому:

* модуль ворот на Raspberry Pi заменяется без изменения бизнес-логики
  (StubRelay -> GpioRelay / UsbRelay);
* хранилище белого списка заменяется с JSON на SQLite без правок ядра
  (JsonWhitelistRepository -> SqliteWhitelistRepository);
* отказ Telegram (порт Notifier) не влияет на распознавание и открытие ворот.

Конкретные адаптеры выбираются и связываются в main.py (Composition Root).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from models import (
    BBox,
    Event,
    HealthStatus,
    OcrResult,
    OpenSource,
    PlateNumber,
)

# Тип кадра оставлен как Any-совместимый на уровне контракта: домен не должен
# знать, что это numpy.ndarray из OpenCV. Адаптеры уточняют тип у себя.
Frame = "any"  # noqa: PYI  (документирующий псевдоним; реальный тип — np.ndarray)


class FrameSource(ABC):
    """Источник видеокадров (напр. RTSP IP-камера)."""

    @abstractmethod
    def open(self) -> None:
        """Открыть/инициализировать поток."""

    @abstractmethod
    def read(self):  # -> Optional[Frame]
        """Прочитать очередной кадр. Возвращает None при обрыве потока."""

    @abstractmethod
    def is_alive(self) -> bool:
        """Проверить, что поток активен."""

    @abstractmethod
    def reconnect(self) -> None:
        """Переподключиться (с внутренней стратегией задержки)."""

    @abstractmethod
    def close(self) -> None:
        """Освободить ресурсы."""


class Detector(ABC):
    """Детектор объектов: автомобиль и номерной знак."""

    @abstractmethod
    def detect_vehicle(self, frame) -> List[BBox]:
        """Найти автомобили на кадре."""

    @abstractmethod
    def detect_plate(self, frame) -> List[BBox]:
        """Найти номерные знаки (на кадре или в ROI автомобиля)."""


class OcrEngine(ABC):
    """Распознавание текста номера."""

    @abstractmethod
    def read_text(self, plate_image) -> OcrResult:
        """Распознать номер по изображению номерного знака."""


class Relay(ABC):
    """Низкоуровневое реле. Под-порт для GateController.

    На macOS -> StubRelay (заглушка), на RPi -> GpioRelay / UsbRelay.
    """

    @abstractmethod
    def pulse(self, milliseconds: int) -> None:
        """Кратковременно замкнуть контакты на указанное число миллисекунд."""


class GateController(ABC):
    """Высокоуровневое управление воротами (имитация нажатия START)."""

    @abstractmethod
    def open(self, source: OpenSource) -> None:
        """Открыть ворота (импульс реле) и учесть источник команды."""


class WhitelistRepository(ABC):
    """Хранилище белого списка номеров.

    Реализации: JsonWhitelistRepository (сейчас) -> SqliteWhitelistRepository.
    """

    @abstractmethod
    def is_allowed(self, plate: PlateNumber) -> bool: ...

    @abstractmethod
    def list_all(self) -> List[PlateNumber]: ...

    @abstractmethod
    def add(self, plate: PlateNumber) -> None: ...

    @abstractmethod
    def remove(self, plate: PlateNumber) -> None: ...


class HistoryRepository(ABC):
    """Журнал событий проезда."""

    @abstractmethod
    def append(self, event: Event) -> None: ...

    @abstractmethod
    def recent(self, limit: int = 20) -> List[Event]: ...


class Notifier(ABC):
    """Доставка уведомлений владельцу (Telegram).

    Является чистым адаптером: его недоступность (нет интернета) не должна
    влиять на работу ядра.
    """

    @abstractmethod
    def notify_known(self, event: Event) -> None:
        """Уведомить об открытии по разрешённому номеру."""

    @abstractmethod
    def notify_unknown(self, event: Event) -> None:
        """Уведомить о неизвестном авто (фото, номер, время, кнопки)."""

    @abstractmethod
    def is_online(self) -> bool:
        """Есть ли сейчас связь с Telegram."""


class HealthProvider(ABC):
    """Источник состояния системы для /status и веб-интерфейса."""

    @abstractmethod
    def snapshot(self) -> HealthStatus: ...
