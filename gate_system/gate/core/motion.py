"""Определение движения номера по кадрам (для выездной камеры).

Задача: не открывать ворота на машину, которая просто СТОИТ в кадре (припаркована
во дворе), и реагировать только когда она поехала. Направление не анализируем —
достаточно факта движения: если рамка номера заметно сместилась между кадрами,
машина едет.

Логика проста и потому легко проверяется тестами:
* по каждому кадру берём центры рамок номера (нормированные к размеру кадра);
* сравниваем с центрами прошлого кадра (ближайшая пара);
* если смещение больше порога — засчитываем «кадр с движением»;
* «движется» = было несколько таких кадров подряд (подтверждение), чтобы дрожание
  камеры и кратковременное перекрытие человеком не считались движением;
* «память движения» (memory_s): подтверждённое движение помнится ещё N секунд.
  Машина подъезжает к воротам и останавливается в ожидании, пока на медленном
  Raspberry Pi дочитывается номер — её по-прежнему считаем «едущей». А машину,
  стоящую давно, — нет.
"""
from __future__ import annotations

import math
import time
from typing import Callable, List, Sequence, Tuple


def _norm_centers(boxes, width: int, height: int) -> List[Tuple[float, float]]:
    """Нормированные центры рамок (доля от ширины/высоты кадра)."""
    if not width or not height:
        return []
    centers = []
    for b in boxes:
        cx = (b.x1 + b.x2) / 2.0 / width
        cy = (b.y1 + b.y2) / 2.0 / height
        centers.append((cx, cy))
    return centers


class MotionGate:
    """Следит, движется ли номер в кадре."""

    def __init__(self, threshold: float = 0.02, confirm_frames: int = 2,
                 memory_s: float = 0.0, clock: Callable[[], float] = time.monotonic) -> None:
        """
        :param threshold: минимальное смещение центра рамки между кадрами (доля
            от размера кадра), которое считается движением. 0.02 = 2% ширины.
        :param confirm_frames: сколько кадров подряд с движением нужно, чтобы
            признать машину движущейся (защита от дрожания/перекрытия).
        :param memory_s: сколько секунд после подтверждённого движения машина
            ещё считается движущейся (0 — без памяти).
        """
        self._threshold = threshold
        self._confirm = max(1, confirm_frames)
        self._memory_s = max(0.0, memory_s)
        self._clock = clock
        self._prev: List[Tuple[float, float]] = []
        self._moving_streak = 0
        self._moved_last = False
        self._confirmed_at = float("-inf")

    def observe(self, boxes: Sequence, width: int, height: int) -> None:
        """Обновить состояние по рамкам текущего кадра."""
        centers = _norm_centers(boxes, width, height)
        if not centers:
            # Номера не видно (нет рамок / перекрыт) — не считаем это движением,
            # но и не сбрасываем прошлую позицию, чтобы после перекрытия сравнить
            # с тем же местом (машина, скорее всего, стоит там же).
            self._moved_last = False
            return
        moved = self._any_moved(centers)
        self._moved_last = moved
        self._moving_streak = self._moving_streak + 1 if moved else 0
        if self._moving_streak >= self._confirm:
            self._confirmed_at = self._clock()
        self._prev = centers

    def moved_last(self) -> bool:
        """Сдвинулся ли номер на последнем кадре (сигнал «проснуться»)."""
        return self._moved_last

    def is_moving(self) -> bool:
        """Движется ли номер (устойчиво, не разовый скачок; с учётом памяти)."""
        if self._moving_streak >= self._confirm:
            return True
        return self._memory_s > 0 and (self._clock() - self._confirmed_at) < self._memory_s

    def _any_moved(self, centers: List[Tuple[float, float]]) -> bool:
        if not self._prev:
            return False
        for cx, cy in centers:
            nearest = min(
                (math.dist((cx, cy), p) for p in self._prev), default=None
            )
            if nearest is not None and nearest > self._threshold:
                return True
        return False
