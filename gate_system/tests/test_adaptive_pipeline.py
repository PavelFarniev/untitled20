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

from gate.core.interfaces import Detector, FrameSource, OcrEngine
from gate.core.models import BBox, OcrResult, PlateNumber
from gate.core.pipeline import RecognitionPipeline


# --------------------------------- фейки ---------------------------------
class NoPlate(Detector):

    def detect_plate(self, frame):
        return []


class HasPlate(Detector):

    def detect_plate(self, frame):
        return [BBox(40, 40, 60, 55, 0.8)]


class ScriptedDetector(Detector):
    """detect_plate отдаёт рамку/пусто по заранее заданному сценарию (list[bool])."""

    def __init__(self, script):
        self._script = list(script)
        self.i = 0


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
                               adaptive=True)
    for _ in range(5):
        pipe._process_frame(FRAME)
    assert ocr.calls == 0
    assert pipe._mode == "idle"


def test_plate_wakes_system_and_runs_ocr():
    """Появление рамки будит систему: запускается OCR и решение по номеру."""
    ocr = CountingOcr()
    dec = FakeDecision()
    pipe = RecognitionPipeline(None, HasPlate(), ocr, dec,
                               adaptive=True)
    pipe._process_frame(FRAME)
    assert ocr.calls == 1
    assert dec.calls == [PlateNumber("А123ВС777")]
    assert pipe._mode == "active"


def test_active_holds_then_returns_to_idle():
    """После active_hold_s секунд без рамки система засыпает."""
    clock = [1000.0]

    det = ScriptedDetector([True, False, False])
    pipe = RecognitionPipeline(None, det, CountingOcr(), FakeDecision(),
                               adaptive=True, active_hold_s=5.0,
                               clock=lambda: clock[0])

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


def test_idle_checks_at_most_once_per_interval():
    """В простое проверка «есть ли номер» — не чаще раза в idle_interval_s секунд."""
    class CountingNoPlate(NoPlate):
        calls = 0

        def detect_plate(self, frame):
            CountingNoPlate.calls += 1
            return []

    det = CountingNoPlate()
    pipe = RecognitionPipeline(None, det, CountingOcr(), FakeDecision(), adaptive=True, idle_interval_s=1.0, clock=lambda: 50.0)
    pipe._source = FakeSource([FRAME] * 6, on_empty=pipe.stop)
    pipe.run_forever()
    assert CountingNoPlate.calls == 1   # время «не шло» — только одна проверка


def test_active_stride_by_frame_number():
    """В частом режиме считаются кадры КАМЕРЫ (frame_seq), включая пропущенные,
    пока шло медленное распознавание."""
    class SeqSource(FakeSource):
        def __init__(self, seqs, on_empty):
            super().__init__([FRAME] * len(seqs), on_empty)
            self._seqs = list(seqs)
            self.frame_seq = 0

        def read(self):
            f = super().read()
            if f is not None:
                self.frame_seq = self._seqs.pop(0)
            return f

    ocr = CountingOcr()
    pipe = RecognitionPipeline(None, HasPlate(), ocr, FakeDecision(),
                               process_every_n=3, adaptive=False)
    pipe._source = SeqSource([1, 5, 6, 10], on_empty=pipe.stop)
    pipe.run_forever()
    assert ocr.calls == 2   # кадры 5 и 10 (1 — рано, 6 — слишком близко к 5)


def test_run_forever_wakes_on_plate():
    """Сквозной прогон: пока рамки нет — решений нет; появилась — сработало."""
    det = ScriptedDetector([False, False, True, True])
    ocr = CountingOcr()
    dec = FakeDecision()
    pipe = RecognitionPipeline(None, det, ocr, dec,
                               process_every_n=1, adaptive=True, idle_interval_s=0.0)
    frames = [FRAME for _ in range(4)]
    pipe._source = FakeSource(frames, on_empty=pipe.stop)
    pipe.run_forever()
    # Пока рамки не было (2 кадра) — OCR не звался; после появления (2 кадра) —
    # сработал на каждом (дедуп anti-replay здесь не участвует, это фейк-решение).
    assert ocr.calls == 2
    assert dec.calls == [PlateNumber("А123ВС777"), PlateNumber("А123ВС777")]
    assert pipe._source.closed is True


# ------------------------- выездная камера: будим по движению -------------------------
class PositionDetector(Detector):
    """Рамка номера по сценарию X-координат (None — номера не видно)."""

    def __init__(self, xs):
        self._xs = list(xs)
        self.i = 0


    def detect_plate(self, frame):
        x = self._xs[min(self.i, len(self._xs) - 1)]
        self.i += 1
        return [] if x is None else [BBox(x, 40, x + 10, 50, 0.9)]


def _exit_pipe(xs, clock, memory_s=0.0):
    from gate.core.motion import MotionGate
    ocr = CountingOcr()
    dec = FakeDecision()
    gate = MotionGate(threshold=0.05, confirm_frames=2, memory_s=memory_s, clock=lambda: clock[0])
    pipe = RecognitionPipeline(None, PositionDetector(xs), ocr, dec, motion_gate=gate, role="exit", adaptive=True,
                               active_hold_s=5.0, clock=lambda: clock[0])
    return pipe, ocr, dec


def test_exit_parked_car_does_not_wake_camera():
    """Машина стоит за воротами: камера выезда остаётся в простое, OCR не тратится."""
    clock = [0.0]
    pipe, ocr, dec = _exit_pipe([40] * 10, clock)
    for i in range(10):
        clock[0] = float(i)
        pipe._process_frame(FRAME)
    assert pipe._mode == "idle"
    assert ocr.calls == 0 and dec.calls == []


def test_exit_moving_car_wakes_and_is_recognized():
    """Едущая машина будит камеру; распознавание — после подтверждения движения."""
    clock = [0.0]
    pipe, ocr, dec = _exit_pipe([10, 30, 50, 70], clock)
    pipe._process_frame(FRAME)            # первая позиция — сравнивать не с чем
    assert pipe._mode == "idle"
    pipe._process_frame(FRAME)            # сдвиг → проснулись, но движение ещё не подтверждено
    assert pipe._mode == "active" and ocr.calls == 0
    pipe._process_frame(FRAME)            # второй сдвиг подряд → подтверждено → OCR
    assert ocr.calls == 1 and dec.calls == [PlateNumber("А123ВС777")]


def test_exit_car_stopping_at_gate_still_read_within_memory():
    """Машина подъехала и встала у ворот: в пределах памяти движения номер дочитывается,
    а после — камера засыпает."""
    clock = [0.0]
    pipe, ocr, dec = _exit_pipe([10, 30, 50, 50, 50], clock, memory_s=8.0)
    for _ in range(3):
        pipe._process_frame(FRAME)        # подъезд: движение подтверждено на 3-м кадре
    assert ocr.calls == 1
    clock[0] = 2.0
    pipe._process_frame(FRAME)            # стоит, но 2 c < памяти 8 c → дочитываем
    assert ocr.calls == 2 and len(dec.calls) == 2
    clock[0] = 20.0
    pipe._process_frame(FRAME)            # стоит давно → не распознаём, засыпаем
    assert ocr.calls == 2 and pipe._mode == "idle"
