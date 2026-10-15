"""Composition Root — сборка и запуск приложения.

Здесь (и только здесь) выбираются КОНКРЕТНЫЕ адаптеры и связывается весь граф
зависимостей: SQLite-хранилища, реле (GPIO или тестовый режим — по
relay.enabled), камеры, распознавание, Telegram, диагностика.

Приложение многопоточное. Каждый «сервис» (камера, Telegram, монитор сети,
watchdog, диагностика, прокси) — объект с методами run_forever() и stop();
GateApp запускает их в потоках, ловит сигналы ОС и корректно всё гасит.

Тяжёлые библиотеки (OpenCV, ultralytics, nomeroff, python-telegram-bot)
импортируются ЛЕНИВО внутри build_app(), поэтому модуль импортируется без них
(важно для тестов).
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional

from gate.config import Config
from gate.common.logger import setup_logging
from gate.paths import CONFIG_FILE, PROJECT_ROOT, resolve, resolve_source

log = logging.getLogger("gate_system.main")


# --------------------------------------------------------------------------- #
#  Обёртка Telegram-бота в «сервис» (у него блокирующий run(), а не run_forever)
# --------------------------------------------------------------------------- #
class TelegramService:
    """Запускает Telegram-бота в отдельном потоке и поднимает его после сбоев.

    notifier.run() блокирует поток до остановки. Если бот упал (сеть, прокси)
    или был перезапущен (смена прокси) — цикл поднимает его заново. stop()
    завершает окончательно.
    """

    def __init__(self, notifier, retry_delay_s: float = 5.0) -> None:
        self._notifier = notifier
        self._retry_delay_s = retry_delay_s
        self._stopped = False
        self._wake = threading.Event()

    def run_forever(self) -> None:
        while not self._stopped:
            try:
                self._notifier.run()   # блокирует до stop()/restart()
            except Exception:  # noqa: BLE001 - бот не должен «умирать» навсегда
                log.exception("Telegram-бот аварийно остановился")
            if self._stopped:
                break
            log.info("Перезапуск Telegram-бота через %.0f с…", self._retry_delay_s)
            self._wake.wait(self._retry_delay_s)
            self._wake.clear()

    def restart(self) -> None:
        """Перезапустить бота (подхватить новый прокси), не завершая сервис."""
        self._notifier.request_stop()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()
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

    :param startup_tasks: разовые фоновые задачи при старте (напр. загрузка моделей).
    :param shutdown_hooks: что освободить при остановке (GPIO, база данных).
    """

    def __init__(self, services: Dict[str, object], critical=None,
                 stop_grace_s: float = 5.0,
                 startup_tasks: Optional[Iterable[Callable[[], None]]] = None,
                 shutdown_hooks: Optional[Iterable[Callable[[], None]]] = None) -> None:
        self._services = services
        # Падение критичного сервиса гасит приложение (systemd перезапустит).
        # Падение вспомогательного (Telegram, диагностика) — только логируется.
        self._critical = set(critical) if critical is not None else set(services)
        self._threads: Dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self._stop_grace_s = stop_grace_s
        self._startup_tasks: List[Callable[[], None]] = list(startup_tasks or [])
        self._shutdown_hooks: List[Callable[[], None]] = list(shutdown_hooks or [])
        self._stopped = False
        self._stop_lock = threading.Lock()
        self.restart_requested = False

    def start(self) -> None:
        for task in self._startup_tasks:
            threading.Thread(target=self._run_task, args=(task,), name="startup", daemon=True).start()
        for name, svc in self._services.items():
            t = threading.Thread(target=self._run_service, args=(name, svc),
                                 name=name, daemon=True)
            t.start()
            self._threads[name] = t
        log.info("Запущены сервисы: %s", ", ".join(self._services))

    @staticmethod
    def _run_task(task: Callable[[], None]) -> None:
        try:
            task()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка фоновой задачи при старте")

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

    def request_restart(self, delay_s: float = 2.0) -> None:
        """Перезапустить процесс (после ответа в Telegram — через delay_s)."""
        self.restart_requested = True
        timer = threading.Timer(delay_s, self._stop.set)
        timer.daemon = True
        timer.start()

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self._stop.set()
        for name, svc in self._services.items():
            try:
                svc.stop()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка остановки сервиса '%s'", name)
        for name, t in self._threads.items():
            t.join(timeout=self._stop_grace_s)
        for hook in self._shutdown_hooks:
            try:
                hook()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка при освобождении ресурса")
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
    logging.shutdown()
    os._exit(1)


