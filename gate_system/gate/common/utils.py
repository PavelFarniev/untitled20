"""Общие утилиты, не привязанные к предметной области."""
from __future__ import annotations

from collections import deque
from pathlib import Path


def tail_lines(path: str, n: int = 20) -> list[str]:
    """Вернуть последние n строк файла (эффективно, без чтения целиком в память).

    Используется журналом в Telegram-боте. При отсутствии файла — пустой список.
    """
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8", errors="replace") as f:
        return [line.rstrip("\n") for line in deque(f, maxlen=max(1, n))]
