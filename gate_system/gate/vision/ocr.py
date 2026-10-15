"""Сборка распознавателя номеров по конфигу.

Используется gate/app.py и утилитами tools/, чтобы движок создавался в одном
месте. Сейчас движок один — Nomeroff-Net (специализирован на номерах РФ).
"""
from __future__ import annotations

import logging

from gate.core.interfaces import OcrEngine

log = logging.getLogger("gate_system.ocr")


def build_ocr_engine(cfg) -> OcrEngine:
    """Создать распознаватель номеров (Nomeroff-Net) по секции ocr конфига."""
    from gate.vision.nomeroff_ocr import NomeroffOcrEngine  # ленивый импорт тяжёлой зависимости

    log.info("Распознаватель номеров: Nomeroff-Net")
    return NomeroffOcrEngine(upscale=cfg.ocr.upscale)
