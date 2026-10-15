"""Высокоуровневое управление воротами (порт GateController).

Не знает, реальное это GPIO-реле или тестовый режим — работает через под-порт
Relay. Отвечает за политику открытия:
* длительность импульса;
* защита от двойного импульса: вход «пошаговый» (SbS) блока Nice Robus работает
  по циклу «открыть → стоп → закрыть», и второй импульс во время открытия
  ОСТАНОВИЛ бы ворота. Поэтому повторная команда в течение min_interval_s
  игнорируется;
* понятное сообщение «Ворота открыты» в консоль и журнал при любом режиме реле.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from gate.core.interfaces import GateController, Relay
from gate.core.models import OpenSource

log = logging.getLogger("gate_system.gate")

_SOURCE_RU = {
    OpenSource.AUTO: "автоматически по номеру",
    OpenSource.TELEGRAM: "через Telegram",
    OpenSource.MANUAL: "вручную",
}


class RelayGateController(GateController):
    """Открывает ворота коротким импульсом реле (имитация нажатия START)."""

    def __init__(
        self,
        relay: Relay,
        pulse_ms: int = 500,
        min_interval_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._relay = relay
        self._pulse_ms = pulse_ms
        self._min_interval_s = max(0.0, min_interval_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._last_pulse = float("-inf")

    @property
    def relay(self) -> Relay:
        return self._relay

    def open(self, source: OpenSource) -> bool:
        """Открыть ворота. Возвращает False, если команда проигнорирована как повтор."""
        with self._lock:
            now = self._clock()
            if now - self._last_pulse < self._min_interval_s:
                log.info("Повторная команда открытия через %.1f с проигнорирована (источник: %s)",
                         now - self._last_pulse, source.value)
                return False
            log.info("Открытие ворот. Источник: %s", source.value)
            try:
                self._relay.pulse(self._pulse_ms)
            except Exception:  # noqa: BLE001 - граница адаптера, логируем всё
                log.exception("Ошибка при управлении реле")
                raise
            self._last_pulse = self._clock()

        real = getattr(self._relay, "is_real", True)
        suffix = "" if real else " (тестовый режим, реле не замыкалось)"
        print(f"Gate opened — Ворота открыты{suffix}", flush=True)
        log.info("Ворота открыты %s%s", _SOURCE_RU.get(source, source.value), suffix)
        return True
