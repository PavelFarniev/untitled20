"""Telegram-бот и уведомления (реализация Notifier).

Стек: python-telegram-bot (asyncio, кроссплатформенно). Тяжёлый импорт telegram
выполняется ЛЕНИВО в run(), поэтому модуль импортируется и тестируется без
установленного пакета.

Разделение ответственности ради тестируемости:
* CommandRouter — ЧИСТАЯ логика команд (авторизация владельца, обращение к ядру
  через колбэки/порты, формирование текста ответа). Не зависит от telegram.
* TelegramNotifier — адаптер: реализует порт Notifier (уведомления known/unknown)
  и связывает CommandRouter с реальным ботом. Отправка инъектируется (`sender`).

Команды (только от владельца): /open /photo /status /history /list /addplate
/removeplate. При неизвестном авто уходит фото + номер + время и кнопки
«✅ Открыть» / «❌ Игнорировать».

Токен по умолчанию пустой (заглушка) — подставляется в config/config.json.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional

from interfaces import Notifier, WhitelistRepository
from models import Event, HealthStatus, OpenSource, PlateNumber
from utils import normalize_plate, tail_lines
from events import event as user_event

log = logging.getLogger("gate_system.telegram")

_DENIED = "⛔ Доступ запрещён."

# Значки уровней логирования для красивого вывода в Telegram.
_LEVEL_EMOJI = {
    "DEBUG": "🔍",
    "INFO": "🟢",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "CRITICAL": "🛑",
}
_TG_LIMIT = 3800  # запас до лимита Telegram (4096) на одно сообщение


def _fmt_time(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def prettify_log_lines(lines: List[str]) -> str:
    """Превратить строки лога в компактный читаемый вид для Telegram.

    Строка формата `дата время | УРОВЕНЬ | имя | сообщение` превращается в
    `<эмодзи> ЧЧ:ММ:СС имя: сообщение`. Строки другого вида (трейсбеки) остаются
    как есть. Итог обрезается под лимит Telegram (берутся самые свежие строки).
    """
    pretty: List[str] = []
    for line in lines:
        parts = line.split(" | ", 3)
        if len(parts) == 4:
            ts, level, name, msg = (p.strip() for p in parts)
            emoji = _LEVEL_EMOJI.get(level, "•")
            time_only = ts.split(",")[0].split(" ")[-1]  # ЧЧ:ММ:СС
            short_name = name.replace("gate_system.", "")
            pretty.append(f"{emoji} {time_only} {short_name}: {msg}")
        else:
            pretty.append(line)

    # Обрезаем с начала, чтобы влезть в лимит Telegram (оставляем свежие строки).
    text = "\n".join(pretty)
    if len(text) > _TG_LIMIT:
        text = "…\n" + text[-_TG_LIMIT:]
    return text


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
    """Чистая логика обработки команд. Не импортирует telegram."""

    def __init__(
        self,
        owner_id: int,
        whitelist: WhitelistRepository,
        gate_open: Callable[[OpenSource], None],
        status_provider: Callable[[], HealthStatus],
        history_provider: Callable[[int], List[Event]],
        photo_provider: Callable[[], Optional[object]],
        config_store: Optional[object] = None,
        logs_path: str = "logs/gate_system.log",
        admin_store: Optional[object] = None,
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

    def is_owner(self, user_id: int) -> bool:
        """Владелец — единственный супер-админ (из config.telegram.owner_id)."""
        return user_id == self._owner_id

    def is_admin(self, user_id: int) -> bool:
        """Админ — владелец ИЛИ пользователь из списка админов.

        Может открывать ворота и смотреть статус/фото/историю/список, но не
        редактировать настройки, белый список, логи и список админов.
        """
        if self.is_owner(user_id):
            return True
        return self._admin_store is not None and self._admin_store.contains(user_id)

    # ------------------------------- команды -------------------------------
    def cmd_open(self, user_id: int) -> str:
        if not self.is_admin(user_id):
            return _DENIED
        self._gate_open(OpenSource.TELEGRAM)
        log.info("Команда /open (user_id=%s) — ворота открыты", user_id)
        user_event("🔓 Ворота открыты через Telegram")
        return "✅ Ворота открываются."

    def cmd_status(self, user_id: int) -> str:
        if not self.is_admin(user_id):
            return _DENIED
        h = self._status_provider()
        temp = f"{h.cpu_temp_c:.0f}°C" if h.cpu_temp_c is not None else "н/д"
        return (
            "🟢 Статус системы\n"
            f"Камера: {'OK' if h.camera_ok else 'ОШИБКА'}\n"
            f"OCR: {'OK' if h.ocr_ok else 'ОШИБКА'}\n"
            f"Telegram: {'онлайн' if h.telegram_online else 'оффлайн'}\n"
            f"Память: {h.memory_percent:.0f}%\n"
            f"Диск свободно: {h.disk_free_percent:.0f}%\n"
            f"Температура CPU: {temp}\n"
            f"Последний номер: {h.last_plate or '—'}"
        )

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
            lines.append(f"{mark} {_fmt_time(e.timestamp)}  {plate}  ({e.result.value})")
        return "\n".join(lines)

    def cmd_list(self, user_id: int) -> str:
        if not self.is_admin(user_id):
            return _DENIED
        plates = self._whitelist.list_all()
        if not plates:
            return "Белый список пуст."
        return "📋 Белый список:\n" + "\n".join(p.value for p in plates)

    def cmd_add(self, user_id: int, arg: str) -> str:
        if not self.is_owner(user_id):
            return _DENIED
        plate = normalize_plate(arg or "")
        if plate is None:
            return f"❌ Некорректный номер: {arg!r}"
        self._whitelist.add(plate)
        return f"✅ Добавлен номер: {plate.value}"

    def cmd_remove(self, user_id: int, arg: str) -> str:
        if not self.is_owner(user_id):
            return _DENIED
        plate = normalize_plate(arg or "")
        if plate is None:
            return f"❌ Некорректный номер: {arg!r}"
        self._whitelist.remove(plate)
        return f"✅ Удалён номер: {plate.value}"

    def cmd_photo(self, user_id: int) -> PhotoReply:
        if not self.is_admin(user_id):
            return PhotoReply(photo=None, caption=_DENIED)
        photo = self._photo_provider()
        if photo is None:
            return PhotoReply(photo=None, caption="Фото пока недоступно.")
        return PhotoReply(photo=photo, caption="📷 Текущий кадр с камеры")

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
        return "⚙️ Текущие настройки:\n" + self._config_store.as_display()

    def cmd_set(self, user_id: int, dotted_key: str, value: str) -> str:
        if not self.is_owner(user_id):
            return _DENIED
        if self._config_store is None:
            return "Изменение настроек недоступно."
        if not dotted_key or value is None or value == "":
            return "Формат: /set секция.ключ значение (пример: /set relay.pulse_ms 700)"
        try:
            return self._config_store.set(dotted_key, value)
        except KeyError as exc:
            return f"❌ {exc.args[0] if exc.args else 'неизвестный ключ'}"
        except ValueError as exc:
            return f"❌ Неверное значение: {exc}"

    # --------------------------- управление админами ---------------------------
    def cmd_addadmin(self, user_id: int, arg: str) -> str:
        if not self.is_owner(user_id):
            return _DENIED
        if self._admin_store is None:
            return "Управление админами недоступно."
        try:
            new_id = int((arg or "").strip())
        except ValueError:
            return f"❌ Некорректный ID: {arg!r} (ожидается число)"
        if new_id == self._owner_id:
            return "Этот пользователь уже владелец."
        self._admin_store.add(new_id)
        return f"✅ Админ добавлен: {new_id}"

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
        return f"✅ Админ удалён: {rid}"

    def cmd_admins(self, user_id: int) -> str:
        if not self.is_owner(user_id):
            return _DENIED
        ids = self._admin_store.list_ids() if self._admin_store else []
        body = "\n".join(str(i) for i in ids) if ids else "—"
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
            self._gate_open(OpenSource.TELEGRAM)
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
        (например, при пустом токене-заглушке).
    """

    def __init__(
        self,
        token: str,
        owner_id: int,
        router: CommandRouter,
        sender: Optional[object] = None,
        reconnect_delay_s: float = 5.0,
        proxy_url: str = "",
    ) -> None:
        self._token = token
        self._owner_id = owner_id
        self._router = router
        self._sender = sender
        self._reconnect_delay = reconnect_delay_s
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
        потока и отдаём его боту. Токен-заглушка (пустой) означает, что бот не
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
            Update,
        )
        from telegram.ext import (
            ApplicationBuilder,
            CommandHandler,
            CallbackQueryHandler,
            MessageHandler,
            ContextTypes,
            filters,
        )
        from menu import MenuController

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

        async def start_h(update, ctx: "ContextTypes.DEFAULT_TYPE"):
            uid = update.effective_user.id
            await _send(update.effective_message, uid, menu.start(uid))

        async def text_h(update, ctx):
            uid = update.effective_user.id
            text = update.effective_message.text or ""
            await _send(update.effective_message, uid, menu.handle_text(uid, text))

        async def callback_h(update, ctx):
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
