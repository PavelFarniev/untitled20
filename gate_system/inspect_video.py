"""Диагностика распознавания на видеофайле.

Запуск:
    python inspect_video.py /путь/к/видео.mp4

Для каждого кадра (примерно раз в секунду):
* находит машины (YOLO);
* если задана модель рамки номера (plate.pt) — находит рамку номера ВНУТРИ машины,
  вырезает её, увеличивает и распознаёт; иначе OCR по всей области машины;
* печатает, что прочитал OCR и приводится ли текст к номеру РФ;
* сохраняет в inspect_out/ кадр с рамками и отдельно вырезанные рамки номера,
  чтобы визуально увидеть, что именно уходит в распознавание.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2

from config import Config
from detector import YoloDetector
from ocr import build_ocr_engine
from whitelist import JsonWhitelistRepository


def _crop(img, box):
    h, w = img.shape[:2]
    x1, y1 = max(0, box.x1), max(0, box.y1)
    x2, y2 = min(w, box.x2), min(h, box.y2)
    return img[y1:y2, x1:x2]


def main() -> None:
    if len(sys.argv) < 2:
        print("Использование: python inspect_video.py /путь/к/видео.mp4")
        sys.exit(1)
    video_path = sys.argv[1]

    cfg = Config.load()
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print("НЕ УДАЛОСЬ открыть видео (проверьте путь/кодек).")
        sys.exit(1)

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    print(f"Видео: {video_path}\nКадров: {total}, FPS: {fps:.1f}")

    print("Загружаю модели…")
    detector = YoloDetector(cfg.detection.model_path, cfg.detection.plate_model_path,
                            cfg.detection.vehicle_conf, cfg.detection.plate_conf)
    has_plate_model = detector._plate_model is not None
    print(f"Модель рамки номера: {'ПОДКЛЮЧЕНА' if has_plate_model else 'НЕТ (читаю всю машину)'}")
    print(f"Движок OCR: {getattr(cfg.ocr, 'engine', 'easyocr')}")
    require_vehicle = getattr(cfg.detection, "require_vehicle", True)
    print(f"Требовать машину в кадре: {'да' if require_vehicle else 'нет (ищу номер по всему кадру)'}")
    ocr = build_ocr_engine(cfg)
    whitelist = JsonWhitelistRepository(cfg.paths.allowed)

    confirm = getattr(cfg.access, "confirm_frames", 0)
    aggregator = None
    if confirm > 0:
        from aggregator import PlateAggregator
        aggregator = PlateAggregator(confirm=confirm)
        print(f"Сборка номера по кадрам: включена (нужно совпадений: {confirm})")

    motion_gate = None
    if getattr(cfg.detection, "require_motion", False):
        from motion import MotionGate
        motion_gate = MotionGate(cfg.detection.motion_threshold,
                                 cfg.detection.motion_confirm_frames)
        print("Контроль движения: включён (реагирую только на движущийся номер)")

    def feed(res):
        """Скормить распознанный текст агрегатору; вернуть подпись о сборке."""
        if aggregator is not None and res.raw_text:
            got = aggregator.add(res.raw_text)
            if got is not None:
                return f"  ✅ СОБРАН НОМЕР: {got} " + (
                    "✅в списке" if whitelist.is_allowed(got) else "❌нет в списке")
        return ""

    out_dir = Path("inspect_out")
    out_dir.mkdir(exist_ok=True)

    step = max(1, int(fps))
    i = 0
    hits = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        i += 1
        if i % step != 0:
            continue

        # Режим без машины: если есть модель рамки — вырезаем номер и читаем крупно.
        if not require_vehicle:
            plates = detector.detect_plate(frame) if has_plate_model else []
            if motion_gate is not None:
                motion_gate.observe(plates, frame.shape[1], frame.shape[0])
            if plates:
                moving = "" if motion_gate is None else (
                    " [движется]" if motion_gate.is_moving() else " [стоит]")
                line = f"[кадр {i}] рамок номера: {len(plates)}{moving}"
                annotated = frame.copy()
                for pi, p in enumerate(plates):
                    cv2.rectangle(annotated, (p.x1, p.y1), (p.x2, p.y2), (0, 0, 255), 3)
                    pw = int((p.x2 - p.x1) * 0.30)
                    ph = int((p.y2 - p.y1) * 0.30)
                    crop = frame[max(0, p.y1 - ph):p.y2 + ph, max(0, p.x1 - pw):p.x2 + pw]
                    if crop.size:
                        cv2.imwrite(str(out_dir / f"plate_{i:05d}_{pi}.jpg"), crop)
                        res = ocr.read_text(crop)
                        line += f"  #{pi}: '{res.raw_text}'(conf={res.confidence:.2f})"
                        if res.plate:
                            allowed = whitelist.is_allowed(res.plate)
                            line += f" → {res.plate} {'✅в списке' if allowed else '❌нет'}"
                            hits += 1
                        line += feed(res)
                cv2.imwrite(str(out_dir / f"frame_{i:05d}.jpg"), annotated)
            else:
                res = ocr.read_text(frame)
                line = f"[кадр {i}] (весь кадр) '{res.raw_text}'(conf={res.confidence:.2f})"
                if res.plate:
                    allowed = whitelist.is_allowed(res.plate)
                    line += f" → {res.plate} {'✅в списке' if allowed else '❌нет'}"
                    hits += 1
                line += feed(res)
                cv2.imwrite(str(out_dir / f"frame_{i:05d}.jpg"), frame)
            print(line)
            continue

        vehicles = detector.detect_vehicle(frame)
        annotated = frame.copy()
        line = f"[кадр {i}] машин: {len(vehicles)}"

        for vi, v in enumerate(vehicles):
            cv2.rectangle(annotated, (v.x1, v.y1), (v.x2, v.y2), (0, 255, 0), 2)
            veh_roi = _crop(frame, v)

            plates = detector.detect_plate(veh_roi) if has_plate_model else []
            if plates:
                for pi, p in enumerate(plates):
                    # координаты рамки номера — внутри области машины
                    cv2.rectangle(annotated, (v.x1 + p.x1, v.y1 + p.y1),
                                  (v.x1 + p.x2, v.y1 + p.y2), (0, 0, 255), 2)
                    plate_img = _crop(veh_roi, p)
                    if plate_img.size == 0:
                        continue
                    cv2.imwrite(str(out_dir / f"plate_{i:05d}_{vi}_{pi}.jpg"), plate_img)
                    res = ocr.read_text(plate_img)
                    line += f"  рамка#{pi}: '{res.raw_text}'(conf={res.confidence:.2f})"
                    if res.plate:
                        allowed = whitelist.is_allowed(res.plate)
                        line += f" → {res.plate} {'✅в списке' if allowed else '❌нет'}"
                        hits += 1
            else:
                res = ocr.read_text(veh_roi)
                line += f"  (вся машина) '{res.raw_text}'(conf={res.confidence:.2f})"
                if res.plate:
                    allowed = whitelist.is_allowed(res.plate)
                    line += f" → {res.plate} {'✅в списке' if allowed else '❌нет'}"
                    hits += 1

        print(line)
        cv2.imwrite(str(out_dir / f"frame_{i:05d}.jpg"), annotated)

    cap.release()
    print(f"\nРаспознано номеров: {hits}")
    print(f"Кадры и вырезанные рамки: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
