"""Кнопочное меню Telegram-бота (чистая логика, без зависимости от telegram).

Слой поверх CommandRouter: превращает нажатия кнопок и пошаговый ввод в вызовы
бизнес-логики. Не импортирует python-telegram-bot, поэтому полностью тестируется.

Взаимодействие:
* main_keyboard(user_id) — раскладка постоянной нижней клавиатуры (для владельца
  и админа она разная);
* handle_text(user_id, text) — обработка нажатия кнопки или введённого текста
  (учитывает «ожидание ввода», напр. после «Добавить номер»);
* handle_callback(user_id, data) — обработка inline-кнопок (добавление,
  исправление и удаление номеров/админов, перезапуск, кнопки под уведомлением).

Каждый метод возвращает MenuResponse: что отправить пользователю (текст/фото),
какую клавиатуру показать и какие inline-кнопки приложить.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# --- Подписи кнопок главного меню ---
BTN_OPEN = "🚪 Открыть ворота"
BTN_PHOTO = "📷 Фото"
BTN_STATUS = "📊 Статус"
BTN_HISTORY = "🕓 История"
BTN_LIST = "📋 Белый список"
BTN_LOGS = "📜 Журнал"
BTN_SETTINGS = "⚙️ Настройки"
BTN_ADMINS = "👥 Админы"

_MAIN_BUTTONS = {BTN_OPEN, BTN_PHOTO, BTN_STATUS, BTN_HISTORY, BTN_LIST,
                 BTN_LOGS, BTN_SETTINGS, BTN_ADMINS}

# Inline-действия, доступные только владельцу (админ получит отказ сразу).
_OWNER_CALLBACKS = ("wl_", "wldel:", "wlren:", "adm_", "admdel:", "admren:", "set_", "restart")

_DENIED = "⛔ Доступ запрещён."


@dataclass
class MenuResponse:
    """Что бот должен показать в ответ."""

    text: str = ""
    photo: Optional[object] = None
    # 'main' — показать главную клавиатуру; None — не менять клавиатуру.
    keyboard: Optional[str] = None
    # Inline-кнопки: список рядов, каждый ряд — [(подпись, callback_data), ...].
    inline: Optional[List[List[Tuple[str, str]]]] = None


class MenuController:
    """Состояние меню и маршрутизация нажатий к CommandRouter."""

    def __init__(self, router) -> None:
        self._router = router
        # Ожидание пошагового ввода: user_id -> вид ввода
        # ('add_plate' | 'add_admin' | 'set' | 'rename_plate:<номер>' | 'rename_admin:<id>').
        self._pending: Dict[int, str] = {}

    # ------------------------------- клавиатура -------------------------------
    def main_keyboard(self, user_id: int) -> List[List[str]]:
        """Раскладка постоянной нижней клавиатуры (зависит от роли)."""
        rows = [[BTN_OPEN, BTN_PHOTO], [BTN_STATUS, BTN_HISTORY], [BTN_LIST]]
        if self._router.is_owner(user_id):
            rows.append([BTN_LOGS, BTN_SETTINGS])
            rows.append([BTN_ADMINS])
        return rows

    def start(self, user_id: int) -> MenuResponse:
        if not self._router.is_admin(user_id):
            return MenuResponse(text=_DENIED)
        return MenuResponse(text="Главное меню. Выберите действие 👇", keyboard="main")

    # ------------------------------- текст/кнопки -------------------------------
    def handle_text(self, user_id: int, text: str) -> MenuResponse:
        if not self._router.is_admin(user_id):
            return MenuResponse(text=_DENIED)

        text = (text or "").strip()

        # 1) Ожидаем пошаговый ввод (после «Добавить номер» и т.п.). Нажатие кнопки
        #    главного меню отменяет ожидание и выполняется как обычно.
        pending = self._pending.pop(user_id, None)
        if pending and text not in _MAIN_BUTTONS and text not in ("/start", "/menu"):
            return self._handle_pending(user_id, pending, text)

        # 2) Нажатие кнопки главного меню.
        if text in ("/start", "меню", "Меню", "/menu"):
            return self.start(user_id)
        if text == BTN_OPEN:
            return MenuResponse(text=self._router.cmd_open(user_id), keyboard="main")
        if text == BTN_PHOTO:
            pr = self._router.cmd_photo(user_id)
            return MenuResponse(text=pr.caption, photo=pr.photo, keyboard="main")
        if text == BTN_STATUS:
            return MenuResponse(text=self._router.cmd_status(user_id), keyboard="main")
        if text == BTN_HISTORY:
            return MenuResponse(text=self._router.cmd_history(user_id), keyboard="main")
        if text == BTN_LIST:
            return self._whitelist_menu(user_id)
        if text == BTN_LOGS:
            return MenuResponse(text=self._router.cmd_logs(user_id), keyboard="main")
        if text == BTN_SETTINGS:
            return self._settings_menu(user_id)
        if text == BTN_ADMINS:
            return self._admins_menu(user_id)

        # 3) Непонятный текст — показать меню.
        return MenuResponse(text="Не понял. Вот меню 👇", keyboard="main")

    def _handle_pending(self, user_id: int, pending: str, text: str) -> MenuResponse:
        if pending == "add_plate":
            return MenuResponse(text=self._router.cmd_add(user_id, text), keyboard="main")
        if pending == "add_admin":
            return MenuResponse(text=self._router.cmd_addadmin(user_id, text), keyboard="main")
        if pending == "set":
            parts = text.split(maxsplit=1)
            key = parts[0] if parts else ""
            value = parts[1] if len(parts) > 1 else ""
            return MenuResponse(text=self._router.cmd_set(user_id, key, value), keyboard="main")
        if pending.startswith("rename_plate:"):
            old = pending.split(":", 1)[1]
            return MenuResponse(text=self._router.cmd_rename(user_id, old, text), keyboard="main")
        if pending.startswith("rename_admin:"):
            old = pending.split(":", 1)[1]
            return MenuResponse(text=self._router.cmd_rename_admin(user_id, old, text), keyboard="main")
        return MenuResponse(text="Отменено.", keyboard="main")

    # ------------------------------- inline-кнопки -------------------------------
    def handle_callback(self, user_id: int, data: str) -> MenuResponse:
        if not self._router.is_admin(user_id):
            return MenuResponse(text=_DENIED)
        data = data or ""

        # Кнопки под уведомлением о неизвестной машине (доступны админам).
        if data.startswith("open") or data.startswith("ignore"):
            return MenuResponse(text=self._router.on_callback(user_id, data))

        if data.startswith(_OWNER_CALLBACKS) and not self._router.is_owner(user_id):
            return MenuResponse(text=_DENIED, keyboard="main")

        # Белый список.
        if data == "wl_add":
            self._pending[user_id] = "add_plate"
            return MenuResponse(text="Отправьте номер, можно с подписью.\nНапример: А123ВС777 Папа")
        if data == "wl_edit":
            return self._pick_plate("Какой номер исправить?", "wlren:")
        if data.startswith("wlren:"):
            plate = data.split(":", 1)[1]
            self._pending[user_id] = f"rename_plate:{plate}"
            return MenuResponse(text=f"Отправьте правильный номер вместо {plate} "
                                     "(можно с новой подписью):")
        if data == "wl_del":
            return self._pick_plate("Какой номер удалить?", "wldel:")
        if data.startswith("wldel:"):
            plate = data.split(":", 1)[1]
            return MenuResponse(text=self._router.cmd_remove(user_id, plate), keyboard="main")

        # Настройки.
        if data == "set_edit":
            self._pending[user_id] = "set"
            return MenuResponse(text="Отправьте: секция.ключ значение\nНапример: relay.pulse_ms 700")
        if data == "restart":
            return MenuResponse(
                text="Перезапустить систему? Распознавание будет недоступно 1–2 минуты.",
                inline=[[("✅ Да, перезапустить", "restart_yes"), ("Отмена", "restart_no")]])
        if data == "restart_yes":
            return MenuResponse(text=self._router.cmd_restart(user_id), keyboard="main")
        if data == "restart_no":
            return MenuResponse(text="Перезапуск отменён.", keyboard="main")

        # Админы.
        if data == "adm_add":
            self._pending[user_id] = "add_admin"
            return MenuResponse(text="Отправьте Telegram ID пользователя (число), можно с подписью.\n"
                                     "Например: 123456789 Мама\n"
                                     "Узнать свой ID человек может у бота @userinfobot.")
        if data == "adm_edit":
            return self._pick_admin("Чей ID исправить?", "admren:")
        if data.startswith("admren:"):
            aid = data.split(":", 1)[1]
            self._pending[user_id] = f"rename_admin:{aid}"
            return MenuResponse(text=f"Отправьте правильный ID вместо {aid} (можно с подписью):")
        if data == "adm_del":
            return self._pick_admin("Какого админа удалить?", "admdel:")
        if data.startswith("admdel:"):
            aid = data.split(":", 1)[1]
            return MenuResponse(text=self._router.cmd_removeadmin(user_id, aid), keyboard="main")

        return MenuResponse(text="Неизвестное действие.", keyboard="main")

    def _pick_plate(self, title: str, prefix: str) -> MenuResponse:
        plates = self._router.list_plates()
        if not plates:
            return MenuResponse(text="Белый список пуст.")
        mark = "❌" if prefix == "wldel:" else "✏️"
        rows = [[(f"{mark} {p}", f"{prefix}{p}")] for p in plates]
        return MenuResponse(text=title, inline=rows)

    def _pick_admin(self, title: str, prefix: str) -> MenuResponse:
        ids = self._router.list_admin_ids()
        if not ids:
            return MenuResponse(text="Админов нет.")
        mark = "❌" if prefix == "admdel:" else "✏️"
        rows = [[(f"{mark} {i}", f"{prefix}{i}")] for i in ids]
        return MenuResponse(text=title, inline=rows)

    # ------------------------------- подменю -------------------------------
    def _whitelist_menu(self, user_id: int) -> MenuResponse:
        text = self._router.cmd_list(user_id)
        if not self._router.is_owner(user_id):
            return MenuResponse(text=text, keyboard="main")
        inline = [[("➕ Добавить", "wl_add"), ("✏️ Изменить", "wl_edit"), ("❌ Удалить", "wl_del")]]
        return MenuResponse(text=text, inline=inline)

    def _settings_menu(self, user_id: int) -> MenuResponse:
        if not self._router.is_owner(user_id):
            return MenuResponse(text=_DENIED, keyboard="main")
        text = self._router.cmd_settings(user_id)
        inline = [[("✏️ Изменить настройку", "set_edit")], [("🔄 Перезапустить систему", "restart")]]
        return MenuResponse(text=text, inline=inline)

    def _admins_menu(self, user_id: int) -> MenuResponse:
        if not self._router.is_owner(user_id):
            return MenuResponse(text=_DENIED, keyboard="main")
        text = self._router.cmd_admins(user_id)
        inline = [[("➕ Добавить", "adm_add"), ("✏️ Изменить", "adm_edit"), ("❌ Удалить", "adm_del")]]
        return MenuResponse(text=text, inline=inline)
