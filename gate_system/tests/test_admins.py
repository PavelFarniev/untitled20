"""Тесты двухуровневого доступа: AdminStore и роли owner/admin в CommandRouter."""
import json

import pytest

from admins import AdminStore
from telegram_bot import CommandRouter
from whitelist import JsonWhitelistRepository
from config import ConfigStore
from models import HealthStatus

OWNER = 42
ADMIN = 100
STRANGER = 7


# --------------------------------- AdminStore ---------------------------------
class TestAdminStore:
    def test_add_contains_persist(self, tmp_path):
        p = tmp_path / "admins.json"
        store = AdminStore(str(p))
        store.add(100)
        assert store.contains(100) is True
        assert AdminStore(str(p)).contains(100) is True   # прочитано с диска

    def test_add_idempotent(self, tmp_path):
        store = AdminStore(str(tmp_path / "admins.json"))
        store.add(100)
        store.add(100)
        assert store.list_ids() == [100]

    def test_remove(self, tmp_path):
        store = AdminStore(str(tmp_path / "admins.json"))
        store.add(100)
        store.remove(100)
        assert store.contains(100) is False


# --------------------------------- fixture ---------------------------------
@pytest.fixture()
def ctx(tmp_path):
    allowed = tmp_path / "allowed.json"
    allowed.write_text('{"allowed": ["А123ВС777"]}', encoding="utf-8")
    logs = tmp_path / "app.log"
    logs.write_text("line\n", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"relay": {"pulse_ms": 500}}), encoding="utf-8")

    admin_store = AdminStore(str(tmp_path / "admins.json"))
    admin_store.add(ADMIN)

    opened = []
    router = CommandRouter(
        owner_id=OWNER,
        whitelist=JsonWhitelistRepository(str(allowed)),
        gate_open=lambda src: opened.append(src),
        status_provider=lambda: HealthStatus(),
        history_provider=lambda limit: [],
        photo_provider=lambda: None,
        config_store=ConfigStore(str(cfg)),
        logs_path=str(logs),
        admin_store=admin_store,
    )
    return {"router": router, "opened": opened, "admin_store": admin_store}


# ------------------------------- роли -------------------------------
class TestRoles:
    def test_role_predicates(self, ctx):
        r = ctx["router"]
        assert r.is_owner(OWNER) and r.is_admin(OWNER)
        assert r.is_admin(ADMIN) and not r.is_owner(ADMIN)
        assert not r.is_admin(STRANGER)


class TestAdminCanUse:
    def test_admin_can_open(self, ctx):
        assert "открыва" in ctx["router"].cmd_open(ADMIN).lower()
        assert ctx["opened"] == ["telegram"] or ctx["opened"]  # ворота открылись

    def test_admin_can_status_history_list_photo(self, ctx):
        r = ctx["router"]
        assert "Статус" in r.cmd_status(ADMIN)
        assert r.cmd_history(ADMIN)
        assert "А123ВС777" in r.cmd_list(ADMIN)
        assert r.cmd_photo(ADMIN).caption  # не «Доступ запрещён»

    def test_admin_can_press_open_button(self, ctx):
        assert "открыт" in ctx["router"].on_callback(ADMIN, "open:X").lower()


class TestAdminCannotManage:
    def test_admin_cannot_edit_whitelist(self, ctx):
        r = ctx["router"]
        assert "запрещён" in r.cmd_add(ADMIN, "М001ММ77")
        assert "запрещён" in r.cmd_remove(ADMIN, "А123ВС777")

    def test_admin_cannot_settings_logs_set(self, ctx):
        r = ctx["router"]
        assert "запрещён" in r.cmd_settings(ADMIN)
        assert "запрещён" in r.cmd_logs(ADMIN)
        assert "запрещён" in r.cmd_set(ADMIN, "relay.pulse_ms", "700")

    def test_admin_cannot_manage_admins(self, ctx):
        r = ctx["router"]
        assert "запрещён" in r.cmd_addadmin(ADMIN, "555")
        assert "запрещён" in r.cmd_removeadmin(ADMIN, "555")
        assert "запрещён" in r.cmd_admins(ADMIN)


class TestOwnerManagesAdmins:
    def test_owner_adds_admin_and_they_can_open(self, ctx):
        r = ctx["router"]
        assert "добавлен" in r.cmd_addadmin(OWNER, "555").lower()
        assert ctx["admin_store"].contains(555)
        assert "открыва" in r.cmd_open(555).lower()   # новый админ уже может открыть

    def test_owner_removes_admin(self, ctx):
        r = ctx["router"]
        r.cmd_removeadmin(OWNER, str(ADMIN))
        assert not ctx["admin_store"].contains(ADMIN)
        assert "запрещён" in r.cmd_open(ADMIN)         # доступ отозван

    def test_addadmin_invalid_id(self, ctx):
        assert "Некорректный" in ctx["router"].cmd_addadmin(OWNER, "abc")

    def test_addadmin_owner_itself(self, ctx):
        assert "владелец" in ctx["router"].cmd_addadmin(OWNER, str(OWNER)).lower()

    def test_admins_listing(self, ctx):
        out = ctx["router"].cmd_admins(OWNER)
        assert str(OWNER) in out and str(ADMIN) in out


class TestStrangerBlocked:
    def test_stranger_denied_everywhere(self, ctx):
        r = ctx["router"]
        assert "запрещён" in r.cmd_open(STRANGER)
        assert "запрещён" in r.cmd_status(STRANGER)
        assert "запрещён" in r.on_callback(STRANGER, "open:X")
