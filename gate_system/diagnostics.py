"""Периодическая самодиагностика (реализация HealthProvider).

Собирает снимок состояния системы: камера, OCR, Telegram, память, температура
CPU (на RPi), свободное место на диске. Отклонения от порогов (мало места,
перегрев) пишутся в журнал. Кроссплатформенно: температура на macOS может быть
недоступна — это нормально (возвращается None).

Метрики (`metrics`) инъектируются: по умолчанию используется psutil, в тестах —
фейковый источник, что позволяет проверять пороги без реального железа.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

from interfaces import HealthProvider
from models import HealthStatus, PlateNumber
from events import event as user_event

log = logging.getLogger("gate_system.diagnostics")


class PsutilMetrics:
    """Источник системных метрик на базе psutil (по умолчанию)."""

    def memory_percent(self) -> float:
        import psutil
        return float(psutil.virtual_memory().percent)

    def disk_free_percent(self, path: str = "/") -> float:
        import psutil
        usage = psutil.disk_usage(path)
        return float(100.0 - usage.percent)

    def cpu_temp_c(self) -> Optional[float]:
        import psutil
        fn = getattr(psutil, "sensors_temperatures", None)
        if fn is None:
            return None
        try:
            temps = fn()
        except Exception:  # noqa: BLE001
            return None
        for entries in temps.values():
            if entries:
                return float(entries[0].current)
        return None


class SelfDiagnostics(HealthProvider):
    """Собирает снимок здоровья системы и логирует отклонения."""

    def __init__(
        self,
        config,
        camera=None,
        ocr_ok_provider: Optional[Callable[[], bool]] = None,
        notifier=None,
        last_plate_provider: Optional[Callable[[], Optional[PlateNumber]]] = None,
        last_event_ts_provider: Optional[Callable[[], Optional[datetime]]] = None,
        metrics: Optional[object] = None,
    ) -> None:
        self._config = config
        self._camera = camera
        self._ocr_ok_provider = ocr_ok_provider
        self._notifier = notifier
        self._last_plate_provider = last_plate_provider
        self._last_event_ts_provider = last_event_ts_provider
        self._metrics = metrics or PsutilMetrics()
        self._stop = None  # threading.Event создаётся в run_forever
        self._reported: set = set()  # какие проблемы уже сообщены (без спама)

    def snapshot(self) -> HealthStatus:
        """Собрать текущее состояние и залогировать проблемы."""
        status = HealthStatus(
            camera_ok=self._safe(lambda: bool(self._camera and self._camera.is_alive()), False),
            ocr_ok=self._safe(lambda: bool(self._ocr_ok_provider and self._ocr_ok_provider()), False),
            telegram_online=self._safe(lambda: bool(self._notifier and self._notifier.is_online()), False),
            memory_percent=self._safe(self._metrics.memory_percent, 0.0),
            cpu_temp_c=self._safe(self._metrics.cpu_temp_c, None),
            disk_free_percent=self._safe(lambda: self._metrics.disk_free_percent("/"), 0.0),
            last_plate=self._safe(lambda: self._last_plate_provider() if self._last_plate_provider else None, None),
            last_event_ts=self._safe(lambda: self._last_event_ts_provider() if self._last_event_ts_provider else None, None),
        )
        self._check_thresholds(status)
        return status

    def _check_thresholds(self, s: HealthStatus) -> None:
        d = self._config.diagnostics
        if not s.camera_ok:
            log.warning("Диагностика: камера не отвечает")
        self._report_problem(
            "disk", s.disk_free_percent < d.min_disk_free_percent,
            f"⚠️ Мало места на диске ({s.disk_free_percent:.0f}%)",
        )
        if s.cpu_temp_c is not None:
            self._report_problem(
                "temp", s.cpu_temp_c > d.max_cpu_temp_c,
                f"🌡️ Перегрев ({s.cpu_temp_c:.0f}°C)",
            )
        if s.disk_free_percent < d.min_disk_free_percent:
            log.warning("Диагностика: мало места на диске (%.1f%% < %.1f%%)",
                        s.disk_free_percent, d.min_disk_free_percent)
        if s.cpu_temp_c is not None and s.cpu_temp_c > d.max_cpu_temp_c:
            log.warning("Диагностика: перегрев CPU (%.1f°C > %.1f°C)",
                        s.cpu_temp_c, d.max_cpu_temp_c)

    def _report_problem(self, key: str, active: bool, message: str) -> None:
        """Сообщить о проблеме в понятный журнал один раз (пока не исчезнет)."""
        if active and key not in self._reported:
            user_event(message)
            self._reported.add(key)
        elif not active and key in self._reported:
            self._reported.discard(key)

    def run_forever(self) -> None:
        """Цикл периодической диагностики до вызова stop()."""
        import threading
        self._stop = threading.Event()
        interval = self._config.diagnostics.interval_s
        log.info("Самодиагностика запущена (интервал %d с)", interval)
        while not self._stop.is_set():
            self.snapshot()
            self._stop.wait(interval)
        log.info("Самодиагностика остановлена")

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    @staticmethod
    def _safe(fn: Callable, default):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            log.debug("Ошибка сбора метрики", exc_info=True)
            return default
