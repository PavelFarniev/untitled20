"""Автоматическая покупка и ротация прокси через API proxy6.net.

Задача: держать бота Telegram на рабочем прокси и раз в ~месяц брать НОВЫЙ прокси
(новый IP), покупая его заранее — за несколько дней до окончания текущего, чтобы
не было простоя. Продление старого не используем (нужен свежий IP).

Безопасность (обязательно для автопокупки за реальные деньги):
* не больше N покупок за 30 дней (жёсткий лимит от бага в цикле);
* кулдаун между покупками;
* не покупаем, если действующий прокси ещё «свежий»;
* история покупок хранится на диске (лимит переживает перезапуски);
* API-ключ маскируется в логах.

Архитектура — порт/адаптер: Proxy6Client общается с API, ProxyManager содержит
чистую логику ротации и лимитов. И то, и другое тестируется без реальных запросов
и без реальных денег (HTTP и часы инъектируются).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Callable, Dict, List, Optional

import database

log = logging.getLogger("gate_system.proxy")

_API_BASE = "https://px6.link/api"
_DAY = 86400


def build_proxy_url(host: str, port, user: str, password: str, ptype: str) -> str:
    """Собрать URL прокси. type: socks/auto -> socks5, http -> http."""
    scheme = "socks5" if ptype in ("socks", "auto") else "http"
    return f"{scheme}://{user}:{password}@{host}:{port}"


def _default_http_get(url: str) -> dict:
    """Разовый GET с JSON-ответом (для реальных запросов к proxy6)."""
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Proxy6Error(Exception):
    """Ошибка API proxy6."""

    def __init__(self, code, message: str) -> None:
        super().__init__(f"proxy6 error {code}: {message}")
        self.code = code
        self.message = message


class Proxy6Client:
    """Тонкая обёртка над API proxy6.net (только нужные методы)."""

    def __init__(self, api_key: str, http_get: Callable[[str], dict] = _default_http_get) -> None:
        self._key = api_key
        self._get = http_get

    def _call(self, method: str, params: str = "") -> dict:
        url = f"{_API_BASE}/{self._key}/{method}"
        if params:
            url += f"?{params}"
        data = self._get(url)
        if data.get("status") != "yes":
            raise Proxy6Error(data.get("error_id"), data.get("error", "unknown"))
        return data

    def list_proxies(self, descr: Optional[str] = None, state: str = "active") -> List[dict]:
        params = f"state={state}"
        if descr:
            params += f"&descr={descr}"
        data = self._call("getproxy", params)
        return list(data.get("list", {}).values())

    def buy(self, version: int, country: str, period: int, descr: str) -> tuple:
        """Купить 1 прокси. Возвращает (данные_прокси, баланс)."""
        params = f"count=1&period={period}&country={country}&version={version}&descr={descr}"
        data = self._call("buy", params)
        items = list(data.get("list", {}).values())
        if not items:
            raise Proxy6Error("empty", "buy вернул пустой список")
        return items[0], data.get("balance")

    def check(self, proxy_id) -> bool:
        data = self._call("check", f"ids={proxy_id}")
        return bool(data.get("proxy_status"))


class ProxyManager:
    """Ротация прокси: выбирает действующий, заранее покупает новый, следит за лимитами."""

    def __init__(
        self,
        client: Proxy6Client,
        version: int = 4,
        country: str = "nl",
        period_days: int = 30,
        buy_before_days: int = 3,
        max_buys_per_30d: int = 3,
        cooldown_hours: float = 24.0,
        descr: str = "gate_system",
        state_path: str = "config/proxy_state.json",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._version = version
        self._country = country
        self._period = period_days
        self._buy_before_s = buy_before_days * _DAY
        self._max_buys = max_buys_per_30d
        self._cooldown_s = cooldown_hours * 3600
        self._descr = descr
        self._state_path = state_path
        self._clock = clock

    # ------------------------------- лимиты -------------------------------
    def _purchases(self) -> List[float]:
        data = database.read_json(self._state_path, {"purchases": []})
        return [float(t) for t in data.get("purchases", [])]

    def _record_purchase(self, now: float) -> None:
        purchases = self._purchases()
        purchases.append(now)
        # Держим только последние 30 дней, чтобы файл не рос.
        purchases = [t for t in purchases if now - t < 30 * _DAY]
        database.write_json_atomic(self._state_path, {"purchases": purchases})

    def _can_buy(self, now: float) -> bool:
        recent = [t for t in self._purchases() if now - t < 30 * _DAY]
        if len(recent) >= self._max_buys:
            log.warning("Лимит покупок прокси исчерпан (%d за 30 дней) — не покупаю", self._max_buys)
            return False
        if recent and (now - max(recent)) < self._cooldown_s:
            log.info("Кулдаун покупки прокси ещё не истёк — не покупаю")
            return False
        return True

    # ------------------------------- ротация -------------------------------
    @staticmethod
    def _freshest(proxies: List[dict]) -> Optional[dict]:
        active = [p for p in proxies if str(p.get("active")) == "1"]
        if not active:
            return None
        return max(active, key=lambda p: int(p.get("unixtime_end", 0)))

    def ensure(self, now: Optional[float] = None) -> "ProxyDecision":
        """Гарантировать рабочий прокси. Вернуть решение (URL + факт покупки)."""
        now = now if now is not None else self._clock()
        current = self._freshest(self._client.list_proxies(descr=self._descr, state="active"))

        if current is None:
            need = True
        else:
            try:
                need = (int(current["unixtime_end"]) - now) < self._buy_before_s
            except (KeyError, TypeError, ValueError):
                # Срок неизвестен/битый — НЕ тратим деньги, используем прокси как есть.
                log.warning("У прокси нет корректного срока действия — покупку не инициирую")
                need = False
        purchased = False
        if need and self._can_buy(now):
            try:
                bought, balance = self._client.buy(self._version, self._country,
                                                   self._period, self._descr)
                self._record_purchase(now)
                current = bought
                purchased = True
                log.info("Куплен новый прокси до %s, баланс: %s",
                         bought.get("date_end"), balance)
            except Proxy6Error as e:
                log.error("Не удалось купить прокси: %s", e)

        url = None
        if current:
            url = build_proxy_url(current["host"], current["port"],
                                  current["user"], current["pass"], current.get("type", "socks"))
        return ProxyDecision(url=url, purchased=purchased,
                             expires=current.get("date_end") if current else None)


class ProxyDecision:
    """Результат ensure(): текущий URL прокси и была ли покупка."""

    def __init__(self, url: Optional[str], purchased: bool, expires: Optional[str]) -> None:
        self.url = url
        self.purchased = purchased
        self.expires = expires
