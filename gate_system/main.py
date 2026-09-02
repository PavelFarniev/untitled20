"""Composition Root — сборка и запуск приложения.

Здесь (и только здесь) выбираются КОНКРЕТНЫЕ адаптеры и связывается весь граф
зависимостей. Чтобы перенести систему на Raspberry Pi, достаточно поменять
backend реле в config.json (stub -> gpio/usb) — код ниже не меняется.

Приложение многопоточное. Каждый «сервис» (Vision-цикл, Telegram, монитор сети,
watchdog, диагностика) — это объект с методами run_forever() и stop(); GateApp
запускает их в потоках, ловит сигналы ОС и корректно всё гасит.

Тяжёлые библиотеки (OpenCV, ultralytics, easyocr, python-telegram-bot)
импортируются ЛЕНИВО внутри build_app(), поэтому сам модуль main импортируется
без них (важно для окружений без ML-стека).
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from typing import Dict

from config import Config
from logger import setup_logging

log = logging.getLogger("gate_system.main")


# --------------------------------------------------------------------------- #
#  Обёртка Telegram-бота в «сервис» (у него асинхронный run(), а не run_forever)
# --------------------------------------------------------------------------- #
class TelegramService:
    """Запускает асинхронного Telegram-бота в отдельном потоке.

    Умеет перезапускаться (например, при смене прокси): notifier.run() блокирует
    поток; restart() останавливает polling — и цикл поднимает бота заново уже с
    новым прокси. stop() завершает окончательно.
    """

    def __init__(self, notifier) -> None:
        self._notifier = notifier
        self._stopped = False

    def run_forever(self) -> None:
        while not self._stopped:
            self._notifier.run()   # блокирует до stop()/restart()
            if self._stopped:
                break
            log.info("Перезапуск Telegram-бота…")
            time.sleep(2)

    def restart(self) -> None:
        """Перезапустить бота (подхватить новый прокси), не завершая сервис."""
        self._notifier.request_stop()

    def stop(self) -> None:
        self._stopped = True
        self._notifier.request_stop()


# --------------------------------------------------------------------------- #
#  Сервис автопокупки/ротации прокси (proxy6)
# --------------------------------------------------------------------------- #
class ProxyRotationService:
    """Периодически проверяет прокси, заранее покупает новый и перезапускает бота."""

    def __init__(self, manager, notifier, telegram_service, config_store,
                 interval_hours: float, on_event) -> None:
        self._manager = manager
        self._notifier = notifier
        self._telegram = telegram_service
        self._config_store = config_store
        self._interval_s = max(60.0, interval_hours * 3600)
        self._on_event = on_event
        self._current_url = None
        self._stop = threading.Event()

    def run_forever(self) -> None:
        log.info("Сервис ротации прокси запущен")
        while not self._stop.is_set():
            try:
                decision = self._manager.ensure()
                if decision.url and decision.url != self._current_url:
                    self._apply(decision)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка ротации прокси")
            self._stop.wait(self._interval_s)

    def _apply(self, decision) -> None:
        first = self._current_url is None
        self._current_url = decision.url
        self._notifier.set_proxy(decision.url)
        try:
            self._config_store.set("telegram.proxy_url", decision.url)
        except Exception:  # noqa: BLE001
            log.debug("Не удалось сохранить прокси в конфиг", exc_info=True)
        if decision.purchased and self._on_event:
            self._on_event(f"🌐 Куплен новый прокси (действует до {decision.expires})")
        # При смене прокси (не на старте) перезапускаем бота, чтобы применить.
        if not first and self._telegram is not None:
            self._telegram.restart()

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
#  Менеджер жизненного цикла: запуск сервисов в потоках + graceful shutdown
# --------------------------------------------------------------------------- #
class GateApp:
    """Запускает набор сервисов в потоках и управляет их остановкой.

    Сервис — любой объект с методами run_forever() и stop().
    """

    def __init__(self, services: Dict[str, object], critical=None,
                 stop_grace_s: float = 5.0) -> None:
        self._services = services
        # Падение критичного сервиса гасит приложение (systemd перезапустит).
        # Падение вспомогательного (Telegram, диагностика) — только логируется.
        self._critical = set(critical) if critical is not None else set(services)
        self._threads: Dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self._stop_grace_s = stop_grace_s

    def start(self) -> None:
        for name, svc in self._services.items():
            t = threading.Thread(target=self._run_service, args=(name, svc),
                                  name=name, daemon=True)
            t.start()
            self._threads[name] = t
        log.info("Запущены сервисы: %s", ", ".join(self._services))

    def _run_service(self, name: str, svc: object) -> None:
        log.info("Сервис '%s' запущен", name)
        try:
            svc.run_forever()
        except Exception:  # noqa: BLE001
            log.exception("Сервис '%s' аварийно завершился", name)
        finally:
            log.info("Сервис '%s' остановлен", name)
            if self._stop.is_set():
                return
            # Критичный сервис завершился сам -> гасим приложение (перезапустит systemd).
            # Вспомогательный -> продолжаем работать без него.
            if name in self._critical:
                log.error("Критичный сервис '%s' завершился неожиданно — останов приложения", name)
                self._stop.set()
            else:
                log.warning("Вспомогательный сервис '%s' остановился — продолжаем без него", name)

    def stop(self) -> None:
        if not self._stop.is_set():
            self._stop.set()
        for name, svc in self._services.items():
            try:
                svc.stop()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка остановки сервиса '%s'", name)
        for name, t in self._threads.items():
            t.join(timeout=self._stop_grace_s)
        log.info("Все сервисы остановлены")

    def run(self) -> None:
        """Установить обработчики сигналов, запустить сервисы и ждать останова."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self._stop.set())
            except ValueError:  # не главный поток — пропускаем (напр. в тестах)
                pass
        self.start()
        try:
            while not self._stop.wait(0.5):
                pass
        finally:
            self.stop()


