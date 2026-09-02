"""Высокоуровневое управление воротами (порт GateController).

Не знает, GPIO это, USB или заглушка — работает через под-порт Relay.
Отвечает за политику открытия: длительность импульса и логирование источника.
"""
from __future__ import annotations

import logging

from interfaces import GateController, Relay
from models import OpenSource

log = logging.getLogger("gate_system.gate")


class RelayGateController(GateController):
    """Открывает ворота коротким импульсом реле (имитация нажатия START)."""

    def __init__(self, relay: Relay, pulse_ms: int = 500) -> None:
        self._relay = relay
        self._pulse_ms = pulse_ms

    def open(self, source: OpenSource) -> None:
        log.info("Открытие ворот. Источник: %s", source.value)
        try:
            self._relay.pulse(self._pulse_ms)
            log.info("Ворота открыты (источник=%s)", source.value)
        except Exception:  # noqa: BLE001 - граница адаптера, логируем всё
            log.exception("Ошибка при управлении реле")
            raise
