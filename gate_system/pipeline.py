"""Прикладная логика (use cases) — сердце системы.

Зависит ТОЛЬКО от портов (interfaces.py) и домена (models.py). Не знает ни о
камере как о RTSP, ни о реле как о GPIO, ни о Telegram. Это делает ядро
полностью переносимым и тестируемым.

Содержит:
* AntiReplayGuard  — защита от повторного открытия (окно 30 с);
* AccessDecision   — решение о доступе и оркестрация побочных эффектов;
* RecognitionPipeline — главный цикл захвата → детекции → OCR → решения.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from interfaces import (
    Detector,
    FrameSource,
    GateController,
    HistoryRepository,
    Notifier,
    OcrEngine,
    WhitelistRepository,
)
from models import (
    AccessResult,
    CheckResult,
    Event,
    OpenSource,
    PlateNumber,
)
from events import event as user_event

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
    """Главный рабочий цикл: кадр → авто → номер → текст → решение."""

    def __init__(
        self,
        source: FrameSource,
        detector: Detector,
        ocr: OcrEngine,
        decision: AccessDecision,
        process_every_n: int = 1,
        on_heartbeat: Optional[callable] = None,
        require_vehicle: bool = True,
        aggregator=None,
        motion_gate=None,
        role: str = "entry",
        coordinator=None,
        adaptive: bool = False,
        idle_every_n: int = 30,
        active_hold_s: float = 5.0,
    ) -> None:
        self._source = source
        self._detector = detector
        self._ocr = ocr
        self._decision = decision
        self._process_every_n = max(1, process_every_n)
        self._on_heartbeat = on_heartbeat
        # Опциональная сборка номера по нескольким кадрам (голосование).
        self._aggregator = aggregator
        # Если False — не требуем видеть машину: OCR ищет номер по всему кадру
        # (подходит для распознавателей вроде Nomeroff, которые сами находят рамку).
        self._require_vehicle = require_vehicle
        # Опциональный контроль движения (выездная камера: игнорировать стоящих).
        self._motion_gate = motion_gate
        # Роль камеры (entry/exit) и координатор для межкамерной паузы.
        self._role = role
        self._coordinator = coordinator
        # Адаптивный режим: в простое только лёгкая детекция рамки с редким шагом
        # (idle_every_n), при появлении номера — частый шаг (process_every_n) с
        # полным распознаванием, затем возврат в простой через active_hold_s.
        self._adaptive = adaptive
        self._idle_every_n = max(1, idle_every_n)
        self._active_hold_s = max(0.0, active_hold_s)
        self._mode = "idle"          # "idle" | "active"
        self._active_until = 0.0     # монотонное время, до которого держим активный режим
        self._running = False
        self._frame_i = 0

    def stop(self) -> None:
        self._running = False

    def _current_stride(self) -> int:
        """Текущий шаг прореживания кадров (зависит от режима в адаптивном варианте)."""
        if self._adaptive and self._mode == "idle":
            return self._idle_every_n
        return self._process_every_n

    def run_forever(self) -> None:
        """Бесконечный цикл обработки с авто-переподключением камеры."""
        self._running = True
        self._source.open()
        log.info("Пайплайн распознавания запущен")
        while self._running:
            try:
                if self._on_heartbeat is not None:
                    self._on_heartbeat()  # сигнал «жив» для watchdog
                frame = self._source.read()
                if frame is None:
                    log.warning("Кадр не получен — переподключение к камере")
                    self._source.reconnect()
                    continue
                self._frame_i += 1
                if self._frame_i % self._current_stride() != 0:
                    continue
                self._process_frame(frame)
            except Exception:  # noqa: BLE001 - цикл не должен падать 24/7
                log.exception("Ошибка в цикле распознавания; продолжаем")
                time.sleep(0.5)
        self._source.close()
        log.info("Пайплайн распознавания остановлен")

    def _process_frame(self, frame) -> None:
        """Обработать кадр: для каждого авто найти номер и распознать.

        Если специализированная модель номера настроена и нашла рамки — OCR по
        ним. Если модели номера нет (detect_plate вернул пусто) — OCR по всей
        области автомобиля: EasyOCR сам найдёт текст, а фильтр формата РФ
        (normalize_plate) отсеет всё, что не похоже на номер. Это позволяет
        тестировать систему, имея только модель обнаружения авто.
        """
        # Адаптивный режим: в простое — только лёгкая детекция рамки; тяжёлый OCR
        # подключается лишь при появлении номера.
        if self._adaptive:
            self._process_adaptive(frame)
            return

        # Режим без требования машины.
        if not self._require_vehicle:
            # Если есть модель рамки номера — сначала находим рамку, вырезаем её
            # крупным планом и распознаём (больше пикселей на номер). Иначе — OCR
            # по всему кадру (распознаватель сам найдёт номер).
            plate_boxes = self._detector.detect_plate(frame)
            # Обновляем детектор движения по положению рамок номера.
            if self._motion_gate is not None:
                try:
                    h, w = frame.shape[:2]
                    self._motion_gate.observe(plate_boxes, w, h)
                except Exception:  # noqa: BLE001
                    pass
            if plate_boxes:
                for pb in plate_boxes:
                    self._ocr_and_decide(_crop_padded(frame, pb, 0.30), frame)
            else:
                self._ocr_and_decide(frame, frame)
            return

        for vbox in self._detector.detect_vehicle(frame):
            vehicle_roi = _crop(frame, vbox)
            plate_boxes = self._detector.detect_plate(vehicle_roi)
            regions = [_crop(vehicle_roi, pb) for pb in plate_boxes] or [vehicle_roi]
            for region in regions:
                self._ocr_and_decide(region, frame)

    def _process_adaptive(self, frame) -> None:
        """Адаптивная обработка кадра.

        В простое запускается ТОЛЬКО лёгкий детектор рамки (best.pt) — он дёшев.
        Пока рамки нет, тяжёлый распознаватель (Nomeroff) не трогается вовсе.
        Как только рамка найдена (уверенность ≥ plate_conf детектора), система
        переходит в активный режим: распознаёт номер и держит частый шаг ещё
        active_hold_s секунд после последней замеченной рамки. Требует наличия
        модели рамки номера (detect_plate); без неё режим бессмысленен.
        """
        plate_boxes = self._detector.detect_plate(frame)
        # Детектор движения (выездная камера) обновляем и в адаптивном режиме.
        if self._motion_gate is not None:
            try:
                h, w = frame.shape[:2]
                self._motion_gate.observe(plate_boxes, w, h)
            except Exception:  # noqa: BLE001
                pass

        now = time.monotonic()
        if plate_boxes:
            if self._mode != "active":
                log.info("Обнаружен номер — активный режим (частая обработка)")
            self._mode = "active"
            self._active_until = now + self._active_hold_s
            # Вырезаем рамку крупным планом и распознаём (как в обычном режиме).
            for pb in plate_boxes:
                self._ocr_and_decide(_crop_padded(frame, pb, 0.30), frame)
            return

        # Рамки нет: если были активны и вышло время удержания — засыпаем.
        if self._mode == "active" and now >= self._active_until:
            log.info("Номер не виден — спящий режим (редкая обработка)")
            self._mode = "idle"

    def _act(self, plate, frame) -> None:
        """Принять решение по номеру с учётом движения и межкамерной паузы."""
        # Выездная камера: если требуется движение, а номер стоит — пропускаем.
        if self._motion_gate is not None and not self._motion_gate.is_moving():
            log.info("Номер не движется (стоит) — пропускаем: %s", plate)
            return
        # Выезд приглушён, пока кто-то заезжает (машина пересекает обзор выезда).
        if (self._coordinator is not None and self._role == "exit"
                and self._coordinator.exit_muted()):
            log.info("Выезд приглушён (идёт заезд) — пропускаем: %s", plate)
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
                log.info("Номер собран по кадрам: %s", plate)
                self._act(plate, frame)
            return
        if ocr.is_readable and ocr.plate is not None:
            log.info("Распознан номер: %s (conf=%.2f)", ocr.plate, ocr.confidence)
            self._act(ocr.plate, frame)


def _crop(frame, box):
    """Вырезать ROI. Реальная реализация (numpy) появится вместе с камерой."""
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
