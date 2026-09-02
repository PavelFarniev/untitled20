"""Тесты YoloDetector.

Логика разбора результатов и фильтрации классов/уверенности покрыта юнит-тестами
на ФЕЙКОВЫХ моделях (без torch/ultralytics). Реальная инференс-проверка вынесена
в отдельный тест, который автоматически пропускается, если ultralytics не
установлен (напр. в лёгкой CI-песочнице), но выполняется на машине разработчика.
"""
import numpy as np
import pytest

from detector import COCO_VEHICLE_CLASSES, YoloDetector, extract_boxes
from models import BBox


# --------------------------- фейки ultralytics ---------------------------
class FakeBoxes:
    """Имитация ultralytics Results.boxes."""

    def __init__(self, xyxy, conf, cls):
        self.xyxy = np.array(xyxy, dtype=float)
        self.conf = np.array(conf, dtype=float)
        self.cls = np.array(cls, dtype=float)

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


def make_loader(mapping):
    """Загрузчик, отдающий разные фейковые модели по пути."""
    def loader(path):
        return mapping[path]
    return loader


# ------------------------------ extract_boxes ------------------------------
class TestExtractBoxes:
    def test_empty_boxes(self):
        assert extract_boxes(FakeResult(FakeBoxes([], [], [])), 0.3) == []

    def test_confidence_filter(self):
        boxes = FakeBoxes([[0, 0, 10, 10], [0, 0, 5, 5]], [0.9, 0.1], [2, 2])
        out = extract_boxes(FakeResult(boxes), conf_threshold=0.5)
        assert len(out) == 1
        assert out[0].confidence == 0.9

    def test_class_filter(self):
        boxes = FakeBoxes([[0, 0, 10, 10], [0, 0, 5, 5]], [0.9, 0.8], [2, 15])  # car, cat
        out = extract_boxes(FakeResult(boxes), 0.3, allowed_classes={2})
        assert len(out) == 1
        assert isinstance(out[0], BBox)

    def test_sorted_by_confidence_desc(self):
        boxes = FakeBoxes([[0, 0, 1, 1], [0, 0, 2, 2]], [0.4, 0.95], [2, 7])
        out = extract_boxes(FakeResult(boxes), 0.3, allowed_classes=COCO_VEHICLE_CLASSES)
        assert [b.confidence for b in out] == [0.95, 0.4]

    def test_coordinates_converted_to_int_bbox(self):
        boxes = FakeBoxes([[1.7, 2.2, 30.9, 40.1]], [0.8], [2])
        out = extract_boxes(FakeResult(boxes), 0.3)
        b = out[0]
        assert (b.x1, b.y1, b.x2, b.y2) == (1, 2, 30, 40)
        assert b.width == 29 and b.height == 38


# ------------------------------ YoloDetector ------------------------------
class TestYoloDetector:
    def test_detect_vehicle_filters_non_vehicles(self):
        boxes = FakeBoxes(
            [[0, 0, 10, 10], [0, 0, 8, 8], [0, 0, 6, 6]],
            [0.9, 0.8, 0.7],
            [2, 0, 7],  # car, person, truck
        )
        model = FakeModel(FakeResult(boxes))
        det = YoloDetector(
            "veh.pt", "", vehicle_conf=0.45,
            model_loader=make_loader({"veh.pt": model}),
        )
        out = det.detect_vehicle(frame=np.zeros((10, 10, 3), np.uint8))
        assert len(out) == 2  # person отфильтрован
        assert model.last_conf == 0.45

    def test_detect_plate_without_model_returns_empty(self):
        model = FakeModel(FakeResult(FakeBoxes([], [], [])))
        det = YoloDetector("veh.pt", "", model_loader=make_loader({"veh.pt": model}))
        assert det.detect_plate(frame=np.zeros((10, 10, 3), np.uint8)) == []

    def test_detect_plate_with_model(self):
        veh = FakeModel(FakeResult(FakeBoxes([], [], [])))
        plate_boxes = FakeBoxes([[5, 5, 25, 15]], [0.6], [0])
        plate = FakeModel(FakeResult(plate_boxes))
        det = YoloDetector(
            "veh.pt", "plate.pt", plate_conf=0.35,
            model_loader=make_loader({"veh.pt": veh, "plate.pt": plate}),
        )
        out = det.detect_plate(frame=np.zeros((20, 30, 3), np.uint8))
        assert len(out) == 1
        assert out[0].width == 20


# -------------------- реальная инференс-проверка (опц.) --------------------
def test_real_yolov8n_inference_on_bundled_image():
    """Сквозная проверка на реальной модели и встроенном изображении.

    Пропускается, если ultralytics/веса недоступны (лёгкая песочница). На машине
    разработчика с установленным requirements.txt тест выполняется и проверяет,
    что на изображении с автобусом реально находится транспортное средство.
    """
    ultra = pytest.importorskip("ultralytics")
    from pathlib import Path

    assets = Path(ultra.__file__).parent / "assets"
    image = assets / "bus.jpg"
    if not image.exists():
        pytest.skip("нет встроенного изображения bus.jpg")

    det = YoloDetector("yolov8n.pt", "", vehicle_conf=0.4)
    import cv2
    frame = cv2.imread(str(image))
    vehicles = det.detect_vehicle(frame)
    assert len(vehicles) >= 1
