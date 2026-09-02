"""Сборка номера из нескольких кадров (устойчивость к смазу/ошибкам OCR).

На отдельных кадрах OCR часто ошибается по-разному: на одном верна основная
часть (Х002МС), на другом — регион (177). Агрегатор голосованием по нескольким
кадрам собирает полный номер: наиболее частую основную часть + наиболее частый
регион. Это резко повышает надёжность и на смазанном видео, и в реальной
установке (машина даёт десятки кадров).
"""
from __future__ import annotations

import re
import time
from collections import Counter, deque
from typing import Callable, Optional

from models import _PLATE_LAT_TO_CYR, PlateNumber
from utils import normalize_plate

# Основная часть номера РФ: буква, 3 цифры, 2 буквы.
_MAIN_RE = re.compile(r"[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}")
# Регион — цифры в самом конце строки (после букв).
_REGION_RE = re.compile(r"(\d{2,3})\d*$")


def _canon(raw: str) -> str:
    """Очистить и привести к кириллице (как в номере РФ)."""
    return re.sub(r"[^A-Za-zА-Яа-я0-9]", "", raw or "").upper().translate(_PLATE_LAT_TO_CYR)


def _top(counter: Counter, min_count: int):
    """Самый частый элемент, если он встретился не реже min_count раз."""
    if not counter:
        return None
    value, count = counter.most_common(1)[0]
    return value if count >= min_count else None


def _main_of(plate_value: str) -> str:
    """Основная часть номера (буква+3 цифры+2 буквы) из полного номера."""
    m = _MAIN_RE.search(plate_value)
    return m.group(0) if m else ""


class PlateAggregator:
    """Голосование по кадрам: собирает основную часть и регион раздельно."""

    def __init__(
        self,
        confirm: int = 2,
        window: int = 25,
        reemit_after_s: float = 200.0,
        evidence_window_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._confirm = max(1, confirm)
        self._reemit_after_s = reemit_after_s
        # Свидетельства старше этого окна выбрасываются: относятся к УЖЕ уехавшей
        # машине и не должны влиять на новую (иначе старый полный номер авторизует
        # другую машину той же серии).
        self._evidence_window_s = evidence_window_s
        self._clock = clock
        # Каждое свидетельство хранится как (время, значение) для устаревания.
        self._mains: deque = deque(maxlen=window)
        self._regions: deque = deque(maxlen=window)
        self._fulls: deque = deque(maxlen=window)   # полные номера, прочитанные ЦЕЛИКОМ
        self._emitted: Optional[str] = None   # последний выданный номер
        self._emitted_at: float = 0.0         # когда он был выдан

    def _prune(self, now: float) -> None:
        """Выбросить свидетельства старше окна присутствия (по времени)."""
        cutoff = now - self._evidence_window_s
        for dq in (self._mains, self._regions, self._fulls):
            while dq and dq[0][0] < cutoff:
                dq.popleft()

    def add(self, raw_text: str) -> Optional[PlateNumber]:
        """Добавить распознанный текст кадра. Вернуть полный номер, когда собран."""
        now = self._clock()
        canon = _canon(raw_text)

        main_match = _MAIN_RE.search(canon)
        if main_match:
            self._mains.append((now, main_match.group(0)))

        region_match = _REGION_RE.search(canon)
        if region_match:
            self._regions.append((now, region_match.group(1)))

        # Полный номер, прочитанный ЦЕЛИКОМ в этом кадре (буква+3ц+2б+регион).
        full = normalize_plate(raw_text)
        if full is not None:
            self._fulls.append((now, full.value))

        self._prune(now)  # убрать свидетельства уехавших машин

        main = _top(Counter(v for _, v in self._mains), self._confirm)
        if not main:
            return None

        # Безопасность против «фантома» из двух машин: если для этой основной части
        # СВЕЖО (в пределах окна) был ЦЕЛИКОМ прочитан полный номер (напр. Х002МС777)
        # — доверяем ему и НЕ склеиваем основную часть с чужим регионом. Собираем из
        # частей только когда полностью номер не видели (ночной случай Х002МС177).
        best_full = _top(Counter(v for _, v in self._fulls if _main_of(v) == main), self._confirm)
        if best_full is not None:
            plate = PlateNumber(best_full)
        else:
            region = _top(Counter(v for _, v in self._regions), self._confirm)
            plate = normalize_plate(main + region) if region else None

        if plate is None:
            return None
        # Не выдаём тот же номер подряд каждый кадр; повторно — только после паузы.
        if plate.value != self._emitted or (now - self._emitted_at) >= self._reemit_after_s:
            self._emitted = plate.value
            self._emitted_at = now
            return plate
        return None

    def reset(self) -> None:
        self._mains.clear()
        self._regions.clear()
        self._fulls.clear()
        self._emitted = None
        self._emitted_at = 0.0
