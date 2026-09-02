"""Интеграционные тесты ядра (pipeline.py).

Собираем полный граф use case'ов с ФЕЙКОВЫМИ адаптерами ввода (камера, детектор,
OCR, уведомления) и НАСТОЯЩИМИ реализациями хранилищ, реле и ворот. Проверяем
сценарии ТЗ целиком: разрешённый номер, неизвестный номер, anti-replay,
сохранение фото, устойчивость цикла и корректный проход run_forever.
"""
import numpy as np
import pytest

from interfaces import Detector, FrameSource, Notifier, OcrEngine
from models import BBox, CheckResult, OcrResult, OpenSource, PlateNumber
from pipeline import AccessDecision, AntiReplayGuard, RecognitionPipeline
from whitelist import JsonWhitelistRepository
from history import HistoryStore, PhotoStorage
from relay import StubRelay
from gate_controller import RelayGateController


# ------------------------------- фейки ввода -------------------------------
class FakeSource(FrameSource):
    """Отдаёт заранее заданные кадры, затем сигналит об окончании."""

    def __init__(self, frames, on_empty=None):
        self._frames = list(frames)
        self._on_empty = on_empty
        self.opened = False
        self.closed = False

    def open(self):
        self.opened = True

    def read(self):
        return self._frames.pop(0) if self._frames else None

    def is_alive(self):
        return True

    def reconnect(self):
        if self._on_empty:
            self._on_empty()  # в тесте: остановить бесконечный цикл

    def close(self):
        self.closed = True


class FakeDetector(Detector):
    def detect_vehicle(self, frame):
        return [BBox(0, 0, 100, 60, 0.9)]

    def detect_plate(self, frame):
        return [BBox(10, 40, 90, 55, 0.8)]


class FakeOcr(OcrEngine):
    def __init__(self, plate_value):
        self._plate_value = plate_value

    def read_text(self, plate_image):
        if self._plate_value is None:
            return OcrResult(None, 0.1, "???")
        return OcrResult(PlateNumber(self._plate_value), 0.9, self._plate_value)


class SpyNotifier(Notifier):
    def __init__(self):
        self.known = []
        self.unknown = []

    def notify_known(self, event):
        self.known.append(event)

    def notify_unknown(self, event):
        self.unknown.append(event)

    def is_online(self):
        return True


# --------------------------------- fixtures ---------------------------------
@pytest.fixture()
def wiring(tmp_path):
    """Собрать AccessDecision с реальными хранилищами/реле и шпионом уведомлений."""
    allowed = tmp_path / "allowed.json"
    allowed.write_text('{"allowed": ["А123ВС777"]}', encoding="utf-8")

    whitelist = JsonWhitelistRepository(str(allowed))
    history = HistoryStore(str(tmp_path / "hist.json"))
    gate = RelayGateController(StubRelay(sleeper=lambda s: None), pulse_ms=500)
    notifier = SpyNotifier()
    guard = AntiReplayGuard(window_s=30)
    photos = PhotoStorage(str(tmp_path / "photos"), writer=lambda p, f: True)

    decision = AccessDecision(
        whitelist, gate, history, notifier, guard,
        photo_saver=lambda frame: photos.save(frame, PlateNumber("А123ВС777")),
    )
    return {"decision": decision, "history": history, "notifier": notifier,
            "guard": guard, "whitelist": whitelist}


# ------------------------------ AntiReplayGuard ------------------------------
class TestAntiReplayGuard:
    def test_first_time_allowed(self):
        g = AntiReplayGuard(30)
        assert g.allow(PlateNumber("А123ВС777"), now=100.0) is True

    def test_blocked_within_window(self):
        g = AntiReplayGuard(30)
        p = PlateNumber("А123ВС777")
        g.mark_opened(p, now=100.0)
        assert g.allow(p, now=120.0) is False   # 20с < 30с

    def test_allowed_after_window(self):
        g = AntiReplayGuard(30)
        p = PlateNumber("А123ВС777")
        g.mark_opened(p, now=100.0)
        assert g.allow(p, now=131.0) is True     # 31с > 30с

    def test_independent_per_plate(self):
        g = AntiReplayGuard(30)
        g.mark_opened(PlateNumber("А123ВС777"), now=100.0)
        assert g.allow(PlateNumber("М001ММ77"), now=101.0) is True


