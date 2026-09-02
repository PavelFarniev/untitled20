"""Распознавание номеров РФ через Nomeroff-Net (реализация порта OcrEngine).

Nomeroff-Net сам находит рамку номера, выравнивает её и распознаёт текст
специализированной моделью (обучена в т.ч. на номерах РФ) — это точнее, чем
универсальный EasyOCR по кадру машины.

Тяжёлые зависимости импортируются ЛЕНИВО, только при реальном использовании,
поэтому модуль импортируется и тестируется без установленного nomeroff-net.
Сама логика распознавания вынесена в инъектируемый `recognizer`
(callable(image) -> list[str]), что делает адаптер юнит-тестируемым.

Установка (на машине разработчика / RPi):
    pip install nomeroff-net
Требуется Python >= 3.9 (рекомендуется 3.11–3.12). Лицензия: GPL v3.
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Callable, List, Optional

from interfaces import OcrEngine
from models import OcrResult
from utils import normalize_plate

log = logging.getLogger("gate_system.ocr.nomeroff")


class _DefaultNomeroffRecognizer:
    """Лениво инициализирует пайплайн Nomeroff-Net и распознаёт номера.

    Изображение сохраняется во временный файл и передаётся пайплайну по пути —
    ровно как в документации Nomeroff-Net (image_loader="opencv").
    """

    def __init__(self, pipeline_name: str = "number_plate_detection_and_reading",
                 upscale: float = 1.0) -> None:
        self._pipeline_name = pipeline_name
        self._upscale = upscale
        self._pipeline = None
        self._unzip = None
        self._load_failed = False  # чтобы не пытаться грузить/качать модель повторно

    @staticmethod
    def _patch_torch_load() -> None:
        """Совместимость с PyTorch>=2.6: вернуть weights_only=False по умолчанию.

        Nomeroff-Net грузит чекпойнты со своими классами (StrLabelConverter и др.),
        которые новый torch.load с weights_only=True не пропускает. Модели скачаны
        из доверенного источника (nomeroff.net.ua), поэтому это безопасно.
        """
        import torch

        if getattr(torch.load, "_gate_patched", False):
            return
        _orig_load = torch.load

        def _patched(*args, **kwargs):
            kwargs.setdefault("weights_only", False)
            return _orig_load(*args, **kwargs)

        _patched._gate_patched = True
        torch.load = _patched

    @staticmethod
    def _patch_modelhub_download() -> None:
        """Сделать загрузку моделей Nomeroff устойчивой к обрывам сети.

        Штатный загрузчик Nomeroff (urllib.urlretrieve) не умеет докачивать: при
        разрыве качает файл заново с нуля — на нестабильном интернете это тупик.
        Подменяем его на загрузчик с докачкой (HTTP Range) и повторными попытками.
        """
        import os
        import shutil
        import time
        import urllib.error
        import urllib.request

        try:
            from nomeroff_net.tools.mcm import modelhub
        except Exception:  # noqa: BLE001
            return
        if getattr(modelhub, "_gate_patched", False):
            return

        def robust_download(url, target_path, *args, **kwargs):
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            last_err = None
            for attempt in range(1, 41):  # до 40 попыток докачки
                existing = os.path.getsize(target_path) if os.path.exists(target_path) else 0
                headers = {"Range": f"bytes={existing}-"} if existing else {}
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        status = resp.getcode()
                        total = resp.headers.get("Content-Length")
                        # Дописываем ТОЛЬКО если сервер подтвердил частичный ответ (206).
                        # Иначе (200) он отдаёт файл целиком — пишем с нуля, иначе портим.
                        if existing and status == 206:
                            mode = "ab"
                        else:
                            mode = "wb"
                            existing = 0
                        with open(target_path, mode) as f:
                            shutil.copyfileobj(resp, f, length=256 * 1024)
                    # Проверяем, что файл догрузился до ожидаемого размера.
                    if total is not None:
                        expected = existing + int(total)
                        if os.path.getsize(target_path) < expected:
                            raise IOError(f"неполный файл: {os.path.getsize(target_path)}/{expected}")
                    return target_path
                except urllib.error.HTTPError as e:
                    if e.code == 416:  # диапазон недопустим = файл уже целиком
                        return target_path
                    last_err = e
                except Exception as e:  # noqa: BLE001 - сеть нестабильна, пробуем снова
                    last_err = e
                log.warning("Докачка модели (попытка %d) прервана, продолжаю: %s", attempt, last_err)
                time.sleep(2)
            raise last_err if last_err else RuntimeError("Не удалось скачать модель")

        modelhub.download = robust_download
        modelhub._gate_patched = True
        log.info("Загрузчик моделей Nomeroff заменён на устойчивый (с докачкой)")

    def _ensure_loaded(self) -> None:
        if self._pipeline is not None or self._load_failed:
            return
        try:
            self._patch_torch_load()
            self._patch_modelhub_download()
            from nomeroff_net import pipeline  # тяжёлый импорт
            from nomeroff_net.tools import unzip
            log.info("Загрузка пайплайна Nomeroff-Net: %s", self._pipeline_name)
            self._pipeline = pipeline(self._pipeline_name, image_loader="opencv")
            self._unzip = unzip
        except Exception:
            # Модель не загрузилась (напр. не докачалась) — фиксируем и больше
            # не пытаемся в этом процессе, чтобы не запускать загрузку на каждом кадре.
            self._load_failed = True
            log.error("Не удалось загрузить Nomeroff-Net (проверьте загрузку моделей)")
            raise

    def __call__(self, image) -> List[str]:
        import cv2
        import numpy as np

        if not isinstance(image, np.ndarray) or self._load_failed:
            return []
        self._ensure_loaded()

        # Увеличиваем кадр перед распознаванием — больше пикселей на номер.
        if self._upscale and self._upscale > 1.0:
            image = cv2.resize(image, None, fx=self._upscale, fy=self._upscale,
                               interpolation=cv2.INTER_CUBIC)

        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            cv2.imwrite(path, image)
            result = self._pipeline([path])
            unzipped = self._unzip(result)
            # В пайплайне ...detection_and_reading тексты — последний элемент.
            texts = unzipped[-1] if unzipped else []
            return _flatten_texts(texts)
        finally:
            if os.path.exists(path):
                os.unlink(path)


def _flatten_texts(texts) -> List[str]:
    """Привести выдачу Nomeroff-Net (списки списков) к плоскому списку строк."""
    out: List[str] = []
    if not texts:
        return out
    first = texts[0] if len(texts) else texts
    if isinstance(first, (list, tuple)):
        out.extend(str(t) for t in first)
    else:
        out.append(str(first))
    return out


class NomeroffOcrEngine(OcrEngine):
    """Адаптер Nomeroff-Net под порт OcrEngine."""

    def __init__(
        self,
        min_confidence: float = 0.0,
        recognizer: Optional[Callable[[object], List[str]]] = None,
        upscale: float = 1.0,
    ) -> None:
        self._min_confidence = min_confidence
        self._recognizer = recognizer or _DefaultNomeroffRecognizer(upscale=upscale)

    def read_text(self, plate_image) -> OcrResult:
        try:
            candidates = self._recognizer(plate_image)
        except Exception:  # noqa: BLE001 - граница адаптера, не роняем цикл
            log.exception("Ошибка Nomeroff-Net при распознавании")
            return OcrResult(plate=None, confidence=0.0, raw_text="")

        for candidate in candidates:
            plate = normalize_plate(str(candidate))
            if plate is not None:
                return OcrResult(plate=plate, confidence=1.0, raw_text=str(candidate))
        raw = str(candidates[0]) if candidates else ""
        return OcrResult(plate=None, confidence=0.0, raw_text=raw)
