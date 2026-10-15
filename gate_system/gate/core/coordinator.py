"""Координация нескольких камер (въезд/выезд).

Проблема: когда машина ЗАЕЗЖАЕТ (въездная камера открыла ворота), она пересекает
обзор ВЫЕЗДНОЙ камеры — и та могла бы ошибочно сработать. Решение: на короткое
время после открытия воротами со стороны въезда «глушим» выездные камеры.

Координатор — общий на все пайплайны. Часы инъектируются для тестируемости.
"""
from __future__ import annotations

import time
from typing import Callable


class CameraCoordinator:
    """Общая точка синхронизации камер: пауза выезда во время заезда."""

    def __init__(self, mute_exit_after_entry_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._mute_s = mute_exit_after_entry_s
        self._clock = clock
        self._entry_opened_at: float = float("-inf")

    def note_open(self, role: str) -> None:
        """Отметить, что камера с ролью `role` открыла ворота."""
        if role == "entry":
            self._entry_opened_at = self._clock()

    def exit_muted(self) -> bool:
        """Приглушены ли сейчас выездные камеры (недавно был заезд)."""
        return (self._clock() - self._entry_opened_at) < self._mute_s
