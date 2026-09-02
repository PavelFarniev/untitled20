"""Юнит-тесты конфигурации (config.py)."""
import json

from config import (
    Config,
    CameraConfig,
    RelayConfig,
)


class TestConfigDefaults:
    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = Config.load(tmp_path / "not_exists.json")
        assert isinstance(cfg, Config)
        assert cfg.relay.backend == "stub"
        assert cfg.relay.pulse_ms == 500
        assert cfg.access.antireplay_window_s == 30
        assert cfg.log_level == "INFO"

    def test_default_sections_types(self):
        cfg = Config()
        assert isinstance(cfg.camera, CameraConfig)
        assert isinstance(cfg.relay, RelayConfig)
        assert cfg.ocr.languages == ["en", "ru"]


class TestConfigLoad:
    def test_partial_json_merges_with_defaults(self, tmp_path):
        # В файле задан только backend реле — остальное берётся из дефолтов.
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"relay": {"backend": "gpio", "gpio_pin": 23}}),
                     encoding="utf-8")
        cfg = Config.load(p)
        assert cfg.relay.backend == "gpio"
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
        assert cfg.paths.allowed == "config/allowed.json"  # дефолт
