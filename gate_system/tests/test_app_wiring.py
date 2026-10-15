"""Сквозной тест сборки приложения (gate/app.py build_app).

Собираем НАСТОЯЩИЙ граф: камера из видеофайла (через фоновый захват), SQLite,
реле в тестовом режиме, адаптивный пайплайн,
агрегатор, решение о доступе, фото. Подменяются только нейросети (YOLO и
распознаватель), которых нет в тестовом окружении.

Ожидание: машина из белого списка → ворота открыты (тестовый режим реле),
событие записано в базу, фото сохранено, статус собран по каждой камере.
"""
import json
import time

import cv2
import numpy as np
import pytest

from gate.config import Config
from gate.core.models import BBox, CheckResult, OcrResult, PlateNumber


class FakeDetector:
    def __init__(self, *a, **kw):
        self.has_plate_model = True


    def detect_plate(self, frame):
        return [BBox(10, 10, 50, 30, 0.9)]


class FakeOcr:
    def warmup(self):
        return True

    def is_ready(self):
        return True

    def state(self):
        return "готов"

    def read_text(self, img):
        return OcrResult(PlateNumber("А123ВС777"), 1.0, "А123ВС777")


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    video = tmp_path / "cam.avi"
    w = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 25.0, (96, 64))
    for i in range(50):
        w.write(np.full((64, 96, 3), (i * 5) % 255, np.uint8))
    w.release()

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "camera": {"rtsp_url": str(video), "process_every_n_frame": 1},
        "detection": {"adaptive": True, "idle_interval_s": 0.05, "plate_model_path": "x.pt"},
        "relay": {"enabled": False, "pulse_ms": 1},
        "telegram": {"enabled": False},
        "access": {"confirm_frames": 2, "antireplay_window_s": 30},
        "paths": {"database": str(tmp_path / "test.db"), "photos_dir": str(tmp_path / "photos"),
                  "logs_dir": str(tmp_path / "logs")},
    }), encoding="utf-8")

    # Своя машина в белом списке (база создаётся заранее, как её заполнил бы бот).
    from helpers import make_repos
    make_repos(tmp_path, plates=["А123ВС777"])
    import gate.vision.detector as det_mod
    import gate.vision.ocr as ocr_mod
    monkeypatch.setattr(det_mod, "YoloDetector", FakeDetector)
    monkeypatch.setattr(ocr_mod, "build_ocr_engine", lambda cfg: FakeOcr())
    # Без реальной сети в тестах.
    import gate.services.network as net_mod
    monkeypatch.setattr(net_mod, "default_probe", lambda *a, **k: False)
    return Config.load(cfg_path), tmp_path


def test_full_system_opens_gate_for_whitelisted_car(app_env):
    from gate.app import build_app
    from gate.storage.database import SqliteDatabase
    from gate.storage.history import SqliteHistoryRepository

    cfg, tmp = app_env
    app = build_app(cfg)
    app.start()
    try:
        history = SqliteHistoryRepository(SqliteDatabase(tmp / "test.db"))
        deadline = time.monotonic() + 15
        events = []
        while time.monotonic() < deadline:
            events = history.recent(5)
            if events:
                break
            time.sleep(0.1)
    finally:
        app.stop()

    assert events, "система не приняла решение по номеру за 15 с"
    ev = events[0]
    assert ev.plate.value == "А123ВС777" and ev.result is CheckResult.GRANTED
    assert ev.gate_opened is True
    assert ev.photo_path and (tmp / "photos").exists()


def test_status_and_photo_from_running_system(app_env):
    from gate.app import build_app

    cfg, tmp = app_env
    app = build_app(cfg)
    app.start()
    try:
        vision = app._services["vision:camera"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and vision._source.latest() is None:
            time.sleep(0.05)
        diag = app._services["diagnostics"]
        status = diag.snapshot()
        assert status.extra["cameras"] == {"camera": "ok"}
        assert status.extra["relay_ok"] is False and "тестовый" in status.extra["relay"]
        assert status.ocr_ok is True
    finally:
        app.stop()
