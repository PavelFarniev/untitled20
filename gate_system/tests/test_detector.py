"""Тесты YoloDetector (детектор рамки номера).

Разбор результатов покрыт тестами на ФЕЙКОВЫХ моделях (без torch/ultralytics).
Проверка настоящей модели models/best.pt пропускается, если ultralytics не
установлен, и выполняется на Маке/Pi с установленным requirements.txt.
"""
import numpy as np
import pytest

from gate.core.models import BBox
from gate.paths import PROJECT_ROOT
from gate.vision.detector import YoloDetector, extract_boxes


class FakeBoxes:
    """Имитация ultralytics Results.boxes."""

    def __init__(self, xyxy, conf):
        self.xyxy = np.array(xyxy, dtype=float)
        self.conf = np.array(conf, dtype=float)

    def __len__(self):
        return len(self.conf)


class FakeResult:
    def __init__(self, boxes):
        self.boxes = boxes


class FakeModel:
    """Имитация ultralytics YOLO: predict() возвращает заранее заданный результат."""

    def __init__(self, result):
        self._result = result
        self.last_conf = None

    def predict(self, frame, conf, verbose=False):
        self.last_conf = conf
        return [self._result]


class TestExtractBoxes:
    def test_empty_boxes(self):
        assert extract_boxes(FakeResult(FakeBoxes([], [])), 0.3) == []

    def test_confidence_filter(self):
        out = extract_boxes(FakeResult(FakeBoxes([[0, 0, 10, 10], [0, 0, 5, 5]], [0.9, 0.1])), 0.5)
        assert len(out) == 1 and out[0].confidence == 0.9

    def test_sorted_by_confidence_desc(self):
        out = extract_boxes(FakeResult(FakeBoxes([[0, 0, 1, 1], [0, 0, 2, 2]], [0.4, 0.95])), 0.3)
        assert [b.confidence for b in out] == [0.95, 0.4]

    def test_coordinates_converted_to_int_bbox(self):
        b = extract_boxes(FakeResult(FakeBoxes([[1.7, 2.2, 30.9, 40.1]], [0.8])), 0.3)[0]
        assert isinstance(b, BBox)
        assert (b.x1, b.y1, b.x2, b.y2) == (1, 2, 30, 40)


class TestYoloDetector:
    def test_detect_plate_with_model(self):
        model = FakeModel(FakeResult(FakeBoxes([[5, 5, 25, 15]], [0.6])))
        det = YoloDetector("plate.pt", plate_conf=0.35, model_loader=lambda p: model)
        out = det.detect_plate(np.zeros((20, 30, 3), np.uint8))
        assert det.has_plate_model is True
        assert len(out) == 1 and out[0].width == 20
        assert model.last_conf == 0.35

    def test_without_model_returns_empty(self):
        det = YoloDetector("", model_loader=lambda p: pytest.fail("не должен грузить"))
        assert det.has_plate_model is False
        assert det.detect_plate(np.zeros((10, 10, 3), np.uint8)) == []

    def test_broken_model_file_does_not_crash(self):
        def broken(path):
            raise FileNotFoundError(path)

        det = YoloDetector("nope.pt", model_loader=broken)
        assert det.has_plate_model is False


def test_real_best_pt_loads_and_runs():
    """Настоящая модель models/best.pt загружается текущей версией ultralytics и
    отрабатывает на кадре (пропускается, если ultralytics не установлен)."""
    pytest.importorskip("ultralytics")
    weights = PROJECT_ROOT / "models" / "best.pt"
    if not weights.exists():
        pytest.skip("нет models/best.pt")
    det = YoloDetector(str(weights), plate_conf=0.3)
    assert det.has_plate_model is True
    assert isinstance(det.detect_plate(np.zeros((480, 640, 3), np.uint8)), list)
