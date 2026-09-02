"""Тесты реле (relay.py) и контроллера ворот (gate_controller.py)."""
import pytest

from relay import StubRelay, GpioRelay, UsbRelay, build_relay
from gate_controller import RelayGateController
from interfaces import Relay
from models import OpenSource


class TestStubRelay:
    def test_prints_gate_opened(self, capsys):
        StubRelay(sleeper=lambda s: None).pulse(500)
        assert capsys.readouterr().out.strip() == "Gate opened"

    def test_pulse_duration_passed_to_sleeper(self):
        slept = []
        StubRelay(sleeper=slept.append).pulse(500)
        # 500 мс -> 0.5 c сна (замыкание контактов на ~500 мс).
        assert slept == [0.5]


class TestBuildRelay:
    def test_build_stub(self):
        assert isinstance(build_relay("stub", gpio_pin=17, usb_device="", active_high=True), StubRelay)

    def test_build_stub_case_insensitive(self):
        assert isinstance(build_relay("STUB", gpio_pin=17, usb_device="", active_high=True), StubRelay)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError):
            build_relay("laser", gpio_pin=17, usb_device="", active_high=True)

    def test_gpio_not_available_on_dev(self):
        # На macOS/в песочнице аппаратные бэкенды ещё не реализованы (этап 12).
        with pytest.raises(NotImplementedError):
            build_relay("gpio", gpio_pin=17, usb_device="", active_high=True)

    def test_usb_not_available_on_dev(self):
        with pytest.raises(NotImplementedError):
            build_relay("usb", gpio_pin=0, usb_device="/dev/ttyUSB0", active_high=True)


class SpyRelay(Relay):
    """Реле-шпион: фиксирует длительности импульсов."""

    def __init__(self):
        self.pulses = []

    def pulse(self, milliseconds):
        self.pulses.append(milliseconds)


class TestRelayGateController:
    def test_open_pulses_configured_duration(self):
        relay = SpyRelay()
        gate = RelayGateController(relay, pulse_ms=500)
        gate.open(OpenSource.AUTO)
        assert relay.pulses == [500]

    def test_open_custom_duration(self):
        relay = SpyRelay()
        RelayGateController(relay, pulse_ms=300).open(OpenSource.TELEGRAM)
        assert relay.pulses == [300]

    def test_open_logs_source(self, caplog):
        import logging
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


class TestStubIntegration:
    def test_full_stub_open(self, capsys):
        relay = StubRelay(sleeper=lambda s: None)
        RelayGateController(relay, pulse_ms=500).open(OpenSource.AUTO)
        assert "Gate opened" in capsys.readouterr().out
