"""Кнопочное меню Telegram-бота (чистая логика, без зависимости от telegram).

Слой поверх CommandRouter: превращает нажатия кнопок и пошаговый ввод в вызовы
бизнес-логики. Не импортирует python-telegram-bot, поэтому полностью тестируется.

Взаимодействие:
* main_keyboard(user_id) — раскладка постоянной нижней клавиатуры (для владельца
  и админа она разная);
* handle_text(user_id, text) — обработка нажатия кнопки или введённого текста
  (учитывает «ожидание ввода», напр. после «Добавить номер»);
* handle_callback(user_id, data) — обработка inline-кнопок (удаление из списков,
  подтверждение для неизвестной машины и т.п.).

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
        # Ожидание пошагового ввода: user_id -> вид ввода ('add_plate'/'add_admin'/'set').
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
            return MenuResponse(text="⛔ Доступ запрещён.")
        return MenuResponse(text="Главное меню. Выберите действие 👇", keyboard="main")

    # ------------------------------- текст/кнопки -------------------------------
    def handle_text(self, user_id: int, text: str) -> MenuResponse:
        if not self._router.is_admin(user_id):
            return MenuResponse(text="⛔ Доступ запрещён.")

        # 1) Ожидаем пошаговый ввод (после «Добавить номер» и т.п.).
        pending = self._pending.pop(user_id, None)
        if pending:
            return self._handle_pending(user_id, pending, text)

        text = (text or "").strip()

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
        text = (text or "").strip()
        if pending == "add_plate":
            return MenuResponse(text=self._router.cmd_add(user_id, text), keyboard="main")
        if pending == "add_admin":
            return MenuResponse(text=self._router.cmd_addadmin(user_id, text), keyboard="main")
        if pending == "set":
            parts = text.split(maxsplit=1)
            key = parts[0] if parts else ""
            value = parts[1] if len(parts) > 1 else ""
            return MenuResponse(text=self._router.cmd_set(user_id, key, value), keyboard="main")
        return MenuResponse(text="Отменено.", keyboard="main")

    # ------------------------------- inline-кнопки -------------------------------
    def handle_callback(self, user_id: int, data: str) -> MenuResponse:
        if not self._router.is_admin(user_id):
            return MenuResponse(text="⛔ Доступ запрещён.")

        # Кнопки под уведомлением о неизвестной машине.
        if data.startswith("open") or data.startswith("ignore"):
            return MenuResponse(text=self._router.on_callback(user_id, data))

        # Белый список.
        if data == "wl_add":
            self._pending[user_id] = "add_plate"
            return MenuResponse(text="Отправьте номер (например, А123ВС777):")
        if data == "wl_del":
            plates = self._router.list_plates()
            if not plates:
                return MenuResponse(text="Белый список пуст.")
            rows = [[(f"❌ {p}", f"wldel:{p}")] for p in plates]
            return MenuResponse(text="Какой номер удалить?", inline=rows)
        if data.startswith("wldel:"):
            plate = data.split(":", 1)[1]
            return MenuResponse(text=self._router.cmd_remove(user_id, plate), keyboard="main")

        # Настройки.
        if data == "set_edit":
            self._pending[user_id] = "set"
            return MenuResponse(text="Отправьте: секция.ключ значение\nНапример: relay.pulse_ms 700")

        # Админы.
        if data == "adm_add":
            self._pending[user_id] = "add_admin"
            return MenuResponse(text="Отправьте Telegram ID пользователя (число):")
        if data == "adm_del":
            ids = self._router.list_admin_ids()
            if not ids:
                return MenuResponse(text="Админов нет.")
            rows = [[(f"❌ {i}", f"admdel:{i}")] for i in ids]
            return MenuResponse(text="Какого админа удалить?", inline=rows)
        if data.startswith("admdel:"):
            aid = data.split(":", 1)[1]
            return MenuResponse(text=self._router.cmd_removeadmin(user_id, aid), keyboard="main")

        return MenuResponse(text="Неизвестное действие.", keyboard="main")

    # ------------------------------- подменю -------------------------------
    def _whitelist_menu(self, user_id: int) -> MenuResponse:
        text = self._router.cmd_list(user_id)
        inline = [[("➕ Добавить", "wl_add"), ("❌ Удалить", "wl_del")]]
        return MenuResponse(text=text, inline=inline)

    def _settings_menu(self, user_id: int) -> MenuResponse:
        if not self._router.is_owner(user_id):
            return MenuResponse(text="⛔ Доступ запрещён.", keyboard="main")
        text = self._router.cmd_settings(user_id)
        inline = [[("✏️ Изменить настройку", "set_edit")]]
        return MenuResponse(text=text, inline=inline)

    def _admins_menu(self, user_id: int) -> MenuResponse:
        if not self._router.is_owner(user_id):
            return MenuResponse(text="⛔ Доступ запрещён.", keyboard="main")
        text = self._router.cmd_admins(user_id)
        inline = [[("➕ Добавить", "adm_add"), ("❌ Удалить", "adm_del")]]
        return MenuResponse(text=text, inline=inline)
