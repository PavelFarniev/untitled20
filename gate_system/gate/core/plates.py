"""Нормализация номерных знаков РФ (доменная логика)."""
from __future__ import annotations

import re
from typing import Optional, Tuple

from gate.core.models import PlateNumber

# Формат номера РФ: буква, 3 цифры, 2 буквы, 2-3 цифры региона.
# Разрешены только буквы, визуально совпадающие с латиницей (ГОСТ Р 50577).
# Ищем ПОДСТРОКОЙ (search), чтобы извлечь номер из шумного текста OCR
# (например «Т198ТТ197RUS» -> «Т198ТТ197»).
_RU_PLATE_RE = re.compile(r"[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}")

# Частые ошибки OCR: латиница -> кириллица для номеров РФ.
_LAT_TO_CYR = str.maketrans(
    {"A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н",
     "O": "О", "P": "Р", "C": "С", "T": "Т", "Y": "У", "X": "Х"}
)


def normalize_plate(raw: str) -> Optional[PlateNumber]:
    """Привести распознанный текст к каноническому номеру РФ.

    Возвращает PlateNumber, если строка соответствует формату, иначе None.
    """
    cleaned = re.sub(r"[^A-Za-zА-Яа-я0-9]", "", raw or "").upper()
    cleaned = cleaned.translate(_LAT_TO_CYR)
    match = _RU_PLATE_RE.search(cleaned)
    if match:
        return PlateNumber(match.group(0))
    return None


def _clean(text: str) -> str:
    return re.sub(r"[^A-Za-zА-Яа-я0-9]", "", text or "").upper().translate(_LAT_TO_CYR)


def split_plate_and_note(text: str) -> Tuple[Optional[PlateNumber], str]:
    """Разобрать ввод «номер [подпись]»: «А123ВС777 Папа», «а 123 вс 777 Курьер».

    Номер может быть набран с пробелами — берётся самое длинное начало строки,
    которое целиком является номером («А123ВС77 7» → регион 777); остаток —
    подпись. Возвращает (номер|None, подпись).
    """
    tokens = (text or "").split()
    for i in range(len(tokens), 0, -1):
        candidate = "".join(tokens[:i])
        plate = normalize_plate(candidate)
        if plate is not None and _clean(candidate) == plate.value:
            return plate, " ".join(tokens[i:]).strip()
    return normalize_plate(text or ""), ""
