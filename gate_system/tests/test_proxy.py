"""Тесты работы с proxy6: URL, клиент API, менеджер ротации и лимиты.

Реальные запросы и деньги не используются — HTTP и часы инъектируются.
"""
import json

import pytest

from proxy import build_proxy_url, Proxy6Client, Proxy6Error, ProxyManager

NOW = 1_000_000_000
DAY = 86400


def proxy_dict(host="1.2.3.4", port="8000", end_days=20, pid="15"):
    return {
        "id": pid, "host": host, "port": port, "user": "u", "pass": "p",
        "type": "socks", "active": "1",
        "unixtime_end": NOW + end_days * DAY,
        "date_end": "2026-08-15 00:00:00",
    }


# ------------------------------ build_proxy_url ------------------------------
class TestBuildUrl:
    def test_socks(self):
        assert build_proxy_url("1.2.3.4", "8000", "u", "p", "socks") == "socks5://u:p@1.2.3.4:8000"

    def test_auto_is_socks5(self):
        assert build_proxy_url("h", 7, "u", "p", "auto").startswith("socks5://")

    def test_http(self):
        assert build_proxy_url("h", 7, "u", "p", "http") == "http://u:p@h:7"


# ------------------------------ Proxy6Client ------------------------------
class TestProxy6Client:
    def test_buy_parses_list(self):
        captured = {}

        def fake_get(url):
            captured["url"] = url
            return {"status": "yes", "balance": "42.5",
                    "list": {"15": proxy_dict(host="9.9.9.9")}}

        client = Proxy6Client("KEY", http_get=fake_get)
        item, balance = client.buy(4, "nl", 30, "gate_system")
        assert item["host"] == "9.9.9.9" and balance == "42.5"
        assert "buy" in captured["url"] and "version=4" in captured["url"]
        assert "country=nl" in captured["url"] and "KEY" in captured["url"]

    def test_error_raises(self):
        def fake_get(url):
            return {"status": "no", "error_id": 400, "error": "Error no money"}

        with pytest.raises(Proxy6Error):
            Proxy6Client("KEY", http_get=fake_get).buy(4, "nl", 30, "d")

    def test_list_active(self):
        def fake_get(url):
            return {"status": "yes", "list": {"11": proxy_dict(), "12": proxy_dict(pid="12")}}
        got = Proxy6Client("KEY", http_get=fake_get).list_proxies(descr="gate_system")
        assert len(got) == 2


# ------------------------------ FakeClient ------------------------------
class FakeClient:
    def __init__(self, proxies=None, bought=None, buy_error=None):
        self._proxies = proxies if proxies is not None else []
        self._bought = bought
        self._buy_error = buy_error
        self.buys = 0

    def list_proxies(self, descr=None, state="active"):
        return list(self._proxies)

    def buy(self, version, country, period, descr):
        self.buys += 1
        if self._buy_error:
            raise self._buy_error
        return self._bought, "100.0"

    def check(self, pid):
        return True


def make_manager(client, tmp_path, **kw):
    return ProxyManager(client, version=4, country="nl", period_days=30,
                        buy_before_days=3, max_buys_per_30d=3, cooldown_hours=24,
                        state_path=str(tmp_path / "proxy_state.json"),
                        clock=lambda: NOW, **kw)


class TestProxyManager:
    def test_fresh_proxy_no_buy(self, tmp_path):
        client = FakeClient(proxies=[proxy_dict(host="5.5.5.5", end_days=20)])
        dec = make_manager(client, tmp_path).ensure()
        assert client.buys == 0
        assert dec.purchased is False
        assert dec.url == "socks5://u:p@5.5.5.5:8000"

    def test_near_expiry_buys_new(self, tmp_path):
        client = FakeClient(
            proxies=[proxy_dict(host="old", end_days=2)],       # истекает через 2 дня (<3)
            bought=proxy_dict(host="new", end_days=30, pid="99"),
        )
        dec = make_manager(client, tmp_path).ensure()
        assert client.buys == 1 and dec.purchased is True
        assert dec.url == "socks5://u:p@new:8000"               # переключились на новый

    def test_no_proxy_buys(self, tmp_path):
        client = FakeClient(proxies=[], bought=proxy_dict(host="new"))
        dec = make_manager(client, tmp_path).ensure()
        assert client.buys == 1 and dec.url == "socks5://u:p@new:8000"

    def test_monthly_limit_blocks_buy(self, tmp_path):
        state = tmp_path / "proxy_state.json"
        state.write_text(json.dumps({"purchases": [NOW - 5 * DAY, NOW - 10 * DAY, NOW - 15 * DAY]}))
        client = FakeClient(proxies=[proxy_dict(host="old", end_days=1)],
                            bought=proxy_dict(host="new"))
        dec = make_manager(client, tmp_path).ensure()
        assert client.buys == 0                                 # лимит 3/30д исчерпан
        assert dec.url == "socks5://u:p@old:8000"               # остаёмся на старом

    def test_cooldown_blocks_buy(self, tmp_path):
        state = tmp_path / "proxy_state.json"
        state.write_text(json.dumps({"purchases": [NOW - 3600]}))  # покупка час назад
        client = FakeClient(proxies=[proxy_dict(host="old", end_days=1)],
                            bought=proxy_dict(host="new"))
        dec = make_manager(client, tmp_path).ensure()
        assert client.buys == 0                                 # кулдаун 24ч

    def test_buy_error_keeps_old(self, tmp_path):
        client = FakeClient(proxies=[proxy_dict(host="old", end_days=1)],
                            buy_error=Proxy6Error(400, "no money"))
        dec = make_manager(client, tmp_path).ensure()
        assert dec.purchased is False
        assert dec.url == "socks5://u:p@old:8000"               # ошибка покупки не роняет

    def test_purchase_recorded(self, tmp_path):
        client = FakeClient(proxies=[], bought=proxy_dict(host="new"))
        mgr = make_manager(client, tmp_path)
        mgr.ensure()
        saved = json.loads((tmp_path / "proxy_state.json").read_text())
        assert len(saved["purchases"]) == 1
