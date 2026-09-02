"""Тесты EasyOcrEngine.

Постобработка (сборка фрагментов, порог, нормализация) проверяется на фейковом
ридере без torch. Реальный прогон EasyOCR — отдельный тест, пропускаемый при
отсутствии зависимости.
"""
import pytest

from ocr import EasyOcrEngine, postprocess
from models import OcrResult


def bbox(x1, y1, x2, y2):
    """Прямоугольная рамка из 4 точек в формате EasyOCR."""
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


class FakeReader:
    """Имитация easyocr.Reader: readtext() возвращает заданные детекции."""

    def __init__(self, detections):
        self._detections = detections

    def readtext(self, image):
        return self._detections


def make_engine(detections, min_confidence=0.4):
    return EasyOcrEngine(
        languages=["en", "ru"],
        min_confidence=min_confidence,
        reader_factory=lambda langs, gpu: FakeReader(detections),
    )


class TestPostprocess:
    def test_empty_detections(self):
        r = postprocess([], 0.4)
        assert r.plate is None and r.confidence == 0.0 and r.raw_text == ""

    def test_single_fragment_normalized(self):
        r = postprocess([(bbox(0, 0, 100, 30), "A123BC777", 0.9)], 0.4)
        assert r.is_readable
        assert r.plate.value == "А123ВС777"  # латиница сконвертирована в кириллицу

    def test_fragments_joined_left_to_right(self):
        # Фрагменты приходят в обратном порядке — должны собраться по X.
        dets = [
            (bbox(60, 0, 120, 30), "BC777", 0.8),
            (bbox(0, 0, 60, 30), "A123", 0.9),
        ]
        r = postprocess(dets, 0.4)
        assert r.raw_text == "A123BC777"
        assert r.plate.value == "А123ВС777"

    def test_below_confidence_threshold_is_unreadable(self):
        r = postprocess([(bbox(0, 0, 100, 30), "A123BC777", 0.2)], min_confidence=0.5)
        assert r.plate is None          # распозналось, но уверенность мала
        assert r.raw_text == "A123BC777"

    def test_garbage_text_unreadable(self):
        r = postprocess([(bbox(0, 0, 50, 20), "!!??", 0.95)], 0.4)
        assert r.plate is None

    def test_average_confidence(self):
        dets = [
            (bbox(0, 0, 60, 30), "A123", 0.6),
            (bbox(60, 0, 120, 30), "BC777", 0.8),
        ]
        r = postprocess(dets, 0.4)
        assert r.confidence == pytest.approx(0.7)


class TestEngine:
    def test_read_text_success(self):
        eng = make_engine([(bbox(0, 0, 100, 30), "м001мм77", 0.85)])
        r = eng.read_text(plate_image=None)
        assert r.is_readable and r.plate.value == "М001ММ77"

    def test_read_text_handles_reader_exception(self):
        class Boom:
            def readtext(self, image):
                raise RuntimeError("cuda oom")

        eng = EasyOcrEngine(["en"], reader_factory=lambda l, g: Boom())
        r = eng.read_text(plate_image=None)
        assert isinstance(r, OcrResult) and r.plate is None


def test_real_easyocr_available():
    """Проверка реального EasyOCR — пропускается, если пакет не установлен."""
    pytest.importorskip("easyocr")
    # На машине разработчика: создаём ридер и распознаём синтетическое изображение.
    import numpy as np
    import cv2

    img = np.full((60, 200, 3), 255, np.uint8)
    cv2.putText(img, "A123BC777", (5, 45), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)
    eng = EasyOcrEngine(["en"], gpu=False, min_confidence=0.1)
    result = eng.read_text(img)
    # Не жёстко проверяем точный номер (OCR шумит), но результат — OcrResult.
    assert result is not None
