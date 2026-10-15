"""Прикладная логика (use cases) — сердце системы.

Зависит ТОЛЬКО от портов (interfaces.py) и домена (models.py). Не знает ни о
камере как о RTSP, ни о реле как о GPIO, ни о Telegram. Это делает ядро
полностью переносимым и тестируемым.

Содержит:
* AntiReplayGuard  — защита от повторного открытия (окно N секунд);
* AccessDecision   — решение о доступе и оркестрация побочных эффектов;
* RecognitionPipeline — главный цикл захвата → детекции → OCR → решения.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Dict, Optional

from gate.core.interfaces import (
    Detector,
    FrameSource,
    GateController,
    HistoryRepository,
    Notifier,
    OcrEngine,
    WhitelistRepository,
)
from gate.core.models import (
    AccessResult,
    CheckResult,
    Event,
    OpenSource,
    PlateNumber,
)
from gate.common.events import event as user_event

log = logging.getLogger("gate_system.pipeline")


class AntiReplayGuard:
    """Не открывать ворота одному и тому же номеру чаще, чем раз в `window_s`."""

    def __init__(self, window_s: int = 30) -> None:
        self._window_s = window_s
        self._last_open: Dict[str, float] = {}

    def allow(self, plate: PlateNumber, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.monotonic()
        last = self._last_open.get(plate.value)
        if last is not None and (now - last) < self._window_s:
            return False
        return True

    def mark_opened(self, plate: PlateNumber, now: Optional[float] = None) -> None:
        self._last_open[plate.value] = now if now is not None else time.monotonic()


class AccessDecision:
    """Принимает распознанный номер и выполняет всю бизнес-логику ТЗ."""

    def __init__(
        self,
        whitelist: WhitelistRepository,
        gate: GateController,
        history: HistoryRepository,
        notifier: Notifier,
        guard: AntiReplayGuard,
        photo_saver=None,  # callable(frame) -> str путь к сохранённому фото
    ) -> None:
        self._whitelist = whitelist
        self._gate = gate
        self._history = history
        self._notifier = notifier
        self._guard = guard
        # Отдельный дедуп для НЕИЗВЕСТНЫХ номеров, чтобы отметка отказа не блокировала
        # последующее открытие того же номера, если его только что добавили в список.
        self._denied_guard = AntiReplayGuard(window_s=getattr(guard, "_window_s", 30))
        self._save_photo = photo_saver or (lambda frame: None)

    def handle(self, plate: PlateNumber, frame=None) -> AccessResult:
        """Обработать распознанный номер: проверить, открыть, залогировать, уведомить."""
        allowed = self._whitelist.is_allowed(plate)
        result = CheckResult.GRANTED if allowed else CheckResult.DENIED
        # Разрешённые и неизвестные номера дедуплицируются РАЗДЕЛЬНО: отметка отказа
        # для чужой машины не должна мешать открытию, если её только что добавили в
        # белый список (иначе новая своя машина «залипает» на всё окно).
        guard = self._guard if allowed else self._denied_guard

        # Дедупликация: тот же номер, обработанный недавно, пропускаем целиком — ни
        # ворот, ни фото, ни уведомления (иначе одна машина = десятки фото в секунду).
        if not guard.allow(plate):
            log.info("Повтор подавлен (anti-replay): %s", plate)
            evt = Event.now(plate, result, gate_opened=False, note="suppressed")
            return AccessResult(result, False, evt, suppressed_by_antireplay=True)
        guard.mark_opened(plate)

        # Фото сохраняем ТОЛЬКО для реально обрабатываемого события (не для повторов).
        photo_path = self._safe_save_photo(frame)

        if allowed:
            self._gate.open(OpenSource.AUTO)
            evt = Event.now(plate, CheckResult.GRANTED, photo_path=photo_path,
                            gate_opened=True, source=OpenSource.AUTO)
            self._history.append(evt)
            user_event(f"🚗 Ворота открыты — {plate}")
            self._safe_notify(self._notifier.notify_known, evt)
            return AccessResult(CheckResult.GRANTED, True, evt)

        # Номер неизвестен: ворота НЕ открывать, сохранить фото, лог, уведомить владельца.
        evt = Event.now(plate, CheckResult.DENIED, photo_path=photo_path, gate_opened=False)
        self._history.append(evt)
        user_event(f"🚫 Неизвестная машина — {plate}")
        self._safe_notify(self._notifier.notify_unknown, evt)
        return AccessResult(CheckResult.DENIED, False, evt)

    def _safe_save_photo(self, frame) -> Optional[str]:
        try:
            return self._save_photo(frame) if frame is not None else None
        except Exception:  # noqa: BLE001
            log.exception("Не удалось сохранить фото события")
            return None

    def _safe_notify(self, fn, event: Event) -> None:
        """Уведомления не должны ронять ядро при отсутствии интернета."""
        try:
            fn(event)
        except Exception:  # noqa: BLE001
            log.warning("Уведомление не доставлено (возможно, нет сети)", exc_info=True)


class RecognitionPipeline:
    """Главный рабочий цикл одной камеры: кадр → номер → текст → решение.

    Прореживание кадров:
    * обычный (и активный) режим — обрабатывается не чаще каждого
      `process_every_n`-го кадра камеры (номер кадра берётся у источника
      `frame_seq`, поэтому кадры, пропущенные пока шло распознавание, тоже
      засчитываются);
    * адаптивный простой — лёгкая проверка «есть ли номер» не чаще раза в
      `idle_interval_s` секунд.
    """

    def __init__(
        self,
        source: FrameSource,
        detector: Detector,
        ocr: OcrEngine,
        decision: AccessDecision,
        process_every_n: int = 1,
        on_heartbeat: Optional[Callable[[], None]] = None,
        aggregator=None,
        motion_gate=None,
        role: str = "entry",
        coordinator=None,
        adaptive: bool = False,
        idle_interval_s: float = 1.0,
        active_hold_s: float = 5.0,
        name: str = "camera",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._detector = detector
        self._ocr = ocr
        self._decision = decision
        self._process_every_n = max(1, process_every_n)
        self._on_heartbeat = on_heartbeat
        # Опциональная сборка номера по нескольким кадрам (голосование).
        self._aggregator = aggregator
        # Опциональный контроль движения (выездная камера: игнорировать стоящих).
        self._motion_gate = motion_gate
        # Роль камеры (entry/exit) и координатор для межкамерной паузы.
        self._role = role
        self._coordinator = coordinator
        self.name = name
        self._clock = clock
        # Адаптивный режим: в простое только лёгкая детекция рамки раз в
        # idle_interval_s секунд; при появлении номера (на выезде — движущегося) —
        # частая обработка с полным распознаванием, затем возврат в простой через
        # active_hold_s секунд.
        self._adaptive = adaptive
        self._idle_interval_s = max(0.0, idle_interval_s)
        self._active_hold_s = max(0.0, active_hold_s)
        self._mode = "idle"          # "idle" | "active"
        self._active_until = 0.0     # монотонное время, до которого держим активный режим
        self._running = False
        self._frame_i = 0            # счётчик кадров для источников без frame_seq
        self._last_pos = 0           # номер кадра последней обработки
        self._last_processed_at = float("-inf")
        self._no_frames_reported = False

    @property
    def mode(self) -> str:
        return self._mode if self._adaptive else "постоянный"

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------ прореживание
    def _current_stride(self) -> int:
        """Шаг прореживания в кадрах для частой обработки."""
        return self._process_every_n

    def _should_process(self, pos: int) -> bool:
        if self._adaptive and self._mode == "idle":
            return (self._clock() - self._last_processed_at) >= self._idle_interval_s
        return (pos - self._last_pos) >= self._current_stride()

    def _frame_position(self) -> int:
        seq = getattr(self._source, "frame_seq", None)
        if isinstance(seq, int) and seq > 0:
            return seq
        self._frame_i += 1
        return self._frame_i

    # ------------------------------------------------------------ главный цикл
    def run_forever(self) -> None:
        """Бесконечный цикл обработки с авто-переподключением камеры."""
        self._running = True
        self._source.open()
        log.info("Камера '%s': пайплайн распознавания запущен (роль=%s, адаптивный=%s)",
                 self.name, self._role, self._adaptive)
        while self._running:
            try:
                if self._on_heartbeat is not None:
                    self._on_heartbeat()  # сигнал «жив» для watchdog
                frame = self._source.read()
                if frame is None:
                    if not self._no_frames_reported:
                        log.warning("Камера '%s': кадры не поступают — жду переподключения", self.name)
                        self._no_frames_reported = True
                    self._source.reconnect()
                    continue
                if self._no_frames_reported:
                    log.info("Камера '%s': кадры снова поступают", self.name)
                    self._no_frames_reported = False
                pos = self._frame_position()
                if not self._should_process(pos):
                    continue
                self._last_pos = pos
                self._last_processed_at = self._clock()
                self._process_frame(frame)
            except Exception:  # noqa: BLE001 - цикл не должен падать 24/7
                log.exception("Камера '%s': ошибка в цикле распознавания; продолжаем", self.name)
                time.sleep(0.5)
        self._source.close()
        log.info("Камера '%s': пайплайн распознавания остановлен", self.name)

    def _process_frame(self, frame) -> None:
        """Обработать кадр: найти номер(а) и распознать.

        Адаптивный режим — см. _process_adaptive. Постоянный режим (adaptive=false
        или нет модели рамки): если рамки найдены — распознаём каждую крупным планом,
        иначе отдаём распознавателю весь кадр (Nomeroff сам ищет номер).
        """
        if self._adaptive:
            self._process_adaptive(frame)
            return

        plate_boxes = self._detector.detect_plate(frame)
        self._observe_motion(plate_boxes, frame)
        if plate_boxes:
            for pb in plate_boxes:
                self._ocr_and_decide(_crop_padded(frame, pb, 0.30), frame)
        else:
            self._ocr_and_decide(frame, frame)

    def _observe_motion(self, plate_boxes, frame) -> None:
        if self._motion_gate is None:
            return
        try:
            h, w = frame.shape[:2]
            self._motion_gate.observe(plate_boxes, w, h)
        except Exception:  # noqa: BLE001
            pass

    def _process_adaptive(self, frame) -> None:
        """Адаптивная обработка кадра.

        В простое запускается ТОЛЬКО лёгкий детектор рамки (best.pt). Пока рамки
        нет, тяжёлый распознаватель (Nomeroff) не трогается вовсе.

        * Камера без контроля движения (въезд): рамка с уверенностью ≥ plate_conf
          будит систему; номер распознаётся, частый режим держится active_hold_s
          секунд после последней рамки.
        * Камера с контролем движения (выезд): будит и держит активный режим только
          ДВИЖУЩИЙСЯ номер. Машина, стоящая за воротами, не будит камеру и не
          грузит процессор; распознавание запускается, когда движение подтверждено.
        """
        plate_boxes = self._detector.detect_plate(frame)
        self._observe_motion(plate_boxes, frame)
        now = self._clock()

        if self._motion_gate is None:
            relevant = bool(plate_boxes)
            run_ocr = relevant
        else:
            moving = self._motion_gate.moved_last() or self._motion_gate.is_moving()
            relevant = bool(plate_boxes) and moving
            run_ocr = relevant and self._motion_gate.is_moving()

        if relevant:
            if self._mode != "active":
                log.info("Камера '%s': %s — активный режим (частая обработка)", self.name,
                         "номер в движении" if self._motion_gate is not None else "обнаружен номер")
            self._mode = "active"
            self._active_until = now + self._active_hold_s
            if run_ocr:
                # Вырезаем рамку крупным планом и распознаём (как в обычном режиме).
                for pb in plate_boxes:
                    self._ocr_and_decide(_crop_padded(frame, pb, 0.30), frame)
            return

        # Номера нет (или он стоит): по истечении удержания — засыпаем.
        if self._mode == "active" and now >= self._active_until:
            log.info("Камера '%s': %s — спящий режим (редкая обработка)", self.name,
                     "номер не движется" if (self._motion_gate is not None and plate_boxes)
                     else "номер не виден")
            self._mode = "idle"

    def _act(self, plate, frame) -> None:
        """Принять решение по номеру с учётом движения и межкамерной паузы."""
        # Выездная камера: если требуется движение, а номер стоит — пропускаем.
        if self._motion_gate is not None and not self._motion_gate.is_moving():
            log.info("Камера '%s': номер не движется (стоит) — пропускаем: %s", self.name, plate)
            return
        # Выезд приглушён, пока кто-то заезжает (машина пересекает обзор выезда).
        if (self._coordinator is not None and self._role == "exit"
                and self._coordinator.exit_muted()):
            log.info("Камера '%s': выезд приглушён (идёт заезд) — пропускаем: %s", self.name, plate)
            return
        result = self._decision.handle(plate, frame)
        if self._coordinator is not None and result is not None and result.gate_opened:
            self._coordinator.note_open(self._role)

    def _ocr_and_decide(self, region, frame) -> None:
        """Распознать номер на области и, если удалось, принять решение."""
        ocr = self._ocr.read_text(region)
        if self._aggregator is not None:
            # Режим сборки по кадрам: копим кандидаты, действуем когда номер собран.
            raw = ocr.raw_text or (ocr.plate.value if ocr.plate else "")
            plate = self._aggregator.add(raw)
            if plate is not None:
                log.info("Камера '%s': номер собран по кадрам: %s", self.name, plate)
                self._act(plate, frame)
            return
        if ocr.is_readable and ocr.plate is not None:
            log.info("Камера '%s': распознан номер %s (conf=%.2f)", self.name, ocr.plate, ocr.confidence)
            self._act(ocr.plate, frame)


def _crop(frame, box):
    """Вырезать ROI."""
    try:
        return frame[box.y1:box.y2, box.x1:box.x2]
    except Exception:  # noqa: BLE001
        return frame


def _crop_padded(frame, box, pad_ratio: float = 0.15):
    """Вырезать ROI с полем вокруг (чтобы распознавателю был виден контекст рамки)."""
    try:
        h, w = frame.shape[:2]
        pw = int((box.x2 - box.x1) * pad_ratio)
        ph = int((box.y2 - box.y1) * pad_ratio)
        x1 = max(0, box.x1 - pw)
        y1 = max(0, box.y1 - ph)
        x2 = min(w, box.x2 + pw)
        y2 = min(h, box.y2 + ph)
        return frame[y1:y2, x1:x2]
    except Exception:  # noqa: BLE001
        return _crop(frame, box)
