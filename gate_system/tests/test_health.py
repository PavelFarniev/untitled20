"""Тесты Watchdog (watchdog.py) и SelfDiagnostics (diagnostics.py)."""
from datetime import datetime, timezone

from watchdog import Watchdog
from diagnostics import SelfDiagnostics
from config import Config
from models import PlateNumber


# --------------------------------- Watchdog ---------------------------------
class FakeClock:
    """Управляемые часы для детерминированных тестов."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class TestWatchdog:
    def test_fresh_heartbeat_alive(self):
        clk = FakeClock()
        wd = Watchdog(timeout_s=30, clock=clk)
        wd.beat("vision")
        clk.advance(10)
        assert wd.check() == {"vision": True}
        assert wd.stale_subsystems() == []

    def test_stale_heartbeat_detected(self):
        clk = FakeClock()
        wd = Watchdog(timeout_s=30, clock=clk)
        wd.beat("vision")
        clk.advance(31)
        assert wd.check() == {"vision": False}
        assert wd.stale_subsystems() == ["vision"]

    def test_on_stale_called(self):
        clk = FakeClock()
        called = []
        wd = Watchdog(timeout_s=30, on_stale=called.append, clock=clk)
        wd.beat("telegram")
        clk.advance(40)
        assert wd.run_once() == ["telegram"]
        assert called == ["telegram"]

    def test_beat_refreshes(self):
        clk = FakeClock()
        wd = Watchdog(timeout_s=30, clock=clk)
        wd.beat("vision")
        clk.advance(20)
        wd.beat("vision")          # обновили heartbeat
        clk.advance(20)            # с момента обновления прошло 20 < 30
        assert wd.stale_subsystems() == []

    def test_on_stale_exception_swallowed(self):
        clk = FakeClock()
        def boom(name):
            raise RuntimeError("x")
        wd = Watchdog(timeout_s=10, on_stale=boom, clock=clk)
        wd.beat("a")
        clk.advance(20)
        assert wd.run_once() == ["a"]   # не бросает наружу

    def test_multiple_subsystems(self):
        clk = FakeClock()
        wd = Watchdog(timeout_s=30, clock=clk)
        wd.beat("vision")
        clk.advance(31)
        wd.beat("telegram")        # свежий
        stale = wd.stale_subsystems()
        assert stale == ["vision"]


# ------------------------------- Diagnostics -------------------------------
class FakeMetrics:
    def __init__(self, mem=40.0, disk=60.0, temp=50.0):
        self._mem, self._disk, self._temp = mem, disk, temp

    def memory_percent(self):
        return self._mem

    def disk_free_percent(self, path="/"):
        return self._disk

    def cpu_temp_c(self):
        return self._temp


class FakeCamera:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


class FakeNotifier:
    def __init__(self, online):
        self._online = online

    def is_online(self):
        return self._online


class TestSelfDiagnostics:
    def _diag(self, **kw):
        cfg = Config()
        return SelfDiagnostics(cfg, metrics=FakeMetrics(**kw.pop("metrics", {})), **kw)

    def test_snapshot_collects_all(self):
        diag = SelfDiagnostics(
            Config(),
            camera=FakeCamera(True),
            ocr_ok_provider=lambda: True,
            notifier=FakeNotifier(True),
            last_plate_provider=lambda: PlateNumber("А123ВС777"),
            last_event_ts_provider=lambda: datetime.now(timezone.utc),
            metrics=FakeMetrics(mem=42, disk=55, temp=48),
        )
        s = diag.snapshot()
        assert s.camera_ok is True
        assert s.ocr_ok is True
        assert s.telegram_online is True
        assert s.memory_percent == 42
        assert s.disk_free_percent == 55
        assert s.cpu_temp_c == 48
        assert s.last_plate.value == "А123ВС777"

    def test_low_disk_warns(self, caplog):
        import logging
        diag = SelfDiagnostics(Config(), camera=FakeCamera(True),
                               metrics=FakeMetrics(disk=5.0))  # порог по умолчанию 10%
        with caplog.at_level(logging.WARNING, logger="gate_system.diagnostics"):
            diag.snapshot()
        assert any("мало места" in r.getMessage() for r in caplog.records)

    def test_overheat_warns(self, caplog):
        import logging
        diag = SelfDiagnostics(Config(), camera=FakeCamera(True),
                               metrics=FakeMetrics(temp=90.0))  # порог по умолчанию 75°C
        with caplog.at_level(logging.WARNING, logger="gate_system.diagnostics"):
            diag.snapshot()
        assert any("перегрев" in r.getMessage() for r in caplog.records)

    def test_camera_down_warns(self, caplog):
        import logging
        diag = SelfDiagnostics(Config(), camera=FakeCamera(False), metrics=FakeMetrics())
        with caplog.at_level(logging.WARNING, logger="gate_system.diagnostics"):
            s = diag.snapshot()
        assert s.camera_ok is False
        assert any("камера не отвечает" in r.getMessage() for r in caplog.records)

    def test_metric_error_falls_back(self):
        class BrokenMetrics:
            def memory_percent(self):
                raise RuntimeError("no psutil")
            def disk_free_percent(self, path="/"):
                raise RuntimeError("x")
            def cpu_temp_c(self):
                raise RuntimeError("x")
        diag = SelfDiagnostics(Config(), camera=FakeCamera(True), metrics=BrokenMetrics())
        s = diag.snapshot()   # не должно бросить
        assert s.memory_percent == 0.0
        assert s.cpu_temp_c is None
