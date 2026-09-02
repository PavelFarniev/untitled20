"""Детекция автомобиля и номерного знака через YOLOv8n (реализация Detector).

Стек: ultralytics (кроссплатформенно). На Raspberry Pi модель можно
экспортировать в ONNX (`model.export(format="onnx")`) и запускать через
onnxruntime для ускорения — интерфейс Detector при этом не меняется.

Детекция автомобиля использует стандартную модель COCO (класс car/bus/truck/
motorcycle). Детекция номера — отдельную модель, обученную на номерных знаках
(её путь задаётся в config.detection.plate_model_path).

Загрузчик модели инъектируется (`model_loader`) — это отделяет бизнес-логику
разбора результатов от тяжёлой зависимости и делает модуль юнит-тестируемым.
"""
from __future__ import annotations

import logging
from typing import Callable, List, Optional, Set

import numpy as np

from interfaces import Detector
from models import BBox

log = logging.getLogger("gate_system.detector")

# Идентификаторы классов транспорта в датасете COCO (порядок стандартный).
COCO_VEHICLE_CLASSES: Set[int] = {2, 3, 5, 7}  # car, motorcycle, bus, truck


def _default_loader(path: str):
    """Ленивая загрузка модели ultralytics YOLO (импорт только при реальном вызове)."""
    from ultralytics import YOLO  # тяжёлый импорт, поэтому локальный

    return YOLO(path)


def extract_boxes(
    result,
    conf_threshold: float,
    allowed_classes: Optional[Set[int]] = None,
) -> List[BBox]:
    """Преобразовать результат ultralytics в список BBox домена.

    :param result: объект Results (или совместимый) с атрибутом .boxes,
        у которого есть xyxy (Nx4), conf (N), cls (N).
    :param conf_threshold: нижний порог уверенности.
    :param allowed_classes: если задан — оставить только эти классы; иначе все.
    """
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return []

    xyxy = np.asarray(boxes.xyxy, dtype=float).reshape(-1, 4)
    conf = np.asarray(boxes.conf, dtype=float).reshape(-1)
    cls = np.asarray(boxes.cls, dtype=float).reshape(-1).astype(int)

    out: List[BBox] = []
    for (x1, y1, x2, y2), c, k in zip(xyxy, conf, cls):
        if c < conf_threshold:
            continue
        if allowed_classes is not None and k not in allowed_classes:
            continue
        out.append(BBox(int(x1), int(y1), int(x2), int(y2), confidence=float(c)))
    # Сначала самые уверенные детекции.
    out.sort(key=lambda b: b.confidence, reverse=True)
    return out


class YoloDetector(Detector):
    """Детектор на YOLOv8n: отдельные модели для транспорта и для номеров."""

    def __init__(
        self,
        model_path: str,
        plate_model_path: str,
        vehicle_conf: float = 0.45,
        plate_conf: float = 0.35,
        vehicle_classes: Optional[Set[int]] = None,
        model_loader: Callable[[str], object] = _default_loader,
    ) -> None:
        self._vehicle_conf = vehicle_conf
        self._plate_conf = plate_conf
        self._vehicle_classes = vehicle_classes if vehicle_classes is not None else COCO_VEHICLE_CLASSES

        log.info("Загрузка модели транспорта: %s", model_path)
        self._vehicle_model = model_loader(model_path)

        self._plate_model = None
        if plate_model_path:
            try:
                log.info("Загрузка модели номеров: %s", plate_model_path)
                self._plate_model = model_loader(plate_model_path)
            except Exception:  # noqa: BLE001 - файла нет/битый: не падаем, работаем по ROI авто
                log.warning(
                    "Модель номеров не загружена (%s) — распознаю по области автомобиля",
                    plate_model_path,
                )
        else:
            log.info("Модель номеров не задана — распознаю по области автомобиля")

    def detect_vehicle(self, frame) -> List[BBox]:
        """Найти транспортные средства на кадре."""
        results = self._vehicle_model.predict(frame, conf=self._vehicle_conf, verbose=False)
        if not results:
            return []
        return extract_boxes(results[0], self._vehicle_conf, self._vehicle_classes)

    def detect_plate(self, frame) -> List[BBox]:
        """Найти номерные знаки на кадре (или в переданном ROI автомобиля)."""
        if self._plate_model is None:
            return []
        results = self._plate_model.predict(frame, conf=self._plate_conf, verbose=False)
        if not results:
            return []
        # У специализированной модели номеров обычно один класс — фильтр не нужен.
        return extract_boxes(results[0], self._plate_conf, allowed_classes=None)
