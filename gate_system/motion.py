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
  камеры и кратковременное перекрытие человеком не считались движением.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple


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

    def __init__(self, threshold: float = 0.02, confirm_frames: int = 2) -> None:
        """
        :param threshold: минимальное смещение центра рамки между кадрами (доля
            от размера кадра), которое считается движением. 0.02 = 2% ширины.
        :param confirm_frames: сколько кадров подряд с движением нужно, чтобы
            признать машину движущейся (защита от дрожания/перекрытия).
        """
        self._threshold = threshold
        self._confirm = max(1, confirm_frames)
        self._prev: List[Tuple[float, float]] = []
        self._moving_streak = 0

    def observe(self, boxes: Sequence, width: int, height: int) -> None:
        """Обновить состояние по рамкам текущего кадра."""
        centers = _norm_centers(boxes, width, height)
        if not centers:
            # Номера не видно (нет рамок / перекрыт) — не считаем это движением,
            # но и не сбрасываем прошлую позицию, чтобы после перекрытия сравнить
            # с тем же местом (машина, скорее всего, стоит там же).
            return
        moved = self._any_moved(centers)
        self._moving_streak = self._moving_streak + 1 if moved else 0
        self._prev = centers

    def is_moving(self) -> bool:
        """Движется ли номер (устойчиво, не разовый скачок)."""
        return self._moving_streak >= self._confirm

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
