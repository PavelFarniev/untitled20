"""Монитор доступности интернета (кроссплатформенно).

Периодически проверяет связь TCP-подключением к надёжному хосту (по умолчанию
DNS-сервер 8.8.8.8:53). При СМЕНЕ состояния онлайн/оффлайн вызывает колбэки —
их использует TelegramNotifier, чтобы переподключаться после возврата интернета
без перезапуска процесса.

Функция проверки (`probe`) инъектируется — это позволяет детерминированно
тестировать логику переходов без реальной сети.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable, Optional

from events import event as user_event

log = logging.getLogger("gate_system.network")


def default_probe(host: str = "8.8.8.8", port: int = 53, timeout: float = 3.0) -> bool:
    """Проверить наличие сети TCP-коннектом к DNS-серверу."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class NetworkMonitor:
    """Следит за наличием интернета и уведомляет о смене статуса."""

    def __init__(
        self,
        check_interval_s: float = 10.0,
        on_online: Optional[Callable[[], None]] = None,
        on_offline: Optional[Callable[[], None]] = None,
        probe: Callable[[], bool] = default_probe,
    ) -> None:
        self._interval = check_interval_s
        self._on_online = on_online
        self._on_offline = on_offline
        self._probe = probe
        self._online = False
        self._stop = threading.Event()

    @property
    def is_online(self) -> bool:
        return self._online

    def check_once(self) -> bool:
        """Разовая проверка. При смене состояния вызывает соответствующий колбэк."""
        online = self._probe()
        if online != self._online:
            self._online = online
            if online:
                log.info("Интернет доступен")
                user_event("🌐 Интернет появился")
                self._safe_call(self._on_online)
            else:
                log.warning("Интернет потерян")
                user_event("🌐 Интернет пропал")
                self._safe_call(self._on_offline)
        return online

    def run_forever(self) -> None:
        """Цикл периодической проверки до вызова stop()."""
        log.info("NetworkMonitor запущен (интервал %.0f с)", self._interval)
        while not self._stop.is_set():
            self.check_once()
            self._stop.wait(self._interval)
        log.info("NetworkMonitor остановлен")

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _safe_call(fn: Optional[Callable[[], None]]) -> None:
        if fn is None:
            return
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в колбэке смены состояния сети")
