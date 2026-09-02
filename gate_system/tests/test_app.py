"""Тесты жизненного цикла GateApp (main.py) на фейковых сервисах."""
import threading
import time

from main import GateApp


class FakeService:
    """Сервис, который блокируется в run_forever до вызова stop()."""

    def __init__(self):
        self.started = threading.Event()
        self.stopped = threading.Event()
        self._stop = threading.Event()

    def run_forever(self):
        self.started.set()
        self._stop.wait()
        self.stopped.set()

    def stop(self):
        self._stop.set()


class SelfExitingService:
    """Сервис, который сам завершает run_forever почти сразу (имитация падения)."""

    def __init__(self):
        self.stop_called = False

    def run_forever(self):
        return  # завершился сам

    def stop(self):
        self.stop_called = True


class TestGateAppLifecycle:
    def test_start_and_stop(self):
        s1, s2 = FakeService(), FakeService()
        app = GateApp({"a": s1, "b": s2})
        app.start()
        assert s1.started.wait(1) and s2.started.wait(1)
        app.stop()
        assert s1.stopped.wait(1) and s2.stopped.wait(1)

    def test_run_returns_after_stop_signal(self):
        s = FakeService()
        app = GateApp({"svc": s})

        def run_in_thread():
            app.run()   # блокируется до app._stop

        t = threading.Thread(target=run_in_thread, daemon=True)
        t.start()
        assert s.started.wait(1)
        app._stop.set()          # имитируем сигнал/останов
        t.join(timeout=2)
        assert not t.is_alive()  # run() корректно завершился
        assert s.stopped.is_set()

    def test_critical_service_exit_triggers_shutdown(self):
        # Падение критичного сервиса гасит приложение.
        dying = SelfExitingService()
        alive = FakeService()
        app = GateApp({"dying": dying, "alive": alive}, critical={"dying"})
        app.start()
        assert app._stop.wait(1) is True
        app.stop()
        assert alive.stopped.wait(1)

    def test_auxiliary_service_exit_does_not_shutdown(self):
        # Падение вспомогательного сервиса НЕ гасит приложение (Vision продолжает).
        dying = SelfExitingService()
        alive = FakeService()
        app = GateApp({"vision": alive, "telegram": dying}, critical={"vision"})
        app.start()
        assert alive.started.wait(1)
        # Даём время потоку telegram завершиться и обработаться в finally.
        time.sleep(0.2)
        assert app._stop.is_set() is False   # приложение живо
        app.stop()
        assert alive.stopped.wait(1)

    def test_stop_is_idempotent(self):
        s = FakeService()
        app = GateApp({"svc": s})
        app.start()
        s.started.wait(1)
        app.stop()
        app.stop()   # повторный вызов не должен падать
        assert s.stopped.is_set()


class TestTelegramService:
    def test_stop_delegates_to_notifier(self):
        from main import TelegramService

        class FakeNotifier:
            def __init__(self):
                self.stopped = False
            def request_stop(self):
                self.stopped = True

        n = FakeNotifier()
        TelegramService(n).stop()
        assert n.stopped is True
