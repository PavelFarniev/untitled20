"""Единая настройка логирования (стандартный модуль logging).

Пишем одновременно в файл (с ротацией) и в консоль. Формат единый по всему
проекту. Также устанавливаем глобальные перехватчики необработанных исключений
основного потока и фоновых потоков, чтобы ни одно исключение не потерялось.
"""
from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-16s | %(message)s"


def setup_logging(
    logs_dir: str = "logs",
    level: str = "INFO",
    max_mb: float = 5.0,
    backups: int = 3,
) -> logging.Logger:
    """Сконфигурировать корневой логгер и вернуть логгер приложения.

    Логи ротируются по размеру: не более `max_mb` МБ на файл и `backups`
    архивных копий. Итоговый максимум на диске ≈ max_mb * (backups + 1) МБ,
    поэтому логи не могут бесконтрольно заполнить диск.
    """
    Path(logs_dir).mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    formatter = logging.Formatter(_LOG_FORMAT)

    file_handler = RotatingFileHandler(
        Path(logs_dir) / "gate_system.log",
        maxBytes=int(max_mb * 1024 * 1024),
        backupCount=max(0, backups),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Отдельный ПОНЯТНЫЙ журнал событий (events.log) — для команды /logs в Telegram.
    # Формат простой: «дата время | сообщение», без уровней и имён модулей.
    events_logger = logging.getLogger("gate_system.events")
    for h in list(events_logger.handlers):
        events_logger.removeHandler(h)
    events_handler = RotatingFileHandler(
        Path(logs_dir) / "events.log",
        maxBytes=int(max_mb * 1024 * 1024),
        backupCount=max(0, backups),
        encoding="utf-8",
    )
    events_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s",
                                                  datefmt="%Y-%m-%d %H:%M:%S"))
    events_logger.addHandler(events_handler)
    events_logger.setLevel(logging.INFO)
    events_logger.propagate = True  # события видны и в техническом логе/консоли

    # Приглушаем «шумные» сторонние библиотеки, чтобы не засорять лог и консоль.
    for noisy in ("httpx", "httpcore", "telegram", "urllib3", "PIL",
                  "matplotlib", "ultralytics", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _install_exception_hooks(root)
    return logging.getLogger("gate_system")


def _install_exception_hooks(logger: logging.Logger) -> None:
    """Логировать необработанные исключения в основном и фоновых потоках."""

    def handle(exc_type, exc_value, exc_tb):  # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("Необработанное исключение", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = handle

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        logger.critical(
            "Необработанное исключение в потоке %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = thread_hook
