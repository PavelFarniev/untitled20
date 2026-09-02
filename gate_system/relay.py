"""Реализации порта Relay.

На macOS используется StubRelay (программная заглушка по требованию ТЗ).
На Raspberry Pi он заменяется на GpioRelay или UsbRelay без изменения ядра.
Библиотеки, специфичные для железа, импортируются ЛЕНИВО внутри __init__
конкретного адаптера, поэтому на macOS они не требуются вовсе.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from interfaces import Relay

log = logging.getLogger("gate_system.relay")


class StubRelay(Relay):
    """Программная заглушка реле для разработки на macOS.

    Печатает 'Gate opened' согласно ТЗ и логирует импульс. Никакого железа.
    Функция сна инъектируется (`sleeper`) для детерминированного тестирования
    длительности импульса без реальных задержек.
    """

    def __init__(self, sleeper: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleeper

    def pulse(self, milliseconds: int) -> None:
        print("Gate opened")
        log.info("StubRelay: импульс %d мс (замыкание контактов)", milliseconds)
        self._sleep(milliseconds / 1000.0)
        log.debug("StubRelay: контакты разомкнуты")


class GpioRelay(Relay):
    """Адаптер реле через GPIO Raspberry Pi. Реализуется на этапе 12."""

    def __init__(self, pin: int, active_high: bool = True) -> None:
        self._pin = pin
        self._active_high = active_high
        # Ленивая инициализация железа выполняется здесь на RPi:
        #   from gpiozero import OutputDevice
        #   self._device = OutputDevice(pin, active_high=active_high)
        raise NotImplementedError("GpioRelay реализуется на этапе переноса (RPi)")

    def pulse(self, milliseconds: int) -> None:  # pragma: no cover - RPi only
        raise NotImplementedError


class UsbRelay(Relay):
    """Адаптер USB-реле. Альтернатива GPIO. Реализуется на этапе 12."""

    def __init__(self, device: str) -> None:
        self._device = device
        raise NotImplementedError("UsbRelay реализуется на этапе переноса (RPi)")

    def pulse(self, milliseconds: int) -> None:  # pragma: no cover - RPi only
        raise NotImplementedError


def build_relay(backend: str, *, gpio_pin: int, usb_device: str, active_high: bool) -> Relay:
    """Фабрика реле по имени бэкенда из конфига (используется в main.py)."""
    backend = backend.lower()
    if backend == "stub":
        return StubRelay()
    if backend == "gpio":
        return GpioRelay(pin=gpio_pin, active_high=active_high)
    if backend == "usb":
        return UsbRelay(device=usb_device)
    raise ValueError(f"Неизвестный backend реле: {backend!r}")
