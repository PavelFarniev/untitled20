"""Источники кадров: RTSP IP-камера и «свежий кадр» в фоне.

* RTSPCamera — тонкая обёртка над cv2.VideoCapture (RTSP или видеофайл) с
  переподключением по экспоненциальной задержке (1 → 2 → 4 → … → max).
* LatestFrameSource — фоновый поток, который непрерывно читает камеру и хранит
  ТОЛЬКО самый свежий кадр. Распознавание на Raspberry Pi идёт медленнее, чем
  камера присылает кадры; без этого кадры копились бы в буфере, и система
  распознавала бы картинку многосекундной давности. Этот же свежий кадр
  отдаётся кнопке «📷 Фото» и диагностике («нет кадров дольше N секунд»).

Видеофайл (для проверки на Маке) проигрывается в реальном темпе по его FPS и
зацикливается — так он ведёт себя как живая камера.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

import cv2
import numpy as np

from gate.core.interfaces import FrameSource
from gate.common.events import event as user_event

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
        open_timeout_ms: int = 10000,
        read_timeout_ms: int = 5000,
        sleeper: Callable[[float], None] = time.sleep,
        name: str = "camera",
    ) -> None:
        """
        :param source: RTSP-URL или путь к видеофайлу.
        :param min_delay_s: начальная задержка переподключения.
        :param max_delay_s: максимальная задержка переподключения.
        :param open_timeout_ms: таймаут открытия потока (FFmpeg).
        :param read_timeout_ms: таймаут чтения кадра (FFmpeg) — защита от
            «вечного» зависания при пропаже камеры из сети.
        :param sleeper: функция сна (инъекция для тестируемости backoff).
        :param name: имя камеры для журнала.
        """
        self._source = source
        self._min_delay = min_delay_s
        self._max_delay = max_delay_s
        self._open_timeout_ms = open_timeout_ms
        self._read_timeout_ms = read_timeout_ms
        self._sleep = sleeper
        self.name = name

        self._cap: Optional[cv2.VideoCapture] = None
        self._current_delay = min_delay_s  # текущая задержка backoff
        self._outage_reported = False      # чтобы событие «камера пропала» шло один раз

    @property
    def is_file(self) -> bool:
        """Источник — видеофайл (а не сетевой поток)."""
        return not self._is_network(self._source)

    def fps(self) -> float:
        """FPS источника (для видеофайла — темп воспроизведения)."""
        try:
            value = float(self._cap.get(cv2.CAP_PROP_FPS)) if self._cap is not None else 0.0
        except Exception:  # noqa: BLE001
            value = 0.0
        return value if 1.0 <= value <= 120.0 else 25.0

    # ------------------------------------------------------------------ open
    def open(self) -> None:
        """Открыть/переоткрыть поток. Идемпотентно (сначала освобождает старый)."""
        self._release_capture()

        if self._is_network(self._source):
            # TCP надёжнее UDP на слабой сети; timeout — таймаут сокета в мкс.
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                f"rtsp_transport;tcp|timeout;{self._read_timeout_ms * 1000}"
            )
            params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self._open_timeout_ms,
                      cv2.CAP_PROP_READ_TIMEOUT_MSEC, self._read_timeout_ms]
            try:
                # Таймауты нужно передавать при открытии — после open() они не действуют.
                self._cap = cv2.VideoCapture(self._source, cv2.CAP_FFMPEG, params)
            except (TypeError, cv2.error):  # старый OpenCV без параметров конструктора
                self._cap = cv2.VideoCapture(self._source, cv2.CAP_FFMPEG)
        else:
            self._cap = cv2.VideoCapture(self._source)

        if self._cap.isOpened():
            log.info("Камера '%s' подключена: %s", self.name, self._safe_source())
        else:
            log.warning("Камера '%s': не удалось открыть источник %s", self.name, self._safe_source())

    # ------------------------------------------------------------------ read
    def read(self) -> Optional[Frame]:
        """Прочитать очередной кадр. Возвращает None при обрыве/ошибке/конце файла."""
        if self._cap is None or not self._cap.isOpened():
            return None
        try:
            ok, frame = self._cap.read()
        except cv2.error as exc:  # pragma: no cover - редкая ошибка декодера
            log.warning("Камера '%s': ошибка чтения кадра: %s", self.name, exc)
            return None
        if not ok or frame is None:
            return None
        return frame

    def is_alive(self) -> bool:
        """Открыт ли поток."""
        return self._cap is not None and self._cap.isOpened()

    # ------------------------------------------------------------- reconnect
    def reconnect(self, sleeper: Optional[Callable[[float], object]] = None) -> None:
        """Одна попытка переподключения с экспоненциальной задержкой.

        Сон происходит ДО попытки открытия. При успехе задержка сбрасывается к
        минимуму, при неудаче — удваивается (но не больше max).

        :param sleeper: своя функция ожидания. Если она вернула True (например,
            threading.Event.wait при остановке системы) — переподключение
            отменяется, чтобы остановка не ждала до 30 секунд.
        """
        log.warning("Камера '%s': переподключение через %.1f с: %s",
                    self.name, self._current_delay, self._safe_source())
        if not self._outage_reported:
            user_event(f"📷 Камера «{self.name}» не отвечает, пытаюсь переподключиться")
            self._outage_reported = True
        if (sleeper or self._sleep)(self._current_delay) is True:
            return  # система останавливается — не подключаемся
        self.open()
        if self.is_alive():
            self._current_delay = self._min_delay
            log.info("Камера '%s' восстановлена", self.name)
            if self._outage_reported:
                user_event(f"📷 Камера «{self.name}» снова на связи")
                self._outage_reported = False
        else:
            self._current_delay = min(self._current_delay * 2, self._max_delay)

    def restart_file(self) -> None:
        """Видеофайл закончился — начать сначала (тихо, без событий «камера пропала»)."""
        self._release_capture()
        self._cap = cv2.VideoCapture(self._source)
        log.debug("Камера '%s': видеофайл начат сначала", self.name)

    def close(self) -> None:
        """Освободить ресурсы камеры."""
        self._release_capture()
        log.info("Камера '%s' закрыта", self.name)

    # -------------------------------------------------------------- internal
    def _release_capture(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001
                pass
            self._cap = None

    @staticmethod
    def _is_network(source: str) -> bool:
        return isinstance(source, str) and "://" in source

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


class LatestFrameSource(FrameSource):
    """Фоновый захват: всегда отдаёт САМЫЙ СВЕЖИЙ кадр камеры.

    Поток-читатель непрерывно забирает кадры (и сам переподключается при
    обрыве), а потребитель (пайплайн распознавания) получает последний кадр.
    Кадры, пришедшие, пока шло распознавание, пропускаются — задержка не копится.

    `frame_seq` — порядковый номер кадра камеры, возвращённого последним read();
    по нему пайплайн понимает, сколько кадров прошло (прореживание «каждый N-й»).
    """

    def __init__(
        self,
        camera: RTSPCamera,
        read_timeout_s: float = 5.0,
        stale_after_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cam = camera
        self.name = getattr(camera, "name", "camera")
        self._read_timeout_s = read_timeout_s
        self._stale_after_s = stale_after_s
        self._clock = clock
        self._cond = threading.Condition()
        self._frame: Optional[Frame] = None
        self._seq = 0
        self._frame_ts: Optional[float] = None
        self._returned_seq = 0
        self._started_at: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.frame_seq = 0

    # ----------------------------------------------------------- FrameSource
    def open(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._started_at = self._clock()
        self._thread = threading.Thread(target=self._loop, name=f"grab:{self.name}", daemon=True)
        self._thread.start()

    def read(self) -> Optional[Frame]:
        """Дождаться кадра новее последнего выданного (не дольше read_timeout_s)."""
        with self._cond:
            ready = self._cond.wait_for(
                lambda: self._seq > self._returned_seq or self._stop.is_set(),
                timeout=self._read_timeout_s,
            )
            if not ready or self._stop.is_set() or self._frame is None:
                return None
            self._returned_seq = self._seq
            self.frame_seq = self._seq
            return self._frame

    def is_alive(self) -> bool:
        """Камера жива, если свежий кадр приходил не позже stale_after_s назад."""
        age = self.frame_age()
        return age is not None and age < self._stale_after_s

    def reconnect(self) -> None:
        """Переподключением занимается фоновый поток — здесь делать нечего."""

    def close(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._cam.close()

    # ------------------------------------------------------- для бота/диагностики
    def latest(self) -> Optional[Frame]:
        """Последний кадр камеры (или None, если кадров ещё не было)."""
        with self._cond:
            return self._frame

    def frame_age(self) -> Optional[float]:
        """Сколько секунд назад пришёл последний кадр (None — ещё не было)."""
        with self._cond:
            ts = self._frame_ts
        return None if ts is None else self._clock() - ts

    def is_starting(self, grace_s: float) -> bool:
        """Источник только что запущен и ещё не прислал кадров (не тревожиться)."""
        return (self._frame_ts is None and self._started_at is not None
                and self._clock() - self._started_at < grace_s)

    # ------------------------------------------------------------- поток чтения
    def _loop(self) -> None:
        cam = self._cam
        cam.open()
        is_file = getattr(cam, "is_file", False)
        period = 1.0 / cam.fps() if is_file else 0.0
        next_due = time.monotonic()
        while not self._stop.is_set():
            try:
                frame = cam.read()
                if frame is None:
                    if self._stop.is_set():
                        break
                    if is_file and cam.is_alive():
                        cam.restart_file()       # конец ролика — по кругу
                    else:
                        # обрыв — переподключение с задержкой, прерываемой остановкой
                        cam.reconnect(sleeper=self._stop.wait)
                    continue
                with self._cond:
                    self._frame = frame
                    self._seq += 1
                    self._frame_ts = self._clock()
                    self._cond.notify_all()
                if period:
                    # Видеофайл проигрываем в реальном темпе, как живую камеру.
                    next_due += period
                    delay = next_due - time.monotonic()
                    if delay > 0:
                        self._stop.wait(delay)
                    else:
                        next_due = time.monotonic()
            except Exception:  # noqa: BLE001 - поток чтения не должен умирать
                log.exception("Камера '%s': ошибка в потоке чтения", self.name)
                self._stop.wait(1.0)
        log.info("Камера '%s': поток чтения остановлен", self.name)