# --------------------------------------------------------------------------- #
#  Сборка реального приложения из конфигурации
# --------------------------------------------------------------------------- #
def _on_stale(name: str) -> None:  # pragma: no cover - аварийный путь
    """Реакция watchdog на зависшую подсистему: перезапуск процесса через systemd."""
    log.critical("Подсистема '%s' зависла — перезапуск процесса", name)
    os._exit(1)


def build_app(cfg: Config) -> GateApp:
    """Связать все компоненты через порты и вернуть готовый к запуску GateApp."""
    # --- Хранилища (JSON; замена на SQLite — сменой класса здесь) ---
    from whitelist import JsonWhitelistRepository
    from history import HistoryStore, PhotoStorage
    from admins import AdminStore
    from config import ConfigStore

    whitelist = JsonWhitelistRepository(cfg.paths.allowed)
    history = HistoryStore(cfg.paths.history_db)
    photos = PhotoStorage(cfg.paths.photos_dir, max_files=cfg.photos_max_files)
    admins = AdminStore(cfg.paths.admins)
    config_store = ConfigStore("config/config.json")

    # --- Управление воротами (Stub на macOS; GPIO/USB на RPi — по конфигу) ---
    from relay import build_relay
    from gate_controller import RelayGateController

    relay = build_relay(cfg.relay.backend, gpio_pin=cfg.relay.gpio_pin,
                        usb_device=cfg.relay.usb_device, active_high=cfg.relay.active_high)
    gate = RelayGateController(relay, pulse_ms=cfg.relay.pulse_ms)

    # --- Зрение (тяжёлые зависимости) ---
    from camera import RTSPCamera
    from detector import YoloDetector
    from ocr import build_ocr_engine

    camera_specs = cfg.effective_cameras()
    cameras = [RTSPCamera(spec.rtsp_url, spec.reconnect_min_delay_s, spec.reconnect_max_delay_s)
               for spec in camera_specs]
    detector = YoloDetector(cfg.detection.model_path, cfg.detection.plate_model_path,
                            cfg.detection.vehicle_conf, cfg.detection.plate_conf)
    ocr = build_ocr_engine(cfg)

    # --- Наблюдение ---
    from watchdog import Watchdog
    from diagnostics import SelfDiagnostics

    watchdog = Watchdog(timeout_s=max(30.0, cfg.diagnostics.interval_s * 2), on_stale=_on_stale)

    def last_plate():
        recent = history.recent(1)
        return recent[0].plate if recent else None

    diagnostics = SelfDiagnostics(cfg, camera=cameras[0] if cameras else None,
                                  ocr_ok_provider=lambda: True,
                                  last_plate_provider=last_plate)

    # --- Telegram ---
    from telegram_bot import CommandRouter, TelegramNotifier

    router = CommandRouter(
        owner_id=cfg.telegram.owner_id,
        whitelist=whitelist,
        gate_open=gate.open,
        status_provider=diagnostics.snapshot,
        history_provider=history.recent,
        photo_provider=lambda: None,  # «текущий кадр» подключается на этапе доработки /photo
        config_store=config_store,
        logs_path=f"{cfg.paths.logs_dir}/events.log",  # понятный журнал событий
        admin_store=admins,
    )

    # --- Прокси (опционально: автопокупка/ротация через proxy6) ---
    proxy_manager = None
    initial_proxy = cfg.telegram.proxy_url
    if cfg.proxy.enabled and cfg.proxy.api_key:
        from proxy import Proxy6Client, ProxyManager
        proxy_manager = ProxyManager(
            Proxy6Client(cfg.proxy.api_key),
            version=cfg.proxy.version, country=cfg.proxy.country,
            period_days=cfg.proxy.period_days, buy_before_days=cfg.proxy.buy_before_days,
            max_buys_per_30d=cfg.proxy.max_buys_per_30d, cooldown_hours=cfg.proxy.cooldown_hours,
            descr=cfg.proxy.descr, state_path=cfg.proxy.state_path,
        )
        try:
            initial_proxy = proxy_manager.ensure().url or cfg.telegram.proxy_url
        except Exception:  # noqa: BLE001
            log.exception("Не удалось получить прокси при старте — работаю без него/со старым")

    notifier = TelegramNotifier(cfg.telegram.token, cfg.telegram.owner_id, router,
                                proxy_url=initial_proxy)

    # --- Монитор сети управляет статусом Telegram (авто-reconnect) ---
    from network import NetworkMonitor

    network = NetworkMonitor(
        on_online=lambda: notifier.set_online(True),
        on_offline=lambda: notifier.set_online(False),
    )

    # --- Ядро (use cases) ---
    from pipeline import AntiReplayGuard, AccessDecision, RecognitionPipeline

    guard = AntiReplayGuard(window_s=cfg.access.antireplay_window_s)
    decision = AccessDecision(whitelist, gate, history, notifier, guard,
                              photo_saver=lambda frame: photos.save(frame))

    # --- Координатор камер (пауза выезда во время заезда) ---
    from coordinator import CameraCoordinator
    coordinator = CameraCoordinator()

    # --- По одному Vision-пайплайну на каждую камеру ---
    def make_aggregator():
        if cfg.access.confirm_frames > 0:
            from aggregator import PlateAggregator
            return PlateAggregator(confirm=cfg.access.confirm_frames)
        return None

    def make_motion_gate(spec):
        if spec.require_motion:
            from motion import MotionGate
            return MotionGate(threshold=cfg.detection.motion_threshold,
                              confirm_frames=cfg.detection.motion_confirm_frames)
        return None

    services: Dict[str, object] = {}
    vision_names = set()
    for spec, cam in zip(camera_specs, cameras):
        vname = f"vision:{spec.name}"
        vision_names.add(vname)
        pipe = RecognitionPipeline(
            cam, detector, ocr, decision,
            process_every_n=spec.process_every_n_frame,
            on_heartbeat=(lambda n=vname: watchdog.beat(n)),
            require_vehicle=cfg.detection.require_vehicle,
            aggregator=make_aggregator(),
            motion_gate=make_motion_gate(spec),
            role=spec.role,
            coordinator=coordinator,
            adaptive=cfg.detection.adaptive,
            idle_every_n=cfg.detection.idle_process_every_n,
            active_hold_s=cfg.detection.active_hold_s,
        )
        services[vname] = pipe
        log.info("Камера '%s' (роль=%s, движение=%s): %s",
                 spec.name, spec.role, spec.require_motion, spec.rtsp_url)

    services["network"] = network
    services["diagnostics"] = diagnostics
    services["watchdog"] = watchdog
    telegram_service = None
    if cfg.telegram.token:
        telegram_service = TelegramService(notifier)
        services["telegram"] = telegram_service
    else:
        log.warning("Токен Telegram пуст — бот не запускается (управление недоступно)")

    # Сервис ротации прокси (если включён) — заранее покупает новый и перезапускает бота.
    if proxy_manager is not None:
        from events import event as user_event
        services["proxy"] = ProxyRotationService(
            proxy_manager, notifier, telegram_service, config_store,
            interval_hours=cfg.proxy.check_interval_hours, on_event=user_event,
        )

    # Критичны только Vision-циклы: без них система бессмысленна. Telegram, сеть и
    # диагностика — вспомогательные, их сбой не должен останавливать распознавание.
    return GateApp(services, critical=vision_names)


def main() -> None:
    cfg = Config.load()
    setup_logging(cfg.paths.logs_dir, cfg.log_level, cfg.log_max_mb, cfg.log_backups)
    log.info("Запуск gate_system (реле=%s, telegram=%s)",
             cfg.relay.backend, "вкл" if cfg.telegram.token else "выкл")
    from events import event as user_event
    user_event("▶️ Система запущена")
    app = build_app(cfg)
    app.run()
    user_event("⏹️ Система остановлена")
    log.info("gate_system остановлен")


if __name__ == "__main__":
    main()
