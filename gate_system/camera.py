"""RTSP IP-камера (реализация порта FrameSource).

Кроссплатформенно через OpenCV (cv2.VideoCapture). Источником может быть как
RTSP-URL реальной камеры, так и путь к видеофайлу (используется в тестах) — API
одинаков. Модуль устойчив к обрывам: при потере потока `reconnect()` делает одну
попытку переподключения с экспоненциально растущей задержкой (1 → 2 → 4 → … →
max), а после успешного подключения задержка сбрасывается к минимальной.

Тяжёлый импорт cv2 выполняется на уровне модуля намеренно: камера — обязательный
компонент, а не опциональный, как GPIO.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

import cv2
import numpy as np

from interfaces import FrameSource
from events import event as user_event

log = logging.getLogger("gate_system.camera")

# Кадр в этом адаптере — это numpy-массив OpenCV (BGR).
Frame = np.ndarray


class RTSPCamera(FrameSource):
    """Захват кадров с IP-камеры по RTSP (или из видеофайла) через OpenCV."""

    def __init__(
        self,
        source: str,
        min_delay_s: float = 1.0,
        max_delay_s: float = 30.0,
        open_timeout_ms: int = 5000,
        read_timeout_ms: int = 5000,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        :param source: RTSP-URL или путь к видеофайлу.
        :param min_delay_s: начальная задержка переподключения.
        :param max_delay_s: максимальная задержка переподключения.
        :param open_timeout_ms: таймаут открытия потока (FFmpeg).
        :param read_timeout_ms: таймаут чтения кадра (FFmpeg).
        :param sleeper: функция сна (инъекция для тестируемости backoff).
        """
        self._source = source
        self._min_delay = min_delay_s
        self._max_delay = max_delay_s
        self._open_timeout_ms = open_timeout_ms
        self._read_timeout_ms = read_timeout_ms
        self._sleep = sleeper

        self._cap: Optional[cv2.VideoCapture] = None
        self._current_delay = min_delay_s  # текущая задержка backoff
        self._outage_reported = False      # чтобы событие «камера пропала» шло один раз

    # ------------------------------------------------------------------ open
    def open(self) -> None:
        """Открыть/переоткрыть поток. Идемпотентно (сначала освобождает старый)."""
        self._release_capture()

        # Для RTSP форсируем транспорт TCP и таймауты — надёжнее UDP на слабой сети.
        if self._is_rtsp(self._source):
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                f"rtsp_transport;tcp|stimeout;{self._read_timeout_ms * 1000}"
            )
            self._cap = cv2.VideoCapture(self._source, cv2.CAP_FFMPEG)
        else:
            # Видеофайл или иной источник — без спец-опций.
            self._cap = cv2.VideoCapture(self._source)

        try:
            self._cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self._open_timeout_ms)
            self._cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, self._read_timeout_ms)
        except Exception:  # noqa: BLE001 - не все бэкенды поддерживают эти свойства
            pass

        if self._cap.isOpened():
            log.info("Камера подключена: %s", self._safe_source())
        else:
            log.warning("Не удалось открыть источник: %s", self._safe_source())

    # ------------------------------------------------------------------ read
    def read(self) -> Optional[Frame]:
        """Прочитать очередной кадр. Возвращает None при обрыве/ошибке."""
        if self._cap is None or not self._cap.isOpened():
            return None
        try:
            ok, frame = self._cap.read()
        except cv2.error as exc:  # pragma: no cover - редкая ошибка декодера
            log.warning("Ошибка чтения кадра: %s", exc)
            return None
        if not ok or frame is None:
            return None
        return frame

    def is_alive(self) -> bool:
        """Активен ли поток."""
        return self._cap is not None and self._cap.isOpened()

    # ------------------------------------------------------------- reconnect
    def reconnect(self) -> None:
        """Одна попытка переподключения с экспоненциальной задержкой.

        Вызывается из главного цикла при получении пустого кадра. Сон происходит
        ДО попытки открытия. При успехе задержка сбрасывается к минимуму, при
        неудаче — удваивается (но не больше max).
        """
        log.warning(
            "Переподключение к камере через %.1f с: %s",
            self._current_delay, self._safe_source(),
        )
        if not self._outage_reported:
            user_event("📷 Камера не отвечает, пытаюсь переподключиться")
            self._outage_reported = True
        self._sleep(self._current_delay)
        self.open()
        if self.is_alive():
            self._current_delay = self._min_delay
            log.info("Камера восстановлена")
            if self._outage_reported:
                user_event("📷 Камера снова на связи")
                self._outage_reported = False
        else:
            self._current_delay = min(self._current_delay * 2, self._max_delay)

    def close(self) -> None:
        """Освободить ресурсы камеры."""
        self._release_capture()
        log.info("Камера закрыта")

    # -------------------------------------------------------------- internal
    def _release_capture(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001
                pass
            self._cap = None

    @staticmethod
    def _is_rtsp(source: str) -> bool:
        return isinstance(source, str) and source.lower().startswith("rtsp://")

    def _safe_source(self) -> str:
        """Скрыть пароль из RTSP-URL при логировании."""
        s = self._source
        if "@" in s and "//" in s:
            scheme, rest = s.split("//", 1)
            if "@" in rest:
                creds, host = rest.split("@", 1)
                user = creds.split(":", 1)[0]
                return f"{scheme}//{user}:***@{host}"
        return s
