"""Общие утилиты, не привязанные к конкретному слою."""
from __future__ import annotations

import re
from collections import deque
from pathlib import Path
from typing import Optional

from models import PlateNumber

# Формат номера РФ: буква, 3 цифры, 2 буквы, 2-3 цифры региона.
# Разрешены только буквы, визуально совпадающие с латиницей (ГОСТ Р 50577).
# Ищем ПОДСТРОКОЙ (search), чтобы извлечь номер из шумного текста OCR
# (например «Т198ТТ197RUS» -> «Т198ТТ197»).
_RU_PLATE_RE = re.compile(r"[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}")

# Частые ошибки OCR: латиница -> кириллица для номеров РФ.
_LAT_TO_CYR = str.maketrans(
    {"A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н",
     "O": "О", "P": "Р", "C": "С", "T": "Т", "Y": "У", "X": "Х", "0": "0"}
)


def normalize_plate(raw: str) -> Optional[PlateNumber]:
    """Привести распознанный текст к каноническому номеру РФ.

    Возвращает PlateNumber, если строка соответствует формату, иначе None.
    """
    cleaned = re.sub(r"[^A-Za-zА-Яа-я0-9]", "", raw).upper()
    cleaned = cleaned.translate(_LAT_TO_CYR)
    match = _RU_PLATE_RE.search(cleaned)
    if match:
        return PlateNumber(match.group(0))
    return None


def tail_lines(path: str, n: int = 20) -> list[str]:
    """Вернуть последние n строк файла (эффективно, без чтения целиком в память).

    Используется командой /logs Telegram-бота. При отсутствии файла — пустой список.
    """
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8", errors="replace") as f:
        return [line.rstrip("\n") for line in deque(f, maxlen=max(1, n))]
