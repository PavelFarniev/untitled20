"""Тесты перенесённого в бота функционала: ConfigStore, tail_lines, команды."""
import json

import pytest

from config import ConfigStore
from utils import tail_lines
from telegram_bot import CommandRouter
from whitelist import JsonWhitelistRepository
from models import HealthStatus

OWNER = 42
STRANGER = 7


# --------------------------------- ConfigStore ---------------------------------
class TestConfigStore:
    def test_display_masks_token(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "relay": {"backend": "stub", "pulse_ms": 500},
            "telegram": {"enabled": True, "token": "SECRET123", "owner_id": 42},
        }), encoding="utf-8")
        store = ConfigStore(str(cfg))
        out = store.as_display()
        assert "relay.pulse_ms = 500" in out
        assert "SECRET123" not in out and "telegram.token = ***" in out

    def test_set_int_persists(self, tmp_path):
        cfg = tmp_path / "config.json"
        store = ConfigStore(str(cfg))
        msg = store.set("relay.pulse_ms", "700")
        assert "relay.pulse_ms = 700" in msg
        # Значение реально сохранено на диск и имеет тип int.
        assert json.loads(cfg.read_text())["relay"]["pulse_ms"] == 700

    def test_set_bool(self, tmp_path):
        store = ConfigStore(str(tmp_path / "config.json"))
        store.set("relay.active_high", "false")
        assert store.raw()["relay"]["active_high"] is False

    def test_set_restart_key_warns(self, tmp_path):
        store = ConfigStore(str(tmp_path / "config.json"))
        msg = store.set("camera.rtsp_url", "rtsp://x/stream")
        assert "перезапуск" in msg

    def test_set_unknown_key_raises(self, tmp_path):
        store = ConfigStore(str(tmp_path / "config.json"))
        with pytest.raises(KeyError):
            store.set("relay.nonexistent", "1")

    def test_set_protected_section_raises(self, tmp_path):
        store = ConfigStore(str(tmp_path / "config.json"))
        with pytest.raises(KeyError):
            store.set("paths.allowed", "/etc/passwd")   # paths не редактируется

    def test_set_bad_int_raises(self, tmp_path):
        store = ConfigStore(str(tmp_path / "config.json"))
        with pytest.raises(ValueError):
            store.set("relay.pulse_ms", "abc")


# --------------------------------- tail_lines ---------------------------------
class TestTailLines:
    def test_missing_file(self, tmp_path):
        assert tail_lines(str(tmp_path / "no.log"), 10) == []

    def test_returns_last_n(self, tmp_path):
        log = tmp_path / "app.log"
        log.write_text("\n".join(f"line{i}" for i in range(100)), encoding="utf-8")
        out = tail_lines(str(log), 5)
        assert out == ["line95", "line96", "line97", "line98", "line99"]

    def test_fewer_lines_than_requested(self, tmp_path):
        log = tmp_path / "app.log"
        log.write_text("a\nb\n", encoding="utf-8")
        assert tail_lines(str(log), 10) == ["a", "b"]


# ------------------------------- команды бота -------------------------------
@pytest.fixture()
def router(tmp_path):
    allowed = tmp_path / "allowed.json"
    allowed.write_text('{"allowed": ["А123ВС777"]}', encoding="utf-8")
    logs = tmp_path / "app.log"
    logs.write_text("2026-07-13 | INFO | старт\n2026-07-13 | INFO | кадр\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"relay": {"backend": "stub", "pulse_ms": 500}}), encoding="utf-8")

    return CommandRouter(
        owner_id=OWNER,
        whitelist=JsonWhitelistRepository(str(allowed)),
        gate_open=lambda src: None,
        status_provider=lambda: HealthStatus(),
        history_provider=lambda limit: [],
        photo_provider=lambda: None,
        config_store=ConfigStore(str(cfg)),
        logs_path=str(logs),
    )


class TestLogsCommand:
    def test_owner_sees_logs(self, router):
        out = router.cmd_logs(OWNER, 10)
        assert "старт" in out and "кадр" in out

    def test_stranger_denied(self, router):
        assert "запрещён" in router.cmd_logs(STRANGER)


class TestSettingsCommands:
    def test_settings_view(self, router):
        assert "relay.pulse_ms = 500" in router.cmd_settings(OWNER)

    def test_settings_stranger_denied(self, router):
        assert "запрещён" in router.cmd_settings(STRANGER)

    def test_set_valid(self, router):
        assert "relay.pulse_ms = 700" in router.cmd_set(OWNER, "relay.pulse_ms", "700")

    def test_set_unknown_key_friendly_error(self, router):
        assert "❌" in router.cmd_set(OWNER, "relay.foo", "1")

    def test_set_bad_value_friendly_error(self, router):
        assert "Неверное значение" in router.cmd_set(OWNER, "relay.pulse_ms", "xx")

    def test_set_empty_shows_usage(self, router):
        assert "Формат" in router.cmd_set(OWNER, "", "")

    def test_set_stranger_denied(self, router):
        assert "запрещён" in router.cmd_set(STRANGER, "relay.pulse_ms", "700")
