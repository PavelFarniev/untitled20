"""Тесты Telegram: CommandRouter (авторизация, команды) и TelegramNotifier."""
import pytest

from telegram_bot import (
    CommandRouter,
    PhotoReply,
    TelegramNotifier,
    prettify_log_lines,
    prettify_event_lines,
)
from whitelist import JsonWhitelistRepository
from models import CheckResult, Event, HealthStatus, OpenSource, PlateNumber

OWNER = 42
STRANGER = 999


@pytest.fixture()
def router(tmp_path):
    allowed = tmp_path / "allowed.json"
    allowed.write_text('{"allowed": ["А123ВС777"]}', encoding="utf-8")
    wl = JsonWhitelistRepository(str(allowed))

    opened = []
    health = HealthStatus(camera_ok=True, ocr_ok=True, telegram_online=True,
                          memory_percent=40, disk_free_percent=70,
                          last_plate=PlateNumber("А123ВС777"))
    events = [Event.now(PlateNumber("А123ВС777"), CheckResult.GRANTED, gate_opened=True,
                        source=OpenSource.AUTO)]

    r = CommandRouter(
        owner_id=OWNER,
        whitelist=wl,
        gate_open=lambda src: opened.append(src),
        status_provider=lambda: health,
        history_provider=lambda limit: events[:limit],
        photo_provider=lambda: b"JPEGDATA",
    )
    r._opened = opened  # для проверки в тесте
    return r


class TestAuthorization:
    def test_stranger_cannot_open(self, router):
        assert "запрещён" in router.cmd_open(STRANGER)
        assert router._opened == []          # ворота не открылись

    def test_owner_can_open(self, router):
        assert "открыва" in router.cmd_open(OWNER).lower()
        assert router._opened == [OpenSource.TELEGRAM]

    def test_stranger_blocked_on_all_commands(self, router):
        assert "запрещён" in router.cmd_status(STRANGER)
        assert "запрещён" in router.cmd_history(STRANGER)
        assert "запрещён" in router.cmd_list(STRANGER)
        assert "запрещён" in router.cmd_add(STRANGER, "М001ММ77")
        assert "запрещён" in router.cmd_remove(STRANGER, "А123ВС777")
        assert "запрещён" in router.cmd_photo(STRANGER).caption


class TestCommands:
    def test_status_format(self, router):
        out = router.cmd_status(OWNER)
        assert "Статус системы" in out
        assert "Камера: OK" in out
        assert "онлайн" in out

    def test_history_format(self, router):
        out = router.cmd_history(OWNER)
        assert "Последние события" in out
        assert "А123ВС777" in out
        assert "✅" in out

    def test_list(self, router):
        assert "А123ВС777" in router.cmd_list(OWNER)

    def test_add_valid(self, router):
        out = router.cmd_add(OWNER, "м001мм77")
        assert "М001ММ77" in out
        assert "М001ММ77" in router.cmd_list(OWNER)

    def test_add_invalid(self, router):
        assert "Некорректный" in router.cmd_add(OWNER, "мусор")

    def test_remove(self, router):
        router.cmd_remove(OWNER, "А123ВС777")
        assert "А123ВС777" not in router.cmd_list(OWNER)

    def test_photo_reply(self, router):
        pr = router.cmd_photo(OWNER)
        assert isinstance(pr, PhotoReply)
        assert pr.photo == b"JPEGDATA"


class TestCallbacks:
    def test_owner_open_button(self, router):
        out = router.on_callback(OWNER, "open:А123ВС777")
        assert "открыт" in out.lower()
        assert router._opened == [OpenSource.TELEGRAM]

    def test_owner_ignore_button(self, router):
        assert "роигнор" in router.on_callback(OWNER, "ignore")
        assert router._opened == []

    def test_stranger_callback_denied(self, router):
        assert "запрещён" in router.on_callback(STRANGER, "open:X")
        assert router._opened == []


