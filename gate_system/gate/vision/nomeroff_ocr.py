"""Распознавание номеров РФ через Nomeroff-Net (реализация порта OcrEngine).

Nomeroff-Net сам находит рамку номера, выравнивает её и распознаёт текст
специализированной моделью (обучена в т.ч. на номерах РФ) — это точнее, чем
универсальный EasyOCR по кадру машины.

Поведение, важное для Raspberry Pi:
* модели загружаются ЗАРАНЕЕ (warmup при старте системы), а не в потоке камеры
  при первой машине: на Pi загрузка идёт десятки секунд, а при первом запуске
  модели ещё и скачиваются — watchdog принял бы это за зависание;
* если загрузка не удалась (нет интернета при первом скачивании и т.п.), система
  не падает: распознавание отвечает «пусто», а повторная попытка идёт в ФОНЕ раз
  в retry_after_s секунд; о сбое и восстановлении пишется в понятный журнал;
* инференс идёт строго по очереди (замок): пайплайны двух камер работают в разных
  потоках с ОДНИМ движком, а модели PyTorch не потокобезопасны;
* временный кадр для Nomeroff пишется в оперативную память (/dev/shm), а не на
  SD-карту — карта не изнашивается от тысяч записей в день.

Тяжёлые зависимости импортируются лениво, поэтому модуль импортируется и
тестируется без установленного nomeroff-net. Лицензия Nomeroff-Net: GPL v3.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from typing import Callable, List, Optional

from gate.core.interfaces import OcrEngine
from gate.core.models import OcrResult
from gate.core.plates import normalize_plate
from gate.common.events import event as user_event

log = logging.getLogger("gate_system.ocr.nomeroff")

# Каталог в оперативной памяти (Linux). На macOS его нет — используется обычный temp.
_RAM_TMP = "/dev/shm" if os.path.isdir("/dev/shm") else None


class _DefaultNomeroffRecognizer:
    """Загружает пайплайн Nomeroff-Net и распознаёт номера.

    Изображение сохраняется во временный файл и передаётся пайплайну по пути —
    ровно как в документации Nomeroff-Net (image_loader="opencv").
    """

    def __init__(self, pipeline_name: str = "number_plate_detection_and_reading",
                 upscale: float = 1.0, retry_after_s: float = 300.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._pipeline_name = pipeline_name
        self._upscale = upscale
        self._retry_after_s = retry_after_s
        self._clock = clock
        self._pipeline = None
        self._unzip = None
        self._load_lock = threading.Lock()
        self._loading = False
        self._failed_at: Optional[float] = None
        self._was_failed = False

    # ------------------------------------------------------------ состояние
    @property
    def ready(self) -> bool:
        return self._pipeline is not None

    @property
    def state(self) -> str:
        if self.ready:
            return "готов"
        if self._loading:
            return "загружается"
        if self._failed_at is not None:
            return "ошибка загрузки (повтор в фоне)"
        return "не загружен"

    # ------------------------------------------------------------ загрузка
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
        import shutil
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
        log.debug("Загрузчик моделей Nomeroff заменён на устойчивый (с докачкой)")

    def load(self) -> bool:
        """Загрузить модели (блокирующе). True — готово к распознаванию."""
        with self._load_lock:
            if self.ready:
                return True
            self._loading = True
            started = time.monotonic()
            try:
                self._patch_torch_load()
                self._patch_modelhub_download()
                from nomeroff_net import pipeline  # тяжёлый импорт
                from nomeroff_net.tools import unzip
                log.info("Загрузка моделей Nomeroff-Net (%s)…", self._pipeline_name)
                pipe = pipeline(self._pipeline_name, image_loader="opencv")
                self._unzip = unzip
                # Сначала прогрев, и только потом «готов»: иначе поток камеры начал
                # бы распознавание одновременно с прогревом на той же модели.
                self._warm_inference(pipe)
                self._pipeline = pipe
                log.info("Nomeroff-Net готов за %.0f с", time.monotonic() - started)
                if self._was_failed:
                    user_event("✅ Распознавание номеров снова работает")
                self._failed_at = None
                self._was_failed = False
                return True
            except Exception:  # noqa: BLE001
                self._failed_at = self._clock()
                if not self._was_failed:
                    user_event("❗ Не удалось загрузить распознавание номеров — повторю попытку автоматически")
                self._was_failed = True
                log.exception("Не удалось загрузить Nomeroff-Net (повтор через %.0f с)", self._retry_after_s)
                return False
            finally:
                self._loading = False

    def load_async(self) -> None:
        """Запустить загрузку в фоне, если она не идёт и пора повторить попытку."""
        if self.ready or self._loading:
            return
        if self._failed_at is not None and self._clock() - self._failed_at < self._retry_after_s:
            return
        self._loading = True  # ставим сразу, чтобы не запустить два потока
        threading.Thread(target=self.load, name="nomeroff-load", daemon=True).start()

    def _warm_inference(self, pipe) -> None:
        """Прогнать один пустой кадр: первый инференс на CPU самый медленный."""
        try:
            import numpy as np
            self._infer(np.zeros((480, 640, 3), dtype=np.uint8), pipe)
        except Exception:  # noqa: BLE001
            log.debug("Прогревочный проход Nomeroff не удался (не критично)", exc_info=True)

    # ------------------------------------------------------------ распознавание
    def __call__(self, image) -> List[str]:
        import numpy as np

        if not isinstance(image, np.ndarray):
            return []
        if not self.ready:
            self.load_async()   # не блокируем поток камеры
            return []
        return self._infer(image)

    def _infer(self, image, pipe=None) -> List[str]:
        import cv2

        pipe = pipe or self._pipeline

        # Увеличиваем кадр перед распознаванием — больше пикселей на номер.
        if self._upscale and self._upscale > 1.0:
            image = cv2.resize(image, None, fx=self._upscale, fy=self._upscale,
                               interpolation=cv2.INTER_CUBIC)

        fd, path = tempfile.mkstemp(suffix=".jpg", dir=_RAM_TMP)
        os.close(fd)
        try:
            cv2.imwrite(path, image)
            result = pipe([path])
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
        recognizer: Optional[Callable[[object], List[str]]] = None,
        upscale: float = 1.0,
        retry_after_s: float = 300.0,
    ) -> None:
        self._recognizer = recognizer or _DefaultNomeroffRecognizer(
            upscale=upscale, retry_after_s=retry_after_s)
        self._lock = threading.Lock()

    # --- готовность (для диагностики и прогрева при старте) ---
    def warmup(self) -> bool:
        """Загрузить модели заранее (блокирующе). True — движок готов."""
        load = getattr(self._recognizer, "load", None)
        return bool(load()) if callable(load) else True

    def is_ready(self) -> bool:
        return bool(getattr(self._recognizer, "ready", True))

    def state(self) -> str:
        return str(getattr(self._recognizer, "state", "готов"))

    def read_text(self, plate_image) -> OcrResult:
        try:
            with self._lock:
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
