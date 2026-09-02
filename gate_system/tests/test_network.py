"""Тесты NetworkMonitor: переходы онлайн/оффлайн и колбэки."""
from network import NetworkMonitor


class TestNetworkMonitor:
    def test_transition_to_online_fires_callback(self):
        events = []
        mon = NetworkMonitor(on_online=lambda: events.append("on"), probe=lambda: True)
        assert mon.is_online is False
        mon.check_once()
        assert mon.is_online is True
        assert events == ["on"]

    def test_no_duplicate_callbacks_while_stable(self):
        events = []
        mon = NetworkMonitor(on_online=lambda: events.append("on"), probe=lambda: True)
        mon.check_once()
        mon.check_once()   # состояние не изменилось — колбэк не повторяется
        assert events == ["on"]

    def test_offline_transition_fires_callback(self):
        states = iter([True, False])
        events = []
        mon = NetworkMonitor(
            on_online=lambda: events.append("on"),
            on_offline=lambda: events.append("off"),
            probe=lambda: next(states),
        )
        mon.check_once()   # -> online
        mon.check_once()   # -> offline
        assert events == ["on", "off"]

    def test_recovery_after_loss(self):
        states = iter([True, False, True])
        events = []
        mon = NetworkMonitor(
            on_online=lambda: events.append("on"),
            on_offline=lambda: events.append("off"),
            probe=lambda: next(states),
        )
        for _ in range(3):
            mon.check_once()
        assert events == ["on", "off", "on"]   # восстановление после потери

    def test_callback_exception_does_not_propagate(self):
        def boom():
            raise RuntimeError("x")
        mon = NetworkMonitor(on_online=boom, probe=lambda: True)
        mon.check_once()   # не должно бросить
        assert mon.is_online is True
