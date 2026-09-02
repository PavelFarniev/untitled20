"""Тесты координатора камер (coordinator.py) и списка камер (config)."""
import json

from coordinator import CameraCoordinator
from config import Config, CameraSpec


class TestCameraCoordinator:
    def test_not_muted_initially(self):
        c = CameraCoordinator(clock=lambda: 0.0)
        assert c.exit_muted() is False

    def test_entry_open_mutes_exit(self):
        t = [0.0]
        c = CameraCoordinator(mute_exit_after_entry_s=60, clock=lambda: t[0])
        c.note_open("entry")
        assert c.exit_muted() is True
        t[0] = 61
        assert c.exit_muted() is False   # пауза истекла

    def test_exit_open_does_not_mute(self):
        c = CameraCoordinator(clock=lambda: 0.0)
        c.note_open("exit")              # выезд не глушит сам себя
        assert c.exit_muted() is False


class TestEffectiveCameras:
    def test_single_default(self):
        specs = Config().effective_cameras()
        assert len(specs) == 1
        assert specs[0].role == "entry"

    def test_from_cameras_list(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "cameras": [
                {"name": "entry", "role": "entry", "rtsp_url": "rtsp://a/1"},
                {"name": "exit", "role": "exit", "rtsp_url": "rtsp://b/2", "require_motion": True},
            ]
        }), encoding="utf-8")
        specs = Config.load(cfg_file).effective_cameras()
        assert [s.name for s in specs] == ["entry", "exit"]
        assert specs[1].role == "exit" and specs[1].require_motion is True

    def test_single_camera_inherits_motion_flag(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "camera": {"rtsp_url": "rtsp://x/1"},
            "detection": {"require_motion": True},
        }), encoding="utf-8")
        specs = Config.load(cfg_file).effective_cameras()
        assert len(specs) == 1 and specs[0].require_motion is True
