"""Проверка ТОЛЬКО детектора рамки номера (best.pt), без Nomeroff.

Отвечает на два вопроса для идеи «дешёвого триггера»:
1. Находит ли best.pt рамку номера на кадрах (и с какой уверенностью)?
2. Сколько времени тратит на ОДИН кадр (это и есть цена простоя, если крутить
   его раз в секунду вместо тяжёлого Nomeroff)?

Запуск:
    python check_plate_detector.py /путь/к/видео.mp4

Печатает по кадрам: число найденных рамок, лучшую уверенность и время инференса.
В конце — среднее время на кадр и доля кадров, где номер найден. Аннотированные
кадры сохраняются в detector_out/.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2

from config import Config


def main() -> None:
    if len(sys.argv) < 2:
        print("Использование: python check_plate_detector.py /путь/к/видео.mp4")
        sys.exit(1)
    video = sys.argv[1]

    cfg = Config.load()
    model_path = cfg.detection.plate_model_path or "models/best.pt"
    print(f"Модель детекции рамки: {model_path}")

    from ultralytics import YOLO  # тяжёлый импорт только здесь
    model = YOLO(model_path)

    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        print("Не удалось открыть видео.")
        sys.exit(1)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"Видео: {video}\nКадров: {total}, FPS: {fps:.0f}")

    out_dir = Path("detector_out")
    out_dir.mkdir(exist_ok=True)

    step = max(1, int(fps))          # ~1 кадр в секунду видео
    conf = cfg.detection.plate_conf
    i = 0
    processed = 0
    frames_with_plate = 0
    times = []
    # Прогреваем модель (первый вызов всегда дольше — не искажаем статистику).
    ok, warm = cap.read()
    if ok:
        model(warm, verbose=False, conf=conf)

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        i += 1
        if i % step != 0:
            continue
        processed += 1

        t0 = time.perf_counter()
        res = model(frame, verbose=False, conf=conf)
        dt = (time.perf_counter() - t0) * 1000  # мс
        times.append(dt)

        boxes = res[0].boxes
        n = len(boxes)
        best_conf = float(boxes.conf.max()) if n else 0.0
        if n:
            frames_with_plate += 1
        annotated = frame.copy()
        for b in boxes.xyxy.cpu().numpy():
            x1, y1, x2, y2 = map(int, b[:4])
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 0, 255), 3)
        cv2.imwrite(str(out_dir / f"det_{i:05d}.jpg"), annotated)
        print(f"[кадр {i}] рамок: {n}  уверенность: {best_conf:.2f}  время: {dt:.0f} мс")

    cap.release()
    if times:
        avg = sum(times) / len(times)
        print("\n--- ИТОГ ---")
        print(f"Обработано кадров: {processed}")
        print(f"Рамка найдена на: {frames_with_plate}/{processed} кадрах")
        print(f"Среднее время на кадр: {avg:.0f} мс  (~{1000/avg:.1f} кадр/с на этом железе)")
        print(f"Аннотированные кадры: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
