"""Watchdog: контроль «сердцебиения» рабочих потоков.

Каждая подсистема (Vision-цикл, Telegram, диагностика) периодически вызывает
`beat(name)`. Watchdog проверяет, что метки свежие; если поток завис дольше
таймаута, он логирует критическую ошибку и вызывает колбэк `on_stale(name)`.

В продакшене `on_stale` обычно завершает процесс (`sys.exit`/`os._exit`), чтобы
systemd поднял сервис заново — это самый надёжный способ восстановления после
зависания. Колбэк и часы инъектируются, что делает модуль детерминированно
тестируемым без реальных задержек.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

log = logging.getLogger("gate_system.watchdog")


class Watchdog:
    """Отслеживает heartbeat зарегистрированных подсистем."""

    def __init__(
        self,
        timeout_s: float = 30.0,
        on_stale: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout = timeout_s
        self._on_stale = on_stale
        self._clock = clock
        self._beats: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def register(self, name: str) -> None:
        """Зарегистрировать подсистему (первичная отметка heartbeat)."""
        self.beat(name)

    def beat(self, name: str) -> None:
        """Отметить, что подсистема `name` жива."""
        with self._lock:
            self._beats[name] = self._clock()

    def check(self) -> Dict[str, bool]:
        """Вернуть {имя: жива?} по свежести heartbeat."""
        now = self._clock()
        with self._lock:
            return {n: (now - t) < self._timeout for n, t in self._beats.items()}

    def stale_subsystems(self) -> List[str]:
        """Список подсистем, чей heartbeat устарел."""
        return [name for name, alive in self.check().items() if not alive]

    def run_once(self) -> List[str]:
        """Одна проверка: для каждой зависшей подсистемы вызвать on_stale."""
        stale = self.stale_subsystems()
        for name in stale:
            log.critical("Подсистема '%s' не отвечает дольше %.0f с", name, self._timeout)
            if self._on_stale is not None:
                try:
                    self._on_stale(name)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка в обработчике зависания подсистемы")
        return stale

    def run_forever(self, interval_s: float = 5.0) -> None:
        """Цикл периодической проверки до вызова stop()."""
        log.info("Watchdog запущен (таймаут %.0f с)", self._timeout)
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(interval_s)
        log.info("Watchdog остановлен")

    def stop(self) -> None:
        self._stop.set()
