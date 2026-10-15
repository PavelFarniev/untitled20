"""Снимок «что сейчас видят камеры» для кнопки «📷 Фото» в Telegram.

Берёт последний кадр каждой камеры (LatestFrameSource.latest()), приводит к
одной ширине и складывает сверху вниз в одну картинку JPEG. Подписи ставятся в
подписи к фото (Telegram), а не на картинку: шрифты OpenCV не умеют кириллицу.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

log = logging.getLogger("gate_system.snapshot")

_ROLE_RU = {"entry": "въезд", "exit": "выезд"}


class SnapshotProvider:
    """:param sources: [(имя, роль, источник с методом latest()), ...]"""

    def __init__(self, sources: Sequence[Tuple[str, str, object]], width: int = 1280,
                 jpeg_quality: int = 80) -> None:
        self._sources = list(sources)
        self._width = width
        self._quality = jpeg_quality

    def snapshot(self) -> Optional[Tuple[bytes, str]]:
        """Вернуть (JPEG-байты, подпись) или None, если кадров ещё нет ни с одной камеры."""
        import cv2

        frames: List = []
        labels: List[str] = []
        missing: List[str] = []
        for name, role, src in self._sources:
            frame = None
            try:
                frame = src.latest()
            except Exception:  # noqa: BLE001
                log.debug("Не удалось взять кадр камеры %s", name, exc_info=True)
            label = f"{name} ({_ROLE_RU.get(role, role)})"
            if frame is None:
                missing.append(label)
                continue
            h, w = frame.shape[:2]
            if w != self._width:
                scale = self._width / float(w)
                frame = cv2.resize(frame, (self._width, max(1, int(h * scale))),
                                   interpolation=cv2.INTER_AREA)
            frames.append(frame)
            labels.append(label)

        if not frames:
            return None
        image = frames[0] if len(frames) == 1 else cv2.vconcat(frames)
        ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self._quality])
        if not ok:
            return None

        if len(labels) == 1:
            caption = f"📷 Камера: {labels[0]}"
        else:
            order = ["сверху", "ниже"] + ["ниже"] * max(0, len(labels) - 2)
            caption = "📷 " + "; ".join(f"{pos}: {lab}" for pos, lab in zip(order, labels))
        if missing:
            caption += "\n⚠️ Нет кадра: " + ", ".join(missing)
        return buf.tobytes(), caption
