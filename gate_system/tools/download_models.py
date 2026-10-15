"""Заранее скачать и проверить все модели (вызывается из deploy/install.sh).

Запуск:
    python tools/download_models.py

* проверяет модели YOLO из папки models/ (best.pt — рамка номера);
* скачивает (с докачкой при обрывах) и загружает модели Nomeroff-Net;
* прогоняет один пробный кадр и печатает, сколько заняла загрузка.

После этого первый запуск системы не зависит от интернета.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # корень проекта

from gate.config import Config  # noqa: E402
from gate.common.logger import quiet_third_party_warnings  # noqa: E402
from gate.paths import resolve  # noqa: E402


def main() -> int:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    quiet_third_party_warnings()
    cfg = Config.load()
    ok = True

    plate_model = resolve(cfg.detection.plate_model_path) if cfg.detection.plate_model_path else None
    if plate_model is None or not plate_model.exists():
        print(f"❌ Нет модели рамки номера: {plate_model}. Скопируйте models/best.pt с Мака.")
        ok = False
    else:
        t = time.monotonic()
        from ultralytics import YOLO
        YOLO(str(plate_model))
        print(f"✅ Модель рамки номера загружается ({time.monotonic() - t:.1f} с): {plate_model}")

    from gate.vision.ocr import build_ocr_engine
    print("Скачиваю/загружаю модели распознавания (первый раз — несколько сотен МБ)…")
    t = time.monotonic()
    ocr = build_ocr_engine(cfg)
    warm = getattr(ocr, "warmup", None)
    if callable(warm) and not warm():
        print("❌ Распознавание не загрузилось — см. ошибку выше (часто это обрыв интернета; "
              "запустите скрипт ещё раз — загрузка продолжится с места обрыва).")
        ok = False
    else:
        print(f"✅ Распознавание номеров готово за {time.monotonic() - t:.0f} с")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