# ------------------------------- AccessDecision ------------------------------
class TestAccessDecision:
    def test_allowed_opens_gate(self, wiring, capsys):
        r = wiring["decision"].handle(PlateNumber("А123ВС777"), frame=object())
        assert r.result is CheckResult.GRANTED and r.gate_opened is True
        assert "Gate opened" in capsys.readouterr().out
        assert len(wiring["notifier"].known) == 1
        ev = wiring["history"].recent(1)[0]
        assert ev.gate_opened is True and ev.source is OpenSource.AUTO
        assert ev.photo_path is not None

    def test_unknown_does_not_open(self, wiring, capsys):
        r = wiring["decision"].handle(PlateNumber("Х999ХХ99"), frame=object())
        assert r.result is CheckResult.DENIED and r.gate_opened is False
        assert "Gate opened" not in capsys.readouterr().out
        assert len(wiring["notifier"].unknown) == 1
        assert wiring["history"].recent(1)[0].gate_opened is False

    def test_antireplay_suppresses_second_open(self, wiring, capsys):
        d = wiring["decision"]
        d.handle(PlateNumber("А123ВС777"), frame=object())
        capsys.readouterr()  # очистить вывод первого открытия
        r2 = d.handle(PlateNumber("А123ВС777"), frame=object())
        assert r2.gate_opened is False
        assert r2.suppressed_by_antireplay is True
        assert "Gate opened" not in capsys.readouterr().out   # реле не сработало

    def test_unknown_dedup_suppresses_repeat(self, wiring):
        # Повтор того же неизвестного номера подавляется: одно уведомление, а не поток.
        d = wiring["decision"]
        r1 = d.handle(PlateNumber("Х999ХХ99"), frame=object())
        r2 = d.handle(PlateNumber("Х999ХХ99"), frame=object())
        assert r1.result is CheckResult.DENIED and r1.gate_opened is False
        assert r2.suppressed_by_antireplay is True
        assert len(wiring["notifier"].unknown) == 1   # только одно уведомление

    def test_notifier_failure_does_not_break_core(self, tmp_path):
        # Нет интернета: notify падает, но открытие/журнал должны отработать.
        class BrokenNotifier(Notifier):
            def notify_known(self, e):
                raise ConnectionError("no internet")
            def notify_unknown(self, e):
                raise ConnectionError("no internet")
            def is_online(self):
                return False

        allowed = tmp_path / "a.json"
        allowed.write_text('{"allowed": ["А123ВС777"]}', encoding="utf-8")
        history = HistoryStore(str(tmp_path / "h.json"))
        d = AccessDecision(
            JsonWhitelistRepository(str(allowed)),
            RelayGateController(StubRelay(sleeper=lambda s: None), 500),
            history, BrokenNotifier(), AntiReplayGuard(30),
        )
        r = d.handle(PlateNumber("А123ВС777"), frame=object())
        assert r.gate_opened is True                 # ядро не упало
        assert history.recent(1)[0].gate_opened is True


# ----------------------------- RecognitionPipeline ---------------------------
class StationaryPlateDetector(Detector):
    def detect_vehicle(self, frame):
        return []

    def detect_plate(self, frame):
        return [BBox(40, 40, 60, 60)]   # рамка всегда на одном месте


class MovingPlateDetector(Detector):
    def __init__(self):
        self._x = 10

    def detect_vehicle(self, frame):
        return []

    def detect_plate(self, frame):
        self._x += 20                    # рамка едет по кадру
        return [BBox(self._x, 40, self._x + 20, 60)]


class TestMotionGating:
    def test_stationary_plate_not_opened(self, wiring, capsys):
        from motion import MotionGate
        frames = [np.zeros((100, 100, 3), np.uint8) for _ in range(4)]
        pipe = RecognitionPipeline(None, StationaryPlateDetector(), FakeOcr("А123ВС777"),
                                   wiring["decision"], require_vehicle=False,
                                   motion_gate=MotionGate(threshold=0.05, confirm_frames=1))
        src = FakeSource(frames, on_empty=pipe.stop)
        pipe._source = src
        pipe.run_forever()
        assert "Gate opened" not in capsys.readouterr().out   # стоит → не открывать

    def test_moving_plate_opens(self, wiring, capsys):
        from motion import MotionGate
        frames = [np.zeros((100, 100, 3), np.uint8) for _ in range(5)]
        pipe = RecognitionPipeline(None, MovingPlateDetector(), FakeOcr("А123ВС777"),
                                   wiring["decision"], require_vehicle=False,
                                   motion_gate=MotionGate(threshold=0.05, confirm_frames=1))
        src = FakeSource(frames, on_empty=pipe.stop)
        pipe._source = src
        pipe.run_forever()
        assert "Gate opened" in capsys.readouterr().out       # едет → открыть