def build_app(cfg: Config) -> GateApp:
    """Связать все компоненты через порты и вернуть готовый к запуску GateApp."""
    from gate.common.events import event as user_event

    # --- Хранилище: SQLite (data/gate.db) ---
    from gate.storage.database import SqliteDatabase
    from gate.storage.whitelist import SqliteWhitelistRepository
    from gate.storage.history import PhotoStorage, SqliteHistoryRepository
    from gate.storage.admins import SqliteAdminRepository
    from gate.config import ConfigStore

    db = SqliteDatabase(resolve(cfg.paths.database))
    whitelist = SqliteWhitelistRepository(db)
    history = SqliteHistoryRepository(db, max_records=cfg.history_max_records)
    admins = SqliteAdminRepository(db)
    photos = PhotoStorage(str(resolve(cfg.paths.photos_dir)), max_files=cfg.photos_max_files)
    config_store = ConfigStore(CONFIG_FILE)

    # --- Ворота: реальное GPIO-реле или тестовый режим (relay.enabled) ---
    from gate.hardware.relay import build_relay
    from gate.hardware.gate_controller import RelayGateController

    relay = build_relay(cfg.relay.enabled, gpio_pin=cfg.relay.gpio_pin,
                        active_high=cfg.relay.active_high)
    gate = RelayGateController(relay, pulse_ms=cfg.relay.pulse_ms,
                               min_interval_s=cfg.relay.min_interval_s)

    # --- Зрение (тяжёлые зависимости) ---
    from gate.vision.camera import LatestFrameSource, RTSPCamera
    from gate.vision.detector import YoloDetector
    from gate.vision.ocr import build_ocr_engine
    from gate.vision.snapshot import SnapshotProvider

    camera_specs = cfg.effective_cameras()
    sources = [
        LatestFrameSource(
            RTSPCamera(resolve_source(spec.rtsp_url), spec.reconnect_min_delay_s,
                       spec.reconnect_max_delay_s, name=spec.name),
            stale_after_s=cfg.diagnostics.camera_stale_s,
        )
        for spec in camera_specs
    ]
    plate_model = str(resolve(cfg.detection.plate_model_path)) if cfg.detection.plate_model_path else ""
    detector = YoloDetector(plate_model, cfg.detection.plate_conf)
    adaptive = cfg.detection.adaptive
    if adaptive and not detector.has_plate_model:
        log.critical("Адаптивный режим требует модель рамки номера (%s) — её нет. "
                     "Работаю без адаптива (распознавание на каждом N-м кадре).", plate_model)
        user_event("⚠️ Нет модели рамки номера — экономичный режим отключён")
        adaptive = False
    ocr = build_ocr_engine(cfg)
    snapshot = SnapshotProvider([(spec.name, spec.role, src)
                                 for spec, src in zip(camera_specs, sources)])

    # --- Наблюдение ---
    from gate.services.watchdog import Watchdog
    from gate.services.diagnostics import SelfDiagnostics

    watchdog = Watchdog(timeout_s=max(120.0, cfg.diagnostics.interval_s * 2), on_stale=_on_stale)

    def last_plate():
        recent = history.recent(1)
        return recent[0].plate if recent else None

    diagnostics = SelfDiagnostics(
        cfg,
        cameras=[(spec.name, src) for spec, src in zip(camera_specs, sources)],
        ocr_ok_provider=getattr(ocr, "is_ready", lambda: True),
        ocr_state_provider=getattr(ocr, "state", lambda: "готов"),
        relay_state_provider=lambda: (getattr(relay, "is_real", True),
                                      getattr(relay, "description", "реле")),
        last_plate_provider=last_plate,
    )

    # --- Telegram ---
    from gate.bot.telegram_bot import CommandRouter, TelegramNotifier

    app_ref: Dict[str, GateApp] = {}
    router = CommandRouter(
        owner_id=cfg.telegram.owner_id,
        whitelist=whitelist,
        gate_open=gate.open,
        status_provider=diagnostics.snapshot,
        history_provider=history.recent,
        photo_provider=snapshot.snapshot,
        config_store=config_store,
        logs_path=str(resolve(cfg.paths.logs_dir) / "events.log"),  # понятный журнал событий
        admin_store=admins,
        restart_request=lambda: app_ref["app"].request_restart(),
    )

    # --- Прокси (опционально: автопокупка/ротация через proxy6) ---
    proxy_manager = None
    initial_proxy = cfg.telegram.proxy_url
    if cfg.proxy.enabled and cfg.proxy.api_key:
        from gate.services.proxy import Proxy6Client, ProxyManager
        proxy_manager = ProxyManager(
            Proxy6Client(cfg.proxy.api_key),
            version=cfg.proxy.version, country=cfg.proxy.country,
            period_days=cfg.proxy.period_days, buy_before_days=cfg.proxy.buy_before_days,
            max_buys_per_30d=cfg.proxy.max_buys_per_30d, cooldown_hours=cfg.proxy.cooldown_hours,
            descr=cfg.proxy.descr, state_path=str(resolve(cfg.proxy.state_path)),
        )
        try:
            initial_proxy = proxy_manager.ensure().url or cfg.telegram.proxy_url
        except Exception:  # noqa: BLE001
            log.exception("Не удалось получить прокси при старте — работаю без него/со старым")

    telegram_on = bool(cfg.telegram.enabled and cfg.telegram.token)
    notifier = TelegramNotifier(cfg.telegram.token if telegram_on else "", cfg.telegram.owner_id,
                                router, proxy_url=initial_proxy)
    diagnostics.set_notifier(notifier)

    # --- Монитор сети управляет статусом Telegram (авто-reconnect) ---
    from gate.services.network import NetworkMonitor

    network = NetworkMonitor(
        on_online=lambda: notifier.set_online(True),
        on_offline=lambda: notifier.set_online(False),
    )

    # --- Ядро (use cases) ---
    from gate.core.pipeline import AntiReplayGuard, AccessDecision, RecognitionPipeline

    guard = AntiReplayGuard(window_s=cfg.access.antireplay_window_s)
    decision = AccessDecision(whitelist, gate, history, notifier, guard,
                              photo_saver=lambda frame: photos.save(frame))

    # --- Координатор камер (пауза выезда во время заезда) ---
    from gate.core.coordinator import CameraCoordinator
    coordinator = CameraCoordinator()

    # --- По одному пайплайну на каждую камеру ---
    def make_aggregator():
        if cfg.access.confirm_frames > 0:
            from gate.core.aggregator import PlateAggregator
            return PlateAggregator(confirm=cfg.access.confirm_frames)
        return None

    def make_motion_gate(spec):
        if spec.require_motion:
            from gate.core.motion import MotionGate
            return MotionGate(threshold=cfg.detection.motion_threshold,
                              confirm_frames=cfg.detection.motion_confirm_frames,
                              memory_s=cfg.detection.motion_memory_s)
        return None

    services: Dict[str, object] = {}
    vision_names = set()
    for spec, src in zip(camera_specs, sources):
        vname = f"vision:{spec.name}"
        vision_names.add(vname)
        services[vname] = RecognitionPipeline(
            src, detector, ocr, decision,
            process_every_n=spec.process_every_n_frame,
            on_heartbeat=(lambda n=vname: watchdog.beat(n)),
            aggregator=make_aggregator(),
            motion_gate=make_motion_gate(spec),
            role=spec.role,
            coordinator=coordinator,
            adaptive=adaptive,
            idle_interval_s=cfg.detection.idle_interval_s,
            active_hold_s=cfg.detection.active_hold_s,
            name=spec.name,
        )
        log.info("Камера '%s' (роль=%s, контроль движения=%s): %s",
                 spec.name, spec.role, spec.require_motion,
                 RTSPCamera(resolve_source(spec.rtsp_url))._safe_source())

    services["network"] = network
    services["diagnostics"] = diagnostics
    services["watchdog"] = watchdog
    telegram_service = None
    if telegram_on:
        telegram_service = TelegramService(notifier, retry_delay_s=cfg.telegram.reconnect_delay_s)
        services["telegram"] = telegram_service
    else:
        log.warning("Telegram выключен или токен пуст — бот не запускается (управление недоступно)")

    # Сервис ротации прокси (если включён) — заранее покупает новый и перезапускает бота.
    if proxy_manager is not None:
        services["proxy"] = ProxyRotationService(
            proxy_manager, notifier, telegram_service, config_store,
            interval_hours=cfg.proxy.check_interval_hours, on_event=user_event,
        )

    def warmup_ocr() -> None:
        """Загрузить распознавание заранее (на Pi — до 1–2 минут), не блокируя запуск."""
        warm = getattr(ocr, "warmup", None)
        if not callable(warm):
            return
        log.info("Загрузка распознавания номеров… (на Raspberry Pi может занять 1–2 минуты)")
        if warm():
            user_event("✅ Распознавание номеров готово")

    # Критичны только Vision-циклы: без них система бессмысленна. Telegram, сеть и
    # диагностика — вспомогательные, их сбой не должен останавливать распознавание.
    app = GateApp(services, critical=vision_names,
                  startup_tasks=[warmup_ocr],
                  shutdown_hooks=[relay.close, db.close])
    app_ref["app"] = app
    return app


def main() -> None:
    cfg = Config.load()
    setup_logging(str(resolve(cfg.paths.logs_dir)), cfg.log_level, cfg.log_max_mb, cfg.log_backups)
    log.info("Запуск gate_system (реле=%s, telegram=%s)",
             "GPIO" if cfg.relay.enabled else "тестовый режим",
             "вкл" if (cfg.telegram.enabled and cfg.telegram.token) else "выкл")
    from gate.common.events import event as user_event
    user_event("▶️ Система запущена")
    app = build_app(cfg)
    app.run()
    if app.restart_requested:
        user_event("🔄 Система перезапускается")
        log.info("Перезапуск процесса…")
        logging.shutdown()
        time.sleep(0.5)
        # Перезапуск «на месте»: тот же PID (systemd это устраивает), свежий конфиг.
        os.execv(sys.executable, [sys.executable, str(PROJECT_ROOT / "main.py")])
    user_event("⏹️ Система остановлена")
    log.info("gate_system остановлен")
