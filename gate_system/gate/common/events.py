"""Журнал событий на понятном человеку языке.

В отличие от технического лога (gate_system.log), сюда попадают ТОЛЬКО значимые
для обычного пользователя события простыми словами: ворота открыты, неизвестная
машина, камера пропала/вернулась, интернет, запуск/остановка. Именно этот журнал
показывается в Telegram по команде /logs.

Записи идут в отдельный logger 'gate_system.events', для которого в logger.py
настроен отдельный файл events.log.
"""
from __future__ import annotations

import logging

_events = logging.getLogger("gate_system.events")


def event(message: str) -> None:
    """Записать понятное событие в пользовательский журнал."""
    _events.info(message)
