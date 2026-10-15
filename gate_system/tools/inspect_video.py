"""Проверка распознавания на видеофайле (без запуска всей системы).

Запуск (из любой папки):
    python tools/inspect_video.py /путь/к/видео.mp4

Примерно раз в секунду видео:
* находит рамку номера (best.pt), вырезает её с полями и распознаёт (Nomeroff);
* печатает прочитанный текст, номер РФ и есть ли он в белом списке;
* собирает номер голосованием по кадрам — как работающая система;
* сохраняет в inspect_out/ кадры с рамками и вырезанные номера.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # корень проекта

import cv2  # noqa: E402

from gate.config import Config  # noqa: E402
from gate.common.logger import quiet_third_party_warnings  # noqa: E402
from gate.core.pipeline import _crop_padded  # noqa: E402
from gate.paths import PROJECT_ROOT, resolve  # noqa: E402
from gate.vision.detector import YoloDetector  # noqa: E402
from gate.vision.ocr import build_ocr_engine  # noqa: E402


def _whitelist(cfg):
    """Белый список из базы системы (если она уже создана), иначе None."""
    db_path = resolve(cfg.paths.database)
    if not db_path.exists():
        return None
    from gate.storage.database import SqliteDatabase
    from gate.storage.whitelist import SqliteWhitelistRepository
    return SqliteWhitelistRepository(SqliteDatabase(db_path))


def main() -> None:
    if len(sys.argv) < 2:
        print("Использование: python tools/inspect_video.py /путь/к/видео.mp4")
        sys.exit(1)
    video_path = sys.argv[1]

    quiet_third_party_warnings()
    cfg = Config.load()
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print("НЕ УДАЛОСЬ открыть видео (проверьте путь/кодек).")
        sys.exit(1)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    print(f"Видео: {video_path}\nКадров: {total}, FPS: {fps:.1f}")

    plate_model = str(resolve(cfg.detection.plate_model_path)) if cfg.detection.plate_model_path else ""
    detector = YoloDetector(plate_model, cfg.detection.plate_conf)
    print(f"Модель рамки номера: {'ПОДКЛЮЧЕНА' if detector.has_plate_model else 'НЕТ (читаю весь кадр)'}")
    ocr = build_ocr_engine(cfg)
    print("Загружаю распознавание номеров (может занять до минуты)…")
    if not ocr.warmup():
        print("НЕ УДАЛОСЬ загрузить распознавание — см. ошибку выше.")
        sys.exit(1)
    whitelist = _whitelist(cfg)

    aggregator = None
    if cfg.access.confirm_frames > 0:
        from gate.core.aggregator import PlateAggregator
        aggregator = PlateAggregator(confirm=cfg.access.confirm_frames)
        print(f"Сборка номера по кадрам: нужно совпадений {cfg.access.confirm_frames}")

    def mark(plate) -> str:
        if whitelist is None:
            return ""
        return " ✅в списке" if whitelist.is_allowed(plate) else " ❌нет в списке"

    out_dir = PROJECT_ROOT / "inspect_out"
    out_dir.mkdir(exist_ok=True)

    step = max(1, int(fps))
    i = hits = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        i += 1
        if i % step:
            continue

        boxes = detector.detect_plate(frame) if detector.has_plate_model else []
        regions = [_crop_padded(frame, b, 0.30) for b in boxes] or [frame]
        annotated = frame.copy()
        for b in boxes:
            cv2.rectangle(annotated, (b.x1, b.y1), (b.x2, b.y2), (0, 0, 255), 3)
        cv2.imwrite(str(out_dir / f"frame_{i:05d}.jpg"), annotated)

        line = f"[кадр {i}] рамок: {len(boxes)}"
        for k, region in enumerate(regions):
            if region.size == 0:
                continue
            if boxes:
                cv2.imwrite(str(out_dir / f"plate_{i:05d}_{k}.jpg"), region)
            res = ocr.read_text(region)
            line += f"  #{k}: '{res.raw_text}'"
            if res.plate:
                hits += 1
                line += f" → {res.plate}{mark(res.plate)}"
            if aggregator is not None and res.raw_text:
                got = aggregator.add(res.raw_text)
                if got is not None:
                    line += f"  ✅ СОБРАН НОМЕР: {got}{mark(got)}"
        print(line)

    cap.release()
    print(f"\nРаспознано номеров: {hits}")
    print(f"Кадры и вырезанные рамки: {out_dir}")


if __name__ == "__main__":
    main()
