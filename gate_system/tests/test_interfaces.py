"""Юнит-тесты портов (interfaces.py).

Проверяем, что абстракции нельзя инстанцировать напрямую, а корректная
конкретная реализация — можно. Это гарантия того, что контракты обязывают
адаптеры реализовывать все методы.
"""
import pytest

from interfaces import (
    Detector,
    FrameSource,
    GateController,
    HealthProvider,
    HistoryRepository,
    Notifier,
    OcrEngine,
    Relay,
    WhitelistRepository,
)
from models import HealthStatus, OpenSource, PlateNumber


ALL_PORTS = [
    FrameSource, Detector, OcrEngine, Relay, GateController,
    WhitelistRepository, HistoryRepository, Notifier, HealthProvider,
]


@pytest.mark.parametrize("port", ALL_PORTS)
def test_abstract_port_cannot_be_instantiated(port):
    with pytest.raises(TypeError):
        port()  # у абстрактных классов есть нереализованные методы


def test_incomplete_implementation_still_abstract():
    # Реализованы не все методы -> остаётся абстрактным.
    class HalfWhitelist(WhitelistRepository):
        def is_allowed(self, plate):
            return True
        # list_all/add/remove не реализованы

    with pytest.raises(TypeError):
        HalfWhitelist()


def test_complete_implementation_instantiable():
    class MemRelay(Relay):
        def __init__(self):
            self.pulses = []
        def pulse(self, milliseconds):
            self.pulses.append(milliseconds)

    class MemGate(GateController):
        def __init__(self, relay):
            self.relay = relay
        def open(self, source: OpenSource):
            self.relay.pulse(500)

    relay = MemRelay()
    gate = MemGate(relay)
    gate.open(OpenSource.MANUAL)
    assert relay.pulses == [500]


def test_health_provider_contract():
    class FakeHealth(HealthProvider):
        def snapshot(self) -> HealthStatus:
            return HealthStatus(camera_ok=True)

    assert FakeHealth().snapshot().camera_ok is True
