"""Реализации порта Relay.

* GpioRelay   — реальное реле на GPIO Raspberry Pi 5 (gpiozero + lgpio).
* DryRunRelay — тестовый режим (relay.enabled=false): контакты не замыкаются,
  только запись в журнал. Нужен, чтобы гонять систему на Маке и в тестах.

Выбор делает build_relay() по флагу relay.enabled из config.json.

На Raspberry Pi 5 GPIO обслуживается новым чипом RP1, поэтому старая библиотека
RPi.GPIO там НЕ работает. Используем gpiozero с бэкендом lgpio (на Pi 5 он
выбирается автоматически; в systemd-сервисе дополнительно задаётся
GPIOZERO_PIN_FACTORY=lgpio).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from gate.core.interfaces import Relay

log = logging.getLogger("gate_system.relay")


class RelayError(RuntimeError):
    """Реле не удалось инициализировать или им управлять."""


class DryRunRelay(Relay):
    """Тестовый режим реле: ничего не замыкает, только пишет в журнал.

    :param reason: почему работаем без реле (для понятного журнала и статуса).
    """

    def __init__(self, sleeper: Callable[[float], None] = time.sleep,
                 reason: str = "relay.enabled=false") -> None:
        self._sleep = sleeper
        self.reason = reason
        self._lock = threading.Lock()

    @property
    def description(self) -> str:
        return f"тестовый режим ({self.reason})"

    @property
    def is_real(self) -> bool:
        return False

    def pulse(self, milliseconds: int) -> None:
        with self._lock:
            log.info("Тестовый режим: реле НЕ замыкается (%s), импульс %d мс",
                     self.reason, milliseconds)
            self._sleep(milliseconds / 1000.0)

    def close(self) -> None:
        pass


def _gpiozero_device(pin: int, active_high: bool):
    """Создать выход GPIO через gpiozero (ленивый импорт: только на Pi)."""
    from gpiozero import OutputDevice

    # initial_value=False — при старте программы реле гарантированно разомкнуто.
    return OutputDevice(pin, active_high=active_high, initial_value=False)


class GpioRelay(Relay):
    """Реле на GPIO Raspberry Pi: короткое замыкание контактов = нажатие START.

    :param pin: номер GPIO в нумерации BCM (GPIO17 = ножка 11 разъёма).
    :param active_high: True — модуль срабатывает от высокого уровня (High-trigger),
        False — от низкого (Low-trigger).
    :param device_factory: (pin, active_high) -> объект с on()/off()/close().
        Инъекция для тестов; по умолчанию gpiozero.OutputDevice.
    """

    def __init__(
        self,
        pin: int,
        active_high: bool = True,
        device_factory: Optional[Callable[[int, bool], object]] = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._pin = pin
        self._active_high = active_high
        self._sleep = sleeper
        self._lock = threading.Lock()
        factory = device_factory or _gpiozero_device
        try:
            self._device = factory(pin, active_high)
        except Exception as exc:  # noqa: BLE001 - нет gpiozero/lgpio, занят пин, не Pi
            raise RelayError(
                f"Не удалось открыть GPIO{pin}: {exc}. Проверьте, что это Raspberry Pi, "
                "установлены gpiozero и lgpio, а пользователь входит в группу gpio."
            ) from exc
        log.info("Реле GPIO готово: GPIO%d (%s-trigger)", pin, "High" if active_high else "Low")

    @property
    def description(self) -> str:
        return f"GPIO{self._pin} ({'High' if self._active_high else 'Low'}-trigger)"

    @property
    def is_real(self) -> bool:
        return True

    def pulse(self, milliseconds: int) -> None:
        """Замкнуть контакты на `milliseconds` мс и обязательно разомкнуть обратно."""
        with self._lock:
            try:
                self._device.on()
                log.info("Реле GPIO%d: контакты замкнуты на %d мс", self._pin, milliseconds)
                self._sleep(milliseconds / 1000.0)
            finally:
                # Что бы ни случилось — реле не должно остаться замкнутым.
                self._device.off()
                log.debug("Реле GPIO%d: контакты разомкнуты", self._pin)

    def close(self) -> None:
        try:
            self._device.off()
            self._device.close()
        except Exception:  # noqa: BLE001
            log.debug("Ошибка освобождения GPIO", exc_info=True)


def build_relay(
    enabled: bool,
    *,
    gpio_pin: int,
    active_high: bool,
    device_factory: Optional[Callable[[int, bool], object]] = None,
) -> Relay:
    """Собрать реле по конфигу.

    enabled=False → тестовый режим. enabled=True → реальное GPIO-реле. Если реле
    включено, но не открывается (не Pi, нет библиотек, занят пин), система НЕ
    падает: продолжает работать в тестовом режиме, чтобы Telegram и уведомления
    оставались доступны, а проблема громко пишется в журнал и видна в «Статусе».
    """
    if not enabled:
        log.warning("Реле в ТЕСТОВОМ режиме (relay.enabled=false): ворота физически не открываются")
        return DryRunRelay()
    try:
        return GpioRelay(pin=gpio_pin, active_high=active_high, device_factory=device_factory)
    except RelayError as exc:
        log.critical("%s Работаю БЕЗ реле — ворота открываться не будут!", exc)
        from gate.common.events import event as user_event
        user_event("❗ Реле не подключилось — ворота автоматически НЕ откроются. Проверьте подключение.")
        return DryRunRelay(reason="ошибка GPIO — реле не подключено")
