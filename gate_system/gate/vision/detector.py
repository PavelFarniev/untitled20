"""Детектор рамки номерного знака (реализация порта Detector).

Модель — models/best.pt (YOLO, обучена на номерных знаках). Она лёгкая и
работает постоянно: в простое раз в секунду проверяет, есть ли номер в кадре,
а в активном режиме даёт рамку, которую вырезаем крупным планом для Nomeroff.

Загрузчик модели инъектируется (`model_loader`) — это отделяет разбор результатов
от тяжёлой зависимости ultralytics и делает модуль юнит-тестируемым.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, List

import numpy as np

from gate.core.interfaces import Detector
from gate.core.models import BBox

log = logging.getLogger("gate_system.detector")


def _default_loader(path: str):
    """Ленивая загрузка модели ultralytics YOLO (импорт только при реальном вызове)."""
    from ultralytics import YOLO  # тяжёлый импорт, поэтому локальный

    return YOLO(path)


def extract_boxes(result, conf_threshold: float) -> List[BBox]:
    """Преобразовать результат ultralytics в список BBox домена (самые уверенные — первыми).

    :param result: объект Results (или совместимый) с атрибутом .boxes,
        у которого есть xyxy (Nx4) и conf (N).
    :param conf_threshold: нижний порог уверенности.
    """
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return []

    xyxy = np.asarray(boxes.xyxy, dtype=float).reshape(-1, 4)
    conf = np.asarray(boxes.conf, dtype=float).reshape(-1)

    out: List[BBox] = []
    for (x1, y1, x2, y2), c in zip(xyxy, conf):
        if c < conf_threshold:
            continue
        out.append(BBox(int(x1), int(y1), int(x2), int(y2), confidence=float(c)))
    out.sort(key=lambda b: b.confidence, reverse=True)
    return out


class YoloDetector(Detector):
    """Детектор рамки номера на YOLO (models/best.pt)."""

    def __init__(
        self,
        plate_model_path: str,
        plate_conf: float = 0.30,
        model_loader: Callable[[str], object] = _default_loader,
    ) -> None:
        self._plate_conf = plate_conf
        # Модели ultralytics не потокобезопасны, а пайплайны камер работают в разных
        # потоках с ОБЩИМ детектором — поэтому инференс идёт строго по очереди.
        self._lock = threading.Lock()

        self._plate_model = None
        if plate_model_path:
            try:
                log.info("Загрузка модели рамки номера: %s", plate_model_path)
                self._plate_model = model_loader(plate_model_path)
            except Exception:  # noqa: BLE001 - файла нет/битый: не падаем, Nomeroff ищет сам
                log.exception("Модель рамки номера не загружена (%s) — распознаю по всему кадру",
                              plate_model_path)
        else:
            log.warning("Модель рамки номера не задана — распознаю по всему кадру")

    @property
    def has_plate_model(self) -> bool:
        """Загружена ли модель рамки номера (нужна адаптивному режиму)."""
        return self._plate_model is not None

    def detect_plate(self, frame) -> List[BBox]:
        """Найти номерные знаки на кадре."""
        if self._plate_model is None:
            return []
        with self._lock:
            results = self._plate_model.predict(frame, conf=self._plate_conf, verbose=False)
        if not results:
            return []
        return extract_boxes(results[0], self._plate_conf)
