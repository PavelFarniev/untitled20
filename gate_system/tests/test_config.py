"""Юнит-тесты конфигурации (config.py)."""
import json
from pathlib import Path

from gate.config import (
    Config,
    CameraConfig,
    RelayConfig,
)


class TestConfigDefaults:
    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = Config.load(tmp_path / "not_exists.json")
        assert isinstance(cfg, Config)
        assert cfg.relay.enabled is False          # по умолчанию — тестовый режим реле
        assert cfg.relay.pulse_ms == 500
        assert cfg.access.antireplay_window_s == 30
        assert cfg.log_level == "INFO"

    def test_default_sections_types(self):
        cfg = Config()
        assert isinstance(cfg.camera, CameraConfig)
        assert isinstance(cfg.relay, RelayConfig)
        assert cfg.ocr.upscale == 1.0


class TestConfigLoad:
    def test_partial_json_merges_with_defaults(self, tmp_path):
        # В файле задано только включение реле — остальное берётся из дефолтов.
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"relay": {"enabled": True, "gpio_pin": 23}}),
                     encoding="utf-8")
        cfg = Config.load(p)
        assert cfg.relay.enabled is True
        assert cfg.relay.gpio_pin == 23
        assert cfg.relay.pulse_ms == 500          # дефолт сохранён
        assert cfg.camera.rtsp_url.startswith("rtsp://")  # секция camera по умолчанию

    def test_full_roundtrip_to_dict(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"log_level": "DEBUG", "access": {"antireplay_window_s": 45}}),
                     encoding="utf-8")
        cfg = Config.load(p)
        d = cfg.to_dict()
        assert d["log_level"] == "DEBUG"
        assert d["access"]["antireplay_window_s"] == 45
        assert "camera" in d and "relay" in d

    def test_paths_section(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"paths": {"photos_dir": "/data/photos"}}),
                     encoding="utf-8")
        cfg = Config.load(p)
        assert cfg.paths.photos_dir == "/data/photos"
        assert cfg.paths.database == "data/gate.db"  # дефолт


class TestOldConfigCompat:
    def test_old_relay_backend_key_is_ignored_not_fatal(self, tmp_path):
        """Старый конфиг (relay.backend/usb_device) не роняет запуск — ключи игнорируются."""
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"relay": {"backend": "stub", "usb_device": "", "pulse_ms": 700}}),
                     encoding="utf-8")
        cfg = Config.load(p)
        assert cfg.relay.enabled is False and cfg.relay.pulse_ms == 700


class TestPaths:
    def test_relative_paths_resolve_from_project_root(self, tmp_path, monkeypatch):
        """Относительные пути считаются от корня проекта, а не от папки запуска."""
        from gate.paths import PROJECT_ROOT, resolve, resolve_source
        monkeypatch.chdir(tmp_path)
        assert resolve("models/best.pt") == PROJECT_ROOT / "models" / "best.pt"
        assert resolve("/abs/x.db") == Path("/abs/x.db")
        assert resolve_source("rtsp://u:p@1.2.3.4/s") == "rtsp://u:p@1.2.3.4/s"
        assert resolve_source("../car.mp4") == str(PROJECT_ROOT / "../car.mp4")
