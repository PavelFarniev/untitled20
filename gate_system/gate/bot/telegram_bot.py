"""Telegram-бот и уведомления (реализация Notifier).

Стек: python-telegram-bot (asyncio, кроссплатформенно). Тяжёлый импорт telegram
выполняется ЛЕНИВО в run(), поэтому модуль импортируется и тестируется без
установленного пакета.

Разделение ответственности ради тестируемости:
* CommandRouter — ЧИСТАЯ логика команд (авторизация владельца, обращение к ядру
  через колбэки/порты, формирование текста ответа). Не зависит от telegram.
* TelegramNotifier — адаптер: реализует порт Notifier (уведомления known/unknown)
  и связывает CommandRouter с реальным ботом. Отправка инъектируется (`sender`).

Управление — кнопочное меню (gate/bot/menu.py). При неизвестном авто владельцу
уходит фото + номер + время и кнопки «✅ Открыть» / «❌ Игнорировать».

Токен бота задаётся в config/config.json (telegram.token). Если он пустой, бот не
запускается, а система работает автономно (распознавание и ворота — без Telegram).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional

from gate.core.interfaces import Notifier, WhitelistRepository
from gate.core.models import Event, HealthStatus, OpenSource
from gate.core.plates import normalize_plate, split_plate_and_note
from gate.common.utils import tail_lines
from gate.common.events import event as user_event

log = logging.getLogger("gate_system.telegram")

_DENIED = "⛔ Доступ запрещён."

_RESULT_RU = {"granted": "пропущен", "denied": "не в списке", "unreadable": "не прочитан"}

_TG_LIMIT = 3800  # запас до лимита Telegram (4096) на одно сообщение


def _fmt_time(ts: datetime) -> str:
    """Время события в МЕСТНОМ часовом поясе (в базе оно хранится в UTC)."""
    if ts.tzinfo is not None:
        ts = ts.astimezone()
    return ts.strftime("%d.%m.%Y %H:%M:%S")


def prettify_event_lines(lines: List[str]) -> str:
    """Оформить понятный журнал событий (events.log) для Telegram.

    Строка формата `дата время | сообщение` превращается в `ЧЧ:ММ:СС сообщение`.
    Сообщения уже написаны простым языком со значками, поэтому дополнительно ничего
    расшифровывать не нужно.
    """
    pretty: List[str] = []
    for line in lines:
        parts = line.split(" | ", 1)
        if len(parts) == 2:
            ts, msg = parts
            time_only = ts.strip().split(" ")[-1]
            pretty.append(f"{time_only}  {msg.strip()}")
        else:
            pretty.append(line)
    text = "\n".join(pretty)
    if len(text) > _TG_LIMIT:
        text = "…\n" + text[-_TG_LIMIT:]
    return text


class _LiveSender:
    """Мост синхронный→асинхронный: шлёт сообщения владельцу из другого потока.

    Уведомления формируются в Vision-потоке, а бот живёт в asyncio-петле в другом
    потоке. Отправка планируется в эту петлю через run_coroutine_threadsafe.
    """

    def __init__(self, bot, owner_id: int, loop) -> None:
        self._bot = bot
        self._owner_id = owner_id
        self._loop = loop

    def _submit(self, coro) -> None:
        import asyncio
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def send_message(self, text: str) -> None:
        self._submit(self._bot.send_message(chat_id=self._owner_id, text=text))

    def send_photo(self, photo, caption: str, buttons) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        markup = None
        if buttons:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(text, callback_data=data) for text, data in buttons]]
            )
        if photo is None:
            self._submit(self._bot.send_message(chat_id=self._owner_id, text=caption,
                                                reply_markup=markup))
            return
        # Локальный файл нужно передать как Path/файл, иначе telegram примет строку
        # за file_id/URL и фото не отправится.
        import os
        from pathlib import Path
        if isinstance(photo, str) and os.path.exists(photo):
            photo = Path(photo)
        self._submit(self._bot.send_photo(chat_id=self._owner_id, photo=photo,
                                          caption=caption, reply_markup=markup))


@dataclass
class PhotoReply:
    """Ответ-фотография (для /photo и уведомлений о неизвестном авто)."""

    photo: Optional[object]
    caption: str
    buttons: Optional[List[tuple]] = None  # [(текст, callback_data), ...]


class CommandRouter:
    """Чистая логика обработки команд. Не импортирует telegram.

    Права: владелец (telegram.owner_id) может всё; админы — открывать ворота и
    смотреть статус/фото/историю/белый список.
    """

    def __init__(
        self,
        owner_id: int,
        whitelist: WhitelistRepository,
        gate_open: Callable[[OpenSource], object],
        status_provider: Callable[[], HealthStatus],
        history_provider: Callable[[int], List[Event]],
        photo_provider: Callable[[], Optional[object]],
        config_store: Optional[object] = None,
        logs_path: str = "logs/events.log",
        admin_store: Optional[object] = None,
        restart_request: Optional[Callable[[], None]] = None,
    ) -> None:
        self._owner_id = owner_id
        self._whitelist = whitelist
        self._gate_open = gate_open
        self._status_provider = status_provider
        self._history_provider = history_provider
        self._photo_provider = photo_provider
        self._config_store = config_store
        self._logs_path = logs_path
        self._admin_store = admin_store
        self._restart_request = restart_request

    def is_owner(self, user_id: int) -> bool:
        """Владелец — единственный супер-админ (из config.telegram.owner_id)."""
        return user_id == self._owner_id

    def is_admin(self, user_id: int) -> bool:
        """Админ — владелец ИЛИ пользователь из списка админов."""
        if self.is_owner(user_id):
            return True
        return self._admin_store is not None and self._admin_store.contains(user_id)

    # ------------------------------- ворота -------------------------------
    def cmd_open(self, user_id: int) -> str:
        if not self.is_admin(user_id):
            return _DENIED
        result = self._gate_open(OpenSource.TELEGRAM)
        if result is False:
            return "⏳ Ворота только что получили команду — повтор проигнорирован."
        log.info("Команда «Открыть ворота» (user_id=%s)", user_id)
        user_event("🔓 Ворота открыты через Telegram")
        return "✅ Ворота открываются."

    # ------------------------------- статус -------------------------------
    def cmd_status(self, user_id: int) -> str:
        if not self.is_admin(user_id):
            return _DENIED
        h = self._status_provider()
        extra = h.extra or {}
        temp = f"{h.cpu_temp_c:.0f}°C" if h.cpu_temp_c is not None else "н/д"
        lines = ["🟢 Статус системы"]

        cams = extra.get("cameras") or {}
        state_ru = {"ok": "OK", "starting": "подключается…", "down": "ОШИБКА (нет кадров)"}
        if len(cams) > 1:
            lines.append("Камеры:")
            for name, st in cams.items():
                lines.append(f"  • {name}: {state_ru.get(st, st)}")
        elif len(cams) == 1:
            st = next(iter(cams.values()))
            lines.append(f"Камера: {state_ru.get(st, st)}")
        else:
            lines.append(f"Камера: {'OK' if h.camera_ok else 'ОШИБКА'}")

        ocr_state = extra.get("ocr_state")
        lines.append(f"Распознавание: {'OK' if h.ocr_ok else 'ОШИБКА'}"
                     + (f" ({ocr_state})" if ocr_state and not h.ocr_ok else ""))
        if "relay" in extra:
            mark = "OK" if extra.get("relay_ok") else "⚠️"
            lines.append(f"Реле: {mark} — {extra['relay']}")
        lines += [
            f"Telegram: {'онлайн' if h.telegram_online else 'оффлайн'}",
            f"Память: {h.memory_percent:.0f}%",
            f"Диск свободно: {h.disk_free_percent:.0f}%",
            f"Температура CPU: {temp}",
            f"Последний номер: {h.last_plate or '—'}",
        ]
        return "\n".join(lines)

    def cmd_history(self, user_id: int, limit: int = 10) -> str:
        if not self.is_admin(user_id):
            return _DENIED
        events = self._history_provider(limit)
        if not events:
            return "История пуста."
        lines = ["🕓 Последние события:"]
        for e in events:
            mark = "✅" if e.gate_opened else "⛔"
            plate = e.plate.value if e.plate else "—"
            lines.append(f"{mark} {_fmt_time(e.timestamp)}  {plate}  ({_RESULT_RU.get(e.result.value, e.result.value)})")
        return "\n".join(lines)

    def cmd_photo(self, user_id: int) -> PhotoReply:
        if not self.is_admin(user_id):
            return PhotoReply(photo=None, caption=_DENIED)
        try:
            snap = self._photo_provider()
        except Exception:  # noqa: BLE001
            log.exception("Не удалось получить кадр с камеры")
            snap = None
        if snap is None:
            return PhotoReply(photo=None, caption="📷 Кадра пока нет — камера ещё не подключилась.")
        if isinstance(snap, tuple):
            photo, caption = snap
            return PhotoReply(photo=photo, caption=caption)
        return PhotoReply(photo=snap, caption="📷 Текущий кадр с камеры")

    # ------------------------------- белый список -------------------------------
    def _entries(self):
        lister = getattr(self._whitelist, "list_entries", None)
        return lister() if callable(lister) else [(p, "") for p in self._whitelist.list_all()]

    def cmd_list(self, user_id: int) -> str:
        if not self.is_admin(user_id):
            return _DENIED
        entries = self._entries()
        if not entries:
            return "Белый список пуст."
        rows = [f"{p.value} — {n}" if n else p.value for p, n in entries]
        return f"📋 Белый список ({len(rows)}):\n" + "\n".join(rows)

    def cmd_add(self, user_id: int, arg: str) -> str:
        """Добавить номер. Можно с подписью: «А123ВС777 Папа»."""
        if not self.is_owner(user_id):
            return _DENIED
        plate, note = split_plate_and_note(arg or "")
        if plate is None:
            return f"❌ Некорректный номер: {arg!r}. Пример: А123ВС777 Папа"
        self._whitelist.add(plate, note)
        user_event(f"📋 В белый список добавлен номер {plate.value}" + (f" ({note})" if note else ""))
        return f"✅ Добавлен номер: {plate.value}" + (f" — {note}" if note else "")

    def cmd_rename(self, user_id: int, old: str, new_text: str) -> str:
        """Исправить номер в белом списке (подпись сохраняется или заменяется новой)."""
        if not self.is_owner(user_id):
            return _DENIED
        old_plate = normalize_plate(old or "")
        new_plate, note = split_plate_and_note(new_text or "")
        if old_plate is None:
            return f"❌ Некорректный старый номер: {old!r}"
        if new_plate is None:
            return f"❌ Некорректный номер: {new_text!r}. Пример: А123ВС777 Папа"
        if not self._whitelist.rename(old_plate, new_plate):
            return f"❌ Номера {old_plate.value} уже нет в белом списке."
        if note:
            self._whitelist.add(new_plate, note)
        user_event(f"📋 Номер {old_plate.value} исправлен на {new_plate.value}")
        return f"✅ Номер исправлен: {old_plate.value} → {new_plate.value}" + (f" — {note}" if note else "")

    def cmd_remove(self, user_id: int, arg: str) -> str:
        if not self.is_owner(user_id):
            return _DENIED
        plate = normalize_plate(arg or "")
        if plate is None:
            return f"❌ Некорректный номер: {arg!r}"
        self._whitelist.remove(plate)
        user_event(f"📋 Из белого списка удалён номер {plate.value}")
        return f"✅ Удалён номер: {plate.value}"

    # ------------------------------- журнал/настройки -------------------------------
    def cmd_logs(self, user_id: int, n: int = 20) -> str:
        if not self.is_owner(user_id):
            return _DENIED
        lines = tail_lines(self._logs_path, n)
        if not lines:
            return "Журнал пуст."
        return f"📜 Последние события ({len(lines)}):\n\n" + prettify_event_lines(lines)

    def cmd_settings(self, user_id: int) -> str:
        if not self.is_owner(user_id):
            return _DENIED
        if self._config_store is None:
            return "Настройки недоступны."
        return ("⚙️ Текущие настройки:\n" + self._config_store.as_display()
                + "\n\nИзменения применяются после перезапуска системы.")

    def cmd_set(self, user_id: int, dotted_key: str, value: str) -> str:
        if not self.is_owner(user_id):
            return _DENIED
        if self._config_store is None:
            return "Изменение настроек недоступно."
        if not dotted_key or value is None or value == "":
            return "Формат: секция.ключ значение (пример: relay.pulse_ms 700)"
        try:
            return self._config_store.set(dotted_key, value)
        except KeyError as exc:
            return f"❌ {exc.args[0] if exc.args else 'неизвестный ключ'}"
        except ValueError as exc:
            return f"❌ Неверное значение: {exc}"

    def cmd_restart(self, user_id: int) -> str:
        """Перезапустить систему (применить настройки). Только владелец."""
        if not self.is_owner(user_id):
            return _DENIED
        if self._restart_request is None:
            return "Перезапуск из бота недоступен."
        user_event("🔄 Перезапуск системы по команде из Telegram")
        self._restart_request()
        return "🔄 Система перезапускается… Бот вернётся через 1–2 минуты (загрузка распознавания)."

    # --------------------------- управление админами ---------------------------
    @staticmethod
    def _parse_admin(arg: str):
        """«123456789 Мама» -> (123456789, "Мама")."""
        parts = (arg or "").strip().split(maxsplit=1)
        if not parts:
            raise ValueError
        return int(parts[0]), (parts[1].strip() if len(parts) > 1 else "")

    def cmd_addadmin(self, user_id: int, arg: str) -> str:
        if not self.is_owner(user_id):
            return _DENIED
        if self._admin_store is None:
            return "Управление админами недоступно."
        try:
            new_id, note = self._parse_admin(arg)
        except ValueError:
            return f"❌ Некорректный ID: {arg!r} (ожидается число, можно с подписью: 123456789 Мама)"
        if new_id == self._owner_id:
            return "Этот пользователь уже владелец."
        try:
            self._admin_store.add(new_id, note)
        except TypeError:  # хранилище без подписей
            self._admin_store.add(new_id)
        user_event(f"👥 Добавлен админ {new_id}" + (f" ({note})" if note else ""))
        return f"✅ Админ добавлен: {new_id}" + (f" — {note}" if note else "")

    def cmd_rename_admin(self, user_id: int, old: str, new_text: str) -> str:
        """Исправить ID админа (например, ошиблись цифрой)."""
        if not self.is_owner(user_id):
            return _DENIED
        if self._admin_store is None or not hasattr(self._admin_store, "rename"):
            return "Управление админами недоступно."
        try:
            old_id = int((old or "").strip())
            new_id, note = self._parse_admin(new_text)
        except ValueError:
            return f"❌ Некорректный ID: {new_text!r} (ожидается число)"
        if new_id == self._owner_id:
            return "Этот пользователь уже владелец."
        if not self._admin_store.rename(old_id, new_id):
            return f"❌ Админа {old_id} уже нет в списке."
        if note:
            self._admin_store.add(new_id, note)
        user_event(f"👥 ID админа исправлен: {old_id} → {new_id}")
        return f"✅ ID админа исправлен: {old_id} → {new_id}" + (f" — {note}" if note else "")

    def cmd_removeadmin(self, user_id: int, arg: str) -> str:
        if not self.is_owner(user_id):
            return _DENIED
        if self._admin_store is None:
            return "Управление админами недоступно."
        try:
            rid = int((arg or "").strip())
        except ValueError:
            return f"❌ Некорректный ID: {arg!r} (ожидается число)"
        self._admin_store.remove(rid)
        user_event(f"👥 Удалён админ {rid}")
        return f"✅ Админ удалён: {rid}"

    def _admin_entries(self):
        if self._admin_store is None:
            return []
        lister = getattr(self._admin_store, "list_entries", None)
        return lister() if callable(lister) else [(i, "") for i in self._admin_store.list_ids()]

    def cmd_admins(self, user_id: int) -> str:
        if not self.is_owner(user_id):
            return _DENIED
        entries = self._admin_entries()
        body = "\n".join(f"{i} — {n}" if n else str(i) for i, n in entries) if entries else "—"
        return f"👥 Владелец: {self._owner_id}\nАдмины:\n{body}"

    # --- Аксессоры данных для построения кнопок меню (menu.py) ---
    def list_plates(self) -> List[str]:
        return [p.value for p in self._whitelist.list_all()]

    def list_admin_ids(self) -> List[int]:
        return self._admin_store.list_ids() if self._admin_store else []

    # ------------------------- нажатия inline-кнопок -------------------------
    def on_callback(self, user_id: int, data: str) -> str:
        """Обработать нажатие кнопки под уведомлением о неизвестном авто."""
        if not self.is_admin(user_id):
            return _DENIED
        if data.startswith("open"):
            result = self._gate_open(OpenSource.TELEGRAM)
            if result is False:
                return "⏳ Ворота только что получили команду — повтор проигнорирован."
            user_event("🔓 Ворота открыты через Telegram (кнопка)")
            return "✅ Ворота открыты вручную."
        if data.startswith("ignore"):
            return "❌ Проигнорировано."
        return "Неизвестное действие."


class TelegramNotifier(Notifier):
    """Адаптер Telegram: уведомления владельцу + приём команд.

    :param sender: объект с методами send_message(text) и
        send_photo(photo, caption, buttons). Инъекция для тестов; в продакшене
        оборачивает telegram.Bot. Если None — сообщения только логируются
        (например, если токен не задан).
    """

    def __init__(
        self,
        token: str,
        owner_id: int,
        router: CommandRouter,
        sender: Optional[object] = None,
        proxy_url: str = "",
    ) -> None:
        self._token = token
        self._owner_id = owner_id
        self._router = router
        self._sender = sender
        self._proxy_url = proxy_url or None
        self._online = False
        self._app = None   # ссылка на telegram Application (для остановки)
        self._loop = None  # event loop потока Telegram

    def set_proxy(self, proxy_url: str) -> None:
        """Сменить прокси (применится при следующем (пере)запуске бота)."""
        self._proxy_url = proxy_url or None

    # ------------------------------- порт Notifier -------------------------------
    def notify_known(self, event: Event) -> None:
        plate = event.plate.value if event.plate else "—"
        text = f"✅ Ворота открыты\nНомер: {plate}\nВремя: {_fmt_time(event.timestamp)}"
        # Если есть фото события — отправляем с фото, иначе просто текст.
        if event.photo_path:
            self._safe_send_photo(event.photo_path, text, None)
        else:
            self._safe_send_message(text)

    def notify_unknown(self, event: Event) -> None:
        plate = event.plate.value if event.plate else "не распознан"
        caption = (
            "🚗 Неизвестный автомобиль\n"
            f"Номер: {plate}\n"
            f"Время: {_fmt_time(event.timestamp)}"
        )
        buttons = [("✅ Открыть", f"open:{plate}"), ("❌ Игнорировать", "ignore")]
        self._safe_send_photo(event.photo_path, caption, buttons)

    def is_online(self) -> bool:
        return self._online

    def set_online(self, value: bool) -> None:
        """Вызывается NetworkMonitor'ом при смене состояния сети."""
        self._online = value

    def request_stop(self) -> None:  # pragma: no cover - требует запущенного бота
        """Остановить бота из другого потока (graceful shutdown)."""
        if self._app is None or self._loop is None:
            return
        try:
            # stop_running() корректно завершает run_polling; планируем в его loop.
            self._loop.call_soon_threadsafe(self._app.stop_running)
        except Exception:  # noqa: BLE001
            log.debug("Ошибка при остановке Telegram-бота", exc_info=True)

    # ------------------------------- отправка -------------------------------
    def _safe_send_message(self, text: str) -> None:
        if self._sender is None:
            log.info("[telegram offline] %s", text.replace("\n", " | "))
            return
        try:
            self._sender.send_message(text)
        except Exception:  # noqa: BLE001 - нет сети/ошибка API не должны ронять ядро
            log.warning("Не удалось отправить сообщение в Telegram", exc_info=True)

    def _safe_send_photo(self, photo, caption: str, buttons) -> None:
        if self._sender is None:
            log.info("[telegram offline] PHOTO %s", caption.replace("\n", " | "))
            return
        try:
            self._sender.send_photo(photo, caption, buttons)
        except Exception:  # noqa: BLE001
            log.warning("Не удалось отправить фото в Telegram", exc_info=True)

    # ------------------------------- запуск бота -------------------------------
    def run(self) -> None:  # pragma: no cover - требует сети и токена
        """Запустить бота (python-telegram-bot). Синхронный, блокирующий вызов.

        Метод run_polling() сам управляет своим циклом событий, поэтому его нельзя
        запускать внутри asyncio.run(). Мы создаём отдельный event loop для этого
        потока и отдаём его боту. Пустой токен означает, что бот не
        стартует — система работает полностью без Telegram (режим без интернета).
        """
        if not self._token:
            log.warning("Токен Telegram не задан — бот не запущен (режим без Telegram)")
            return
        import asyncio
        # Ленивый импорт: пакет нужен только при реальном запуске.
        from telegram import (
            InlineKeyboardButton,
            InlineKeyboardMarkup,
            ReplyKeyboardMarkup,
        )
        from telegram.ext import (
            ApplicationBuilder,
            CommandHandler,
            CallbackQueryHandler,
            MessageHandler,
            filters,
        )
        from gate.bot.menu import MenuController

        # Отдельный event loop для потока Telegram (мы не в главном потоке).
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        builder = ApplicationBuilder().token(self._token)
        if self._proxy_url:
            # Прокси и для обычных запросов к API, и для long-polling getUpdates.
            builder = builder.proxy(self._proxy_url).get_updates_proxy(self._proxy_url)
            log.info("Telegram работает через прокси")
        app = builder.build()
        menu = MenuController(self._router)

        def _build_markup(resp, user_id):
            """Собрать telegram-клавиатуру из MenuResponse."""
            if resp.inline:
                rows = [[InlineKeyboardButton(t, callback_data=d) for t, d in row]
                        for row in resp.inline]
                return InlineKeyboardMarkup(rows)
            if resp.keyboard == "main":
                return ReplyKeyboardMarkup(menu.main_keyboard(user_id), resize_keyboard=True)
            return None

        async def _send(message, user_id, resp) -> None:
            markup = _build_markup(resp, user_id)
            if resp.photo is not None:
                await message.reply_photo(resp.photo, caption=resp.text or None, reply_markup=markup)
            else:
                await message.reply_text(resp.text or "…", reply_markup=markup)

        async def start_h(update, _ctx):
            uid = update.effective_user.id
            await _send(update.effective_message, uid, menu.start(uid))

        async def text_h(update, _ctx):
            uid = update.effective_user.id
            text = update.effective_message.text or ""
            await _send(update.effective_message, uid, menu.handle_text(uid, text))

        async def callback_h(update, _ctx):
            q = update.callback_query
            await q.answer()
            uid = q.from_user.id
            await _send(q.message, uid, menu.handle_callback(uid, q.data))

        # Кнопочное меню: /start и /menu показывают клавиатуру, обычный текст —
        # это нажатия кнопок или пошаговый ввод, inline-кнопки — callback.
        app.add_handler(CommandHandler(["start", "menu"], start_h))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_h))
        app.add_handler(CallbackQueryHandler(callback_h))

        # Живой sender: с этого момента notify_known/unknown уходят реально.
        self._app = app
        self._loop = loop
        self._sender = _LiveSender(app.bot, self._owner_id, loop)
        self._online = True
        log.info("Telegram-бот запущен")
        # Синхронный блокирующий запуск; stop_signals=None — сигналы ловит main.py.
        app.run_polling(close_loop=False, stop_signals=None)
