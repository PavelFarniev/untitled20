"""Тесты реле (gate/hardware/relay.py) и контроллера ворот (gate_controller.py)."""
import logging

import pytest

from gate.hardware.relay import DryRunRelay, GpioRelay, RelayError, build_relay
from gate.hardware.gate_controller import RelayGateController
from gate.core.interfaces import Relay
from gate.core.models import OpenSource


class FakeOutputDevice:
    """Подмена gpiozero.OutputDevice: записывает последовательность on/off."""

    def __init__(self, pin, active_high):
        self.pin = pin
        self.active_high = active_high
        self.calls = []
        self.closed = False

    def on(self):
        self.calls.append("on")

    def off(self):
        self.calls.append("off")

    def close(self):
        self.closed = True


def _factory(store):
    def make(pin, active_high):
        dev = FakeOutputDevice(pin, active_high)
        store.append(dev)
        return dev
    return make


# ------------------------------- тестовый режим -------------------------------
class TestDryRunRelay:
    def test_does_not_touch_hardware_and_logs(self, caplog):
        with caplog.at_level(logging.INFO, logger="gate_system.relay"):
            DryRunRelay(sleeper=lambda s: None).pulse(500)
        assert any("НЕ замыкается" in r.getMessage() for r in caplog.records)

    def test_pulse_duration_passed_to_sleeper(self):
        slept = []
        DryRunRelay(sleeper=slept.append).pulse(500)
        assert slept == [0.5]

    def test_is_not_real(self):
        assert DryRunRelay().is_real is False


# ------------------------------- реальное GPIO -------------------------------
class TestGpioRelay:
    def test_pulse_closes_then_opens_contacts(self):
        devs = []
        slept = []
        relay = GpioRelay(17, active_high=True, device_factory=_factory(devs), sleeper=slept.append)
        relay.pulse(500)
        assert devs[0].pin == 17 and devs[0].active_high is True
        assert devs[0].calls == ["on", "off"]
        assert slept == [0.5]

    def test_low_trigger_passed_to_device(self):
        devs = []
        GpioRelay(27, active_high=False, device_factory=_factory(devs))
        assert devs[0].active_high is False

    def test_contacts_opened_even_if_sleep_fails(self):
        """Реле не должно остаться замкнутым при любой ошибке посреди импульса."""
        devs = []

        def boom(_):
            raise RuntimeError("interrupted")

        relay = GpioRelay(17, device_factory=_factory(devs), sleeper=boom)
        with pytest.raises(RuntimeError):
            relay.pulse(500)
        assert devs[0].calls == ["on", "off"]

    def test_init_failure_raises_relay_error(self):
        def broken(pin, active_high):
            raise OSError("not a Raspberry Pi")

        with pytest.raises(RelayError):
            GpioRelay(17, device_factory=broken)

    def test_close_releases_device(self):
        devs = []
        relay = GpioRelay(17, device_factory=_factory(devs))
        relay.close()
        assert devs[0].closed is True and devs[0].calls[-1] == "off"


# ------------------------------- фабрика по конфигу -------------------------------
class TestBuildRelay:
    def test_disabled_gives_dry_run(self):
        assert isinstance(build_relay(False, gpio_pin=17, active_high=True), DryRunRelay)

    def test_enabled_gives_gpio(self):
        devs = []
        relay = build_relay(True, gpio_pin=17, active_high=True, device_factory=_factory(devs))
        assert isinstance(relay, GpioRelay) and relay.is_real

    def test_enabled_but_gpio_broken_falls_back_loudly(self, caplog):
        """Реле включено, но GPIO недоступен: система не падает, но громко сообщает."""
        def broken(pin, active_high):
            raise OSError("no lgpio")

        with caplog.at_level(logging.CRITICAL, logger="gate_system.relay"):
            relay = build_relay(True, gpio_pin=17, active_high=True, device_factory=broken)
        assert isinstance(relay, DryRunRelay) and relay.is_real is False
        assert "ошибка GPIO" in relay.description
        assert any(r.levelno == logging.CRITICAL for r in caplog.records)


# ------------------------------- контроллер ворот -------------------------------
class SpyRelay(Relay):
    """Реле-шпион: фиксирует длительности импульсов."""

    def __init__(self):
        self.pulses = []

    def pulse(self, milliseconds):
        self.pulses.append(milliseconds)


class TestRelayGateController:
    def test_open_pulses_configured_duration(self):
        relay = SpyRelay()
        RelayGateController(relay, pulse_ms=500).open(OpenSource.AUTO)
        assert relay.pulses == [500]

    def test_open_custom_duration(self):
        relay = SpyRelay()
        RelayGateController(relay, pulse_ms=300).open(OpenSource.TELEGRAM)
        assert relay.pulses == [300]

    def test_open_logs_source(self, caplog):
        relay = SpyRelay()
        with caplog.at_level(logging.INFO, logger="gate_system.gate"):
            RelayGateController(relay, pulse_ms=500).open(OpenSource.MANUAL)
        assert any("manual" in rec.getMessage() for rec in caplog.records)

    def test_open_propagates_relay_error(self):
        class BoomRelay(Relay):
            def pulse(self, milliseconds):
                raise OSError("GPIO busy")

        with pytest.raises(OSError):
            RelayGateController(BoomRelay(), pulse_ms=500).open(OpenSource.AUTO)

    def test_prints_gate_opened_text(self, capsys):
        """Текст об открытии ворот печатается при любом режиме реле."""
        RelayGateController(SpyRelay(), pulse_ms=500).open(OpenSource.AUTO)
        out = capsys.readouterr().out
        assert "Gate opened" in out and "Ворота открыты" in out

    def test_dry_run_text_marks_test_mode(self, capsys):
        RelayGateController(DryRunRelay(sleeper=lambda s: None), 500).open(OpenSource.AUTO)
        assert "тестовый режим" in capsys.readouterr().out

    def test_repeat_within_interval_is_ignored(self):
        """Второй импульс сразу после первого остановил бы ворота (Nice SbS) — игнор."""
        clock = [100.0]
        relay = SpyRelay()
        gate = RelayGateController(relay, 500, min_interval_s=3.0, clock=lambda: clock[0])
        assert gate.open(OpenSource.AUTO) is True
        clock[0] = 101.0
        assert gate.open(OpenSource.TELEGRAM) is False
        clock[0] = 104.0
        assert gate.open(OpenSource.TELEGRAM) is True
        assert relay.pulses == [500, 500]
