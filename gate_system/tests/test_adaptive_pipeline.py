"""Тесты адаптивного режима RecognitionPipeline.

Идея режима: в простое крутится ТОЛЬКО лёгкая детекция рамки (best.pt) с редким
шагом; тяжёлый распознаватель (Nomeroff) подключается лишь при появлении номера,
после чего частый шаг держится ещё active_hold_s секунд. Проверяем:
* в простое без рамки OCR не вызывается вовсе (Nomeroff не трогаем);
* появление рамки будит систему и запускает распознавание/решение;
* после active_hold_s без рамки система возвращается в простой;
* шаг прореживания зависит от режима, а в НЕадаптивном режиме постоянен.
"""
import numpy as np
import pytest

from interfaces import Detector, FrameSource, OcrEngine
from models import BBox, OcrResult, PlateNumber
from pipeline import RecognitionPipeline
import pipeline as pipeline_mod


# --------------------------------- фейки ---------------------------------
class NoPlate(Detector):
    def detect_vehicle(self, frame):
        return []

    def detect_plate(self, frame):
        return []


class HasPlate(Detector):
    def detect_vehicle(self, frame):
        return []

    def detect_plate(self, frame):
        return [BBox(40, 40, 60, 55, 0.8)]


class ScriptedDetector(Detector):
    """detect_plate отдаёт рамку/пусто по заранее заданному сценарию (list[bool])."""

    def __init__(self, script):
        self._script = list(script)
        self.i = 0

    def detect_vehicle(self, frame):
        return []

    def detect_plate(self, frame):
        has = self._script[min(self.i, len(self._script) - 1)]
        self.i += 1
        return [BBox(40, 40, 60, 55, 0.8)] if has else []


class CountingOcr(OcrEngine):
    """Считает вызовы read_text — это индикатор «тяжёлый OCR отработал»."""

    def __init__(self, plate="А123ВС777"):
        self.calls = 0
        self._plate = plate

    def read_text(self, region):
        self.calls += 1
        return OcrResult(PlateNumber(self._plate), 0.9, self._plate)


class FakeDecision:
    def __init__(self):
        self.calls = []

    def handle(self, plate, frame=None):
        self.calls.append(plate)
        return None


class FakeSource(FrameSource):
    def __init__(self, frames, on_empty=None):
        self._frames = list(frames)
        self._on_empty = on_empty
        self.closed = False

    def open(self):
        pass

    def read(self):
        return self._frames.pop(0) if self._frames else None

    def is_alive(self):
        return True

    def reconnect(self):
        if self._on_empty:
            self._on_empty()

    def close(self):
        self.closed = True


FRAME = np.zeros((100, 100, 3), np.uint8)


# --------------------------------- тесты ---------------------------------
def test_idle_without_plate_skips_ocr():
    """Простой без рамки: тяжёлый OCR не должен вызываться ни разу."""
    ocr = CountingOcr()
    pipe = RecognitionPipeline(None, NoPlate(), ocr, FakeDecision(),
                               require_vehicle=False, adaptive=True)
    for _ in range(5):
        pipe._process_frame(FRAME)
    assert ocr.calls == 0
    assert pipe._mode == "idle"


def test_plate_wakes_system_and_runs_ocr():
    """Появление рамки будит систему: запускается OCR и решение по номеру."""
    ocr = CountingOcr()
    dec = FakeDecision()
    pipe = RecognitionPipeline(None, HasPlate(), ocr, dec,
                               require_vehicle=False, adaptive=True)
    pipe._process_frame(FRAME)
    assert ocr.calls == 1
    assert dec.calls == [PlateNumber("А123ВС777")]
    assert pipe._mode == "active"


def test_active_holds_then_returns_to_idle(monkeypatch):
    """После active_hold_s секунд без рамки система засыпает."""
    clock = [1000.0]
    monkeypatch.setattr(pipeline_mod.time, "monotonic", lambda: clock[0])

    det = ScriptedDetector([True, False, False])
    pipe = RecognitionPipeline(None, det, CountingOcr(), FakeDecision(),
                               require_vehicle=False, adaptive=True, active_hold_s=5.0)

    # 1) Рамка есть → активный режим, удержание до 1005.
    pipe._process_frame(FRAME)
    assert pipe._mode == "active"

    # 2) Рамки нет, прошло 2 c (< 5 c удержания) → всё ещё активны.
    clock[0] = 1002.0
    pipe._process_frame(FRAME)
    assert pipe._mode == "active"

    # 3) Рамки нет, прошло 6.5 c (> 5 c) → возврат в простой.
    clock[0] = 1006.5
    pipe._process_frame(FRAME)
    assert pipe._mode == "idle"


def test_stride_switches_with_mode():
    """Шаг прореживания: редкий в простое, частый в активном режиме."""
    pipe = RecognitionPipeline(None, HasPlate(), CountingOcr(), FakeDecision(),
                               process_every_n=3, require_vehicle=False,
                               adaptive=True, idle_every_n=30)
    assert pipe._current_stride() == 30   # простой
    pipe._mode = "active"
    assert pipe._current_stride() == 3    # активный


def test_non_adaptive_stride_constant():
    """Без адаптива шаг постоянен независимо от внутреннего режима."""
    pipe = RecognitionPipeline(None, HasPlate(), CountingOcr(), FakeDecision(),
                               process_every_n=3, require_vehicle=False, adaptive=False)
    assert pipe._current_stride() == 3
    pipe._mode = "active"
    assert pipe._current_stride() == 3


def test_run_forever_wakes_on_plate():
    """Сквозной прогон: пока рамки нет — решений нет; появилась — сработало."""
    det = ScriptedDetector([False, False, True, True])
    ocr = CountingOcr()
    dec = FakeDecision()
    pipe = RecognitionPipeline(None, det, ocr, dec,
                               process_every_n=1, require_vehicle=False,
                               adaptive=True, idle_every_n=1)
    frames = [FRAME for _ in range(4)]
    pipe._source = FakeSource(frames, on_empty=pipe.stop)
    pipe.run_forever()
    # Пока рамки не было (2 кадра) — OCR не звался; после появления (2 кадра) —
    # сработал на каждом (дедуп anti-replay здесь не участвует, это фейк-решение).
    assert ocr.calls == 2
    assert dec.calls == [PlateNumber("А123ВС777"), PlateNumber("А123ВС777")]
    assert pipe._source.closed is True
