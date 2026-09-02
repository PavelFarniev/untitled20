"""Распознавание текста номера через EasyOCR (реализация OcrEngine).

Стек: easyocr (кроссплатформенно). Тяжёлый ридер инъектируется через
`reader_factory`, что отделяет бизнес-логику постобработки (сборка фрагментов,
усреднение уверенности, нормализация номера РФ) от ML-зависимости и делает
модуль юнит-тестируемым.

Пайплайн read_text:
1. EasyOCR отдаёт список детекций (bbox, text, confidence).
2. Фрагменты сортируются слева-направо и склеиваются.
3. Считается средняя уверенность.
4. Результат нормализуется в канонический номер РФ (utils.normalize_plate).
5. Если номер распознан и уверенность >= порога — читаемый результат, иначе None.
"""
from __future__ import annotations

import logging
from typing import Callable, List, Optional, Sequence, Tuple

from interfaces import OcrEngine
from models import OcrResult, PlateNumber
from utils import normalize_plate

log = logging.getLogger("gate_system.ocr")

# Детекция EasyOCR: (bbox из 4 точек, распознанный текст, уверенность).
Detection = Tuple[Sequence[Sequence[float]], str, float]


def _left_x(detection: Detection) -> float:
    """Левая координата X рамки — для сортировки фрагментов слева-направо."""
    bbox = detection[0]
    return min(point[0] for point in bbox)


def postprocess(detections: List[Detection], min_confidence: float) -> OcrResult:
    """Собрать распознанный номер из фрагментов EasyOCR (чистая функция).

    Стратегия (в порядке приоритета):
    1) Найти ОТДЕЛЬНЫЙ фрагмент, который сам похож на номер — так уверенность
       берётся именно у номера, а не усредняется с мусором (RUS, телефон, дилер).
    2) Если номер разбит на части — склеить фрагменты слева-направо и извлечь
       номер поиском по подстроке.
    """
    if not detections:
        return OcrResult(plate=None, confidence=0.0, raw_text="")

    ordered = sorted(detections, key=_left_x)
    texts = [d[1] for d in ordered]
    confs = [float(d[2]) for d in ordered]
    raw = "".join(texts)

    # 1) Лучший отдельный фрагмент-номер (по уверенности).
    best: Optional[tuple] = None
    for text, conf in zip(texts, confs):
        plate = normalize_plate(text)
        if plate is not None and conf >= min_confidence:
            if best is None or conf > best[1]:
                best = (plate, conf, text)
    if best is not None:
        return OcrResult(plate=best[0], confidence=best[1], raw_text=best[2])

    # 2) Склейка всех фрагментов + поиск номера в получившейся строке.
    plate = normalize_plate(raw)
    avg_conf = sum(confs) / len(confs)
    if plate is not None and avg_conf >= min_confidence:
        return OcrResult(plate=plate, confidence=avg_conf, raw_text=raw)
    return OcrResult(plate=None, confidence=avg_conf, raw_text=raw)


def _default_reader_factory(languages: List[str], gpu: bool):
    """Ленивое создание easyocr.Reader (импорт только при реальном использовании)."""
    import easyocr  # тяжёлый импорт

    return easyocr.Reader(languages, gpu=gpu)


def build_ocr_engine(cfg) -> OcrEngine:
    """Собрать движок OCR по конфигу (ocr.engine: easyocr | nomeroff).

    Используется main.py и inspect_video.py, чтобы движок выбирался в одном месте.
    """
    engine = getattr(cfg.ocr, "engine", "easyocr").lower()
    if engine == "nomeroff":
        from nomeroff_ocr import NomeroffOcrEngine  # ленивый импорт тяжёлой зависимости
        log.info("Движок OCR: Nomeroff-Net")
        return NomeroffOcrEngine(min_confidence=cfg.ocr.min_confidence,
                                 upscale=getattr(cfg.ocr, "upscale", 1.0))
    log.info("Движок OCR: EasyOCR")
    return EasyOcrEngine(cfg.ocr.languages, cfg.ocr.gpu, cfg.ocr.min_confidence)


class EasyOcrEngine(OcrEngine):
    """Обёртка над easyocr.Reader с постобработкой под номера РФ."""

    # Символы, допустимые в номере РФ: буквы-двойники (латиница и кириллица)
    # плюс цифры. Ограничение алфавита резко улучшает чтение номера, отсекая
    # «RUS», телефоны и надписи дилера ещё на этапе распознавания.
    _ALLOWLIST = "ABEKMHOPCTYXАВЕКМНОРСТУХ0123456789"

    def __init__(
        self,
        languages: List[str],
        gpu: bool = False,
        min_confidence: float = 0.4,
        reader_factory: Callable[[List[str], bool], object] = _default_reader_factory,
        allowlist: Optional[str] = None,
    ) -> None:
        self._min_confidence = min_confidence
        self._allowlist = allowlist if allowlist is not None else self._ALLOWLIST
        log.info("Инициализация EasyOCR (языки=%s, gpu=%s)", languages, gpu)
        self._reader = reader_factory(languages, gpu)

    def _readtext(self, image):
        """Вызов ридера с ограничением алфавита (фейковые ридеры — без него)."""
        if self._allowlist:
            try:
                return self._reader.readtext(image, allowlist=self._allowlist)
            except TypeError:
                pass  # тестовый ридер без параметра allowlist
        return self._reader.readtext(image)

    def read_text(self, plate_image) -> OcrResult:
        """Распознать номер по изображению (области автомобиля или знака)."""
        try:
            detections = self._readtext(plate_image)
        except Exception:  # noqa: BLE001 - граница адаптера, не роняем цикл
            log.exception("Ошибка EasyOCR при распознавании")
            return OcrResult(plate=None, confidence=0.0, raw_text="")

        result = postprocess(list(detections), self._min_confidence)
        if result.is_readable:
            log.debug("OCR: %s (conf=%.2f)", result.plate, result.confidence)
            return result

        # Номер не распознан с первого прохода — пробуем «зум»: вырезаем найденные
        # области текста, увеличиваем и распознаём повторно (номер часто мелкий).
        try:
            zoom_dets = self._zoom_detections(plate_image, detections)
            if zoom_dets:
                zoom_result = postprocess(zoom_dets, self._min_confidence)
                if zoom_result.is_readable:
                    log.debug("OCR(zoom): %s (conf=%.2f)", zoom_result.plate, zoom_result.confidence)
                    return zoom_result
        except Exception:  # noqa: BLE001
            log.debug("Ошибка зум-распознавания", exc_info=True)
        return result

    def _zoom_detections(self, image, detections) -> List[Detection]:
        """Вырезать области найденного текста, увеличить и распознать повторно."""
        import numpy as np
        import cv2

        if not isinstance(image, np.ndarray) or not detections:
            return []
        h, w = image.shape[:2]
        collected: List[Detection] = []
        for box, _text, _conf in detections:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x1, x2 = max(0, int(min(xs))), min(w, int(max(xs)))
            y1, y2 = max(0, int(min(ys))), min(h, int(max(ys)))
            if x2 - x1 < 5 or y2 - y1 < 5:
                continue
            # Небольшие поля вокруг области и увеличение до высоты ~120 px.
            pad_x, pad_y = int((x2 - x1) * 0.1), int((y2 - y1) * 0.3)
            cx1, cy1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
            cx2, cy2 = min(w, x2 + pad_x), min(h, y2 + pad_y)
            crop = image[cy1:cy2, cx1:cx2]
            if crop.size == 0:
                continue
            scale = max(1.0, 120.0 / max(1, cy2 - cy1))
            big = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            collected.extend(self._readtext(big))
        return collected