# --------------------------------- Notifier ---------------------------------
class TestPrettifyLogs:
    def test_standard_line_formatted(self):
        line = "2026-07-17 00:24:34,857 | INFO     | gate_system.pipeline | Распознан номер"
        out = prettify_log_lines([line])
        assert "🟢" in out and "00:24:34" in out
        assert "pipeline: Распознан номер" in out
        assert "gate_system." not in out   # префикс убран

    def test_level_emojis(self):
        w = "2026-07-17 00:24:34,857 | WARNING  | gate_system.camera | нет кадра"
        e = "2026-07-17 00:24:34,857 | ERROR    | gate_system.main | сбой"
        assert "⚠️" in prettify_log_lines([w])
        assert "❌" in prettify_log_lines([e])

    def test_non_standard_line_kept_as_is(self):
        out = prettify_log_lines(["Traceback (most recent call last):"])
        assert "Traceback (most recent call last):" in out

    def test_truncated_to_telegram_limit(self):
        big = ["2026-07-17 00:24:34,857 | INFO     | gate_system.x | " + "a" * 120
               for _ in range(300)]
        out = prettify_log_lines(big)
        assert len(out) <= 3802   # лимит 3800 + префикс "…\n"


class TestPrettifyEvents:
    def test_event_line_formatted(self):
        line = "2026-07-17 00:24:34 | 🚗 Ворота открыты — Т198ТТ197"
        out = prettify_event_lines([line])
        assert out == "00:24:34  🚗 Ворота открыты — Т198ТТ197"

    def test_multiple_events(self):
        lines = [
            "2026-07-17 00:24:34 | 🚗 Ворота открыты — Т198ТТ197",
            "2026-07-17 00:25:00 | 🌐 Интернет пропал",
        ]
        out = prettify_event_lines(lines)
        assert "🚗 Ворота открыты" in out and "🌐 Интернет пропал" in out
        assert "gate_system" not in out and "INFO" not in out  # без техножаргона


class FakeSender:
    def __init__(self):
        self.messages = []
        self.photos = []

    def send_message(self, text):
        self.messages.append(text)

    def send_photo(self, photo, caption, buttons):
        self.photos.append((photo, caption, buttons))


class TestTelegramNotifier:
    def _notifier(self, sender=None):
        return TelegramNotifier("TOKEN_PLACEHOLDER", OWNER, router=None, sender=sender)

    def test_notify_known_sends_message(self):
        s = FakeSender()
        self._notifier(s).notify_known(
            Event.now(PlateNumber("А123ВС777"), CheckResult.GRANTED, gate_opened=True)
        )
        assert len(s.messages) == 1 and "А123ВС777" in s.messages[0]

    def test_notify_known_with_photo_sends_photo(self):
        s = FakeSender()
        self._notifier(s).notify_known(
            Event.now(PlateNumber("А123ВС777"), CheckResult.GRANTED,
                      gate_opened=True, photo_path="photos/car.jpg")
        )
        assert len(s.photos) == 1
        photo, caption, _ = s.photos[0]
        assert photo == "photos/car.jpg" and "А123ВС777" in caption

    def test_notify_unknown_sends_photo_with_buttons(self):
        s = FakeSender()
        self._notifier(s).notify_unknown(
            Event.now(PlateNumber("Х999ХХ99"), CheckResult.DENIED, photo_path="p.jpg")
        )
        assert len(s.photos) == 1
        photo, caption, buttons = s.photos[0]
        assert photo == "p.jpg"
        assert "Неизвестный автомобиль" in caption
        labels = [b[0] for b in buttons]
        assert "✅ Открыть" in labels and "❌ Игнорировать" in labels

    def test_no_sender_does_not_raise(self):
        # Пустой токен/оффлайн: уведомление просто логируется, ядро не падает.
        self._notifier(sender=None).notify_known(
            Event.now(PlateNumber("А123ВС777"), CheckResult.GRANTED)
        )

    def test_sender_exception_swallowed(self):
        class Boom:
            def send_message(self, text):
                raise ConnectionError("offline")
        self._notifier(Boom()).notify_known(
            Event.now(PlateNumber("А123ВС777"), CheckResult.GRANTED)
        )  # не должно бросить

    def test_online_flag(self):
        n = self._notifier()
        assert n.is_online() is False
        n.set_online(True)
        assert n.is_online() is True