class TestExitMute:
    def test_exit_muted_suppresses_open(self, wiring, capsys):
        from coordinator import CameraCoordinator
        coord = CameraCoordinator(clock=lambda: 0.0)
        coord.note_open("entry")   # только что открыли на въезд → выезд приглушён
        frames = [np.zeros((100, 100, 3), np.uint8) for _ in range(4)]
        pipe = RecognitionPipeline(None, MovingPlateDetector(), FakeOcr("А123ВС777"),
                                   wiring["decision"], require_vehicle=False,
                                   role="exit", coordinator=coord)
        pipe._source = FakeSource(frames, on_empty=pipe.stop)
        pipe.run_forever()
        assert "Gate opened" not in capsys.readouterr().out

    def test_exit_not_muted_opens(self, wiring, capsys):
        from coordinator import CameraCoordinator
        coord = CameraCoordinator(clock=lambda: 0.0)   # заезда не было → не приглушён
        frames = [np.zeros((100, 100, 3), np.uint8) for _ in range(4)]
        pipe = RecognitionPipeline(None, MovingPlateDetector(), FakeOcr("А123ВС777"),
                                   wiring["decision"], require_vehicle=False,
                                   role="exit", coordinator=coord)
        pipe._source = FakeSource(frames, on_empty=pipe.stop)
        pipe.run_forever()
        assert "Gate opened" in capsys.readouterr().out


class TestRecognitionPipeline:
    def test_full_cycle_opens_for_allowed(self, wiring, capsys):
        pipe = RecognitionPipeline(
            source=None, detector=FakeDetector(),
            ocr=FakeOcr("А123ВС777"), decision=wiring["decision"],
        )
        # Источник со стоп-колбэком, чтобы завершить бесконечный цикл.
        src = FakeSource([object(), object()], on_empty=pipe.stop)
        pipe._source = src

        pipe.run_forever()

        assert src.opened is True and src.closed is True
        # Оба кадра — один и тот же номер: открытие один раз, второй подавлен anti-replay.
        assert len(wiring["notifier"].known) == 1
        assert "Gate opened" in capsys.readouterr().out

    def test_loop_survives_processing_error(self, wiring):
        class ExplodingOcr(OcrEngine):
            def __init__(self):
                self.calls = 0
            def read_text(self, plate_image):
                self.calls += 1
                raise RuntimeError("boom")

        ocr = ExplodingOcr()
        pipe = RecognitionPipeline(None, FakeDetector(), ocr, wiring["decision"])
        src = FakeSource([object(), object()], on_empty=pipe.stop)
        pipe._source = src
        # Не должно выбросить наружу: ошибки кадра логируются и цикл продолжается.
        pipe.run_forever()
        assert ocr.calls >= 1 and src.closed is True

    def test_ocr_fallback_without_plate_model(self, wiring, capsys):
        # Модель номера не находит рамок -> OCR идёт по области автомобиля.
        class NoPlateDetector(FakeDetector):
            def detect_plate(self, frame):
                return []

        pipe = RecognitionPipeline(None, NoPlateDetector(), FakeOcr("А123ВС777"),
                                   wiring["decision"])
        src = FakeSource([object()], on_empty=pipe.stop)
        pipe._source = src
        pipe.run_forever()
        assert "Gate opened" in capsys.readouterr().out   # номер распознан без модели номера

    def test_require_vehicle_false_ocr_full_frame(self, wiring, capsys):
        # Без требования машины: детектор не вызывается, OCR читает весь кадр.
        class BoomDetector(FakeDetector):
            def detect_vehicle(self, frame):
                raise AssertionError("detect_vehicle не должен вызываться")

        pipe = RecognitionPipeline(None, BoomDetector(), FakeOcr("А123ВС777"),
                                   wiring["decision"], require_vehicle=False)
        src = FakeSource([object()], on_empty=pipe.stop)
        pipe._source = src
        pipe.run_forever()
        assert "Gate opened" in capsys.readouterr().out   # номер распознан без детекции авто

    def test_heartbeat_called(self, wiring):
        beats = []
        pipe = RecognitionPipeline(None, FakeDetector(), FakeOcr(None), wiring["decision"],
                                   on_heartbeat=lambda: beats.append(1))
        src = FakeSource([object(), object()], on_empty=pipe.stop)
        pipe._source = src
        pipe.run_forever()
        assert len(beats) >= 2   # heartbeat на каждой итерации цикла

    def test_unreadable_plate_no_action(self, wiring, capsys):
        pipe = RecognitionPipeline(None, FakeDetector(), FakeOcr(None), wiring["decision"])
        src = FakeSource([object()], on_empty=pipe.stop)
        pipe._source = src
        pipe.run_forever()
        assert "Gate opened" not in capsys.readouterr().out
        assert wiring["history"].recent(1) == []   # нечитаемый номер — событий нет
