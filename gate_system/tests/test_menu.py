"""Тесты кнопочного меню (menu.py) поверх CommandRouter."""
import json

import pytest

from menu import (
    MenuController,
    BTN_OPEN, BTN_PHOTO, BTN_STATUS, BTN_HISTORY, BTN_LIST,
    BTN_LOGS, BTN_SETTINGS, BTN_ADMINS,
)
from telegram_bot import CommandRouter
from whitelist import JsonWhitelistRepository
from admins import AdminStore
from config import ConfigStore
from models import HealthStatus, PlateNumber

OWNER = 42
ADMIN = 100
STRANGER = 7


@pytest.fixture()
def ctx(tmp_path):
    allowed = tmp_path / "allowed.json"
    allowed.write_text('{"allowed": ["А123ВС777"]}', encoding="utf-8")
    logs = tmp_path / "events.log"
    logs.write_text("2026-07-17 00:24:34 | 🚗 Ворота открыты — А123ВС777\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"relay": {"pulse_ms": 500}}), encoding="utf-8")

    admin_store = AdminStore(str(tmp_path / "admins.json"))
    admin_store.add(ADMIN)
    opened = []
    router = CommandRouter(
        owner_id=OWNER,
        whitelist=JsonWhitelistRepository(str(allowed)),
        gate_open=lambda src: opened.append(src),
        status_provider=lambda: HealthStatus(camera_ok=True),
        history_provider=lambda limit: [],
        photo_provider=lambda: b"JPEG",
        config_store=ConfigStore(str(cfg)),
        logs_path=str(logs),
        admin_store=admin_store,
    )
    return {"menu": MenuController(router), "opened": opened, "admin_store": admin_store,
            "router": router}


class TestKeyboard:
    def test_owner_keyboard_has_admin_and_settings(self, ctx):
        flat = [b for row in ctx["menu"].main_keyboard(OWNER) for b in row]
        assert BTN_SETTINGS in flat and BTN_ADMINS in flat and BTN_LOGS in flat

    def test_admin_keyboard_limited(self, ctx):
        flat = [b for row in ctx["menu"].main_keyboard(ADMIN) for b in row]
        assert BTN_OPEN in flat and BTN_STATUS in flat and BTN_LIST in flat
        assert BTN_SETTINGS not in flat and BTN_ADMINS not in flat and BTN_LOGS not in flat

    def test_start_denied_for_stranger(self, ctx):
        assert "запрещён" in ctx["menu"].start(STRANGER).text

    def test_start_shows_main(self, ctx):
        r = ctx["menu"].start(OWNER)
        assert r.keyboard == "main"


class TestButtons:
    def test_open_button(self, ctx):
        r = ctx["menu"].handle_text(ADMIN, BTN_OPEN)
        assert "открыва" in r.text.lower()
        assert ctx["opened"] == ["telegram"]

    def test_photo_button(self, ctx):
        r = ctx["menu"].handle_text(ADMIN, BTN_PHOTO)
        assert r.photo == b"JPEG"

    def test_status_button(self, ctx):
        assert "Статус" in ctx["menu"].handle_text(ADMIN, BTN_STATUS).text

    def test_list_button_has_inline(self, ctx):
        r = ctx["menu"].handle_text(ADMIN, BTN_LIST)
        assert "А123ВС777" in r.text
        labels = [b[0] for row in r.inline for b in row]
        assert "➕ Добавить" in labels and "❌ Удалить" in labels

    def test_logs_button_owner(self, ctx):
        assert "событ" in ctx["menu"].handle_text(OWNER, BTN_LOGS).text.lower()

    def test_settings_button_owner_inline(self, ctx):
        r = ctx["menu"].handle_text(OWNER, BTN_SETTINGS)
        assert any("Изменить" in b[0] for row in r.inline for b in row)

    def test_admins_button_owner_inline(self, ctx):
        r = ctx["menu"].handle_text(OWNER, BTN_ADMINS)
        assert any("Добавить" in b[0] for row in r.inline for b in row)

    def test_unknown_text_shows_menu(self, ctx):
        r = ctx["menu"].handle_text(ADMIN, "абырвалг")
        assert r.keyboard == "main"

    def test_stranger_denied(self, ctx):
        assert "запрещён" in ctx["menu"].handle_text(STRANGER, BTN_OPEN).text


class TestWhitelistFlow:
    def test_add_plate_flow(self, ctx):
        m = ctx["menu"]
        r1 = m.handle_callback(OWNER, "wl_add")
        assert "номер" in r1.text.lower()          # попросил ввести
        r2 = m.handle_text(OWNER, "м001мм77")       # пользователь ввёл
        assert "М001ММ77" in r2.text
        assert "М001ММ77" in ctx["router"].list_plates()

    def test_remove_plate_flow(self, ctx):
        m = ctx["menu"]
        r1 = m.handle_callback(OWNER, "wl_del")
        # среди inline-кнопок есть удаление существующего номера
        datas = [b[1] for row in r1.inline for b in row]
        assert "wldel:А123ВС777" in datas
        r2 = m.handle_callback(OWNER, "wldel:А123ВС777")
        assert "Удалён" in r2.text
        assert "А123ВС777" not in ctx["router"].list_plates()


class TestAdminFlow:
    def test_add_admin_flow(self, ctx):
        m = ctx["menu"]
        m.handle_callback(OWNER, "adm_add")
        r = m.handle_text(OWNER, "555")
        assert "555" in r.text
        assert ctx["admin_store"].contains(555)

    def test_remove_admin_flow(self, ctx):
        m = ctx["menu"]
        r1 = m.handle_callback(OWNER, "adm_del")
        datas = [b[1] for row in r1.inline for b in row]
        assert f"admdel:{ADMIN}" in datas
        m.handle_callback(OWNER, f"admdel:{ADMIN}")
        assert not ctx["admin_store"].contains(ADMIN)

    def test_admin_cannot_open_admins_menu(self, ctx):
        # У админа этой кнопки нет, но даже прямой текст должен быть отклонён.
        assert "запрещён" in ctx["menu"].handle_text(ADMIN, BTN_ADMINS).text


class TestSettingsFlow:
    def test_set_flow(self, ctx):
        m = ctx["menu"]
        m.handle_callback(OWNER, "set_edit")
        r = m.handle_text(OWNER, "relay.pulse_ms 700")
        assert "relay.pulse_ms = 700" in r.text


class TestUnknownCarCallback:
    def test_open_button_from_notification(self, ctx):
        r = ctx["menu"].handle_callback(OWNER, "open:А123ВС777")
        assert "открыт" in r.text.lower()
        assert ctx["opened"] == ["telegram"]

    def test_ignore_button(self, ctx):
        assert "роигнор" in ctx["menu"].handle_callback(OWNER, "ignore").text
