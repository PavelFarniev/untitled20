"""Тесты поведения, важного на Raspberry Pi: свежий кадр, снимки, диагностика
нескольких камер, неблокирующая загрузка Nomeroff, потокобезопасность моделей,
исправление номеров/админов из бота, перезапуск."""
import threading
import time
from datetime import datetime, timezone

import cv2
import numpy as np
import pytest

from gate.bot.menu import BTN_LIST, BTN_STATUS, MenuController
from gate.bot.telegram_bot import CommandRouter, _fmt_time
from gate.config import Config
from gate.core.models import HealthStatus, PlateNumber
from gate.core.plates import split_plate_and_note
from gate.services.diagnostics import SelfDiagnostics
from gate.storage.admins import SqliteAdminRepository
from gate.storage.database import SqliteDatabase
from gate.storage.whitelist import SqliteWhitelistRepository
from gate.vision.camera import LatestFrameSource, RTSPCamera
from gate.vision.nomeroff_ocr import NomeroffOcrEngine, _DefaultNomeroffRecognizer
from gate.vision.snapshot import SnapshotProvider

OWNER, ADMIN, STRANGER = 1, 2, 3


# ------------------------------- свежий кадр -------------------------------
@pytest.fixture()
def video_file(tmp_path):
    path = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 50.0, (64, 48))
    for i in range(20):
        writer.write(np.full((48, 64, 3), i * 10, dtype=np.uint8))
    writer.release()
    return str(path)


class TestLatestFrameSource:
    def test_reads_fresh_frames_and_reports_alive(self, video_file):
        src = LatestFrameSource(RTSPCamera(video_file, name="test"), read_timeout_s=2.0)
        src.open()
        try:
            f1 = src.read()
            seq1 = src.frame_seq
            f2 = src.read()
            assert f1 is not None and f2 is not None
            assert src.frame_seq > seq1            # всегда кадр новее предыдущего
            assert src.is_alive() is True
            assert src.latest() is not None        # для кнопки «Фото»
        finally:
            src.close()

    def test_slow_consumer_skips_old_frames(self, video_file):
        """Пока шло медленное распознавание, старые кадры пропускаются (нет отставания)."""
        src = LatestFrameSource(RTSPCamera(video_file), read_timeout_s=2.0)
        src.open()
        try:
            src.read()
            first = src.frame_seq
            time.sleep(0.3)                       # «распознаём» 0.3 c при 50 fps
            src.read()
            assert src.frame_seq - first >= 5     # перескочили через накопившиеся кадры
        finally:
            src.close()

    def test_missing_camera_not_alive_but_starting_grace(self, tmp_path):
        src = LatestFrameSource(RTSPCamera(str(tmp_path / "nope.avi"), sleeper=lambda s: time.sleep(0.01)),
                                read_timeout_s=0.2)
        src.open()
        try:
            assert src.read() is None
            assert src.is_alive() is False
            assert src.is_starting(grace_s=30) is True   # только что запущена — не тревожиться
        finally:
            src.close()


# ------------------------------- снимок «Фото» -------------------------------
class _Src:
    def __init__(self, frame):
        self._f = frame

    def latest(self):
        return self._f


class TestSnapshot:
    def test_two_cameras_stacked_into_one_jpeg(self):
        a = np.zeros((480, 640, 3), np.uint8)
        b = np.full((720, 1280, 3), 255, np.uint8)
        snap = SnapshotProvider([("cam1", "entry", _Src(a)), ("cam2", "exit", _Src(b))], width=640)
        data, caption = snap.snapshot()
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        assert img.shape[1] == 640 and img.shape[0] == 480 + 360   # сверху въезд, ниже выезд
        assert "въезд" in caption and "выезд" in caption

    def test_no_frames_returns_none(self):
        assert SnapshotProvider([("cam", "entry", _Src(None))]).snapshot() is None

    def test_one_camera_missing_is_mentioned(self):
        snap = SnapshotProvider([("cam1", "entry", _Src(np.zeros((10, 10, 3), np.uint8))),
                                 ("cam2", "exit", _Src(None))], width=10)
        _, caption = snap.snapshot()
        assert "Нет кадра" in caption and "cam2" in caption


# ------------------------------- диагностика -------------------------------
class _Cam:
    def __init__(self, alive, starting=False):
        self._a, self._s = alive, starting

    def is_alive(self):
        return self._a

    def is_starting(self, grace_s):
        return self._s


class _Metrics:
    def memory_percent(self):
        return 30.0

    def disk_free_percent(self, path="/"):
        return 80.0

    def cpu_temp_c(self):
        return 50.0


class TestDiagnostics:
    def test_per_camera_states(self):
        d = SelfDiagnostics(Config(), metrics=_Metrics(),
                            cameras=[("entry", _Cam(True)), ("exit", _Cam(False))])
        s = d.snapshot()
        assert s.extra["cameras"] == {"entry": "ok", "exit": "down"}
        assert s.camera_ok is False

    def test_starting_camera_is_not_an_error(self, caplog):
        """На старте камера ещё не прислала кадр — это не «камера не отвечает»."""
        import logging
        d = SelfDiagnostics(Config(), metrics=_Metrics(), cameras=[("entry", _Cam(False, True))])
        with caplog.at_level(logging.WARNING, logger="gate_system.diagnostics"):
            s = d.snapshot()
        assert s.camera_ok is True and s.extra["cameras"]["entry"] == "starting"
        assert not any("не отвечает" in r.getMessage() for r in caplog.records)

    def test_camera_warning_once_per_outage(self, caplog):
        import logging
        d = SelfDiagnostics(Config(), metrics=_Metrics(), cameras=[("entry", _Cam(False))])
        with caplog.at_level(logging.WARNING, logger="gate_system.diagnostics"):
            d.snapshot()
            d.snapshot()
            d.snapshot()
        assert sum("не отвечает" in r.getMessage() for r in caplog.records) == 1

    def test_ocr_and_relay_state_in_status(self):
        d = SelfDiagnostics(Config(), metrics=_Metrics(), cameras=[("c", _Cam(True))],
                            ocr_ok_provider=lambda: False,
                            ocr_state_provider=lambda: "загружается",
                            relay_state_provider=lambda: (False, "тестовый режим"))
        s = d.snapshot()
        assert s.ocr_ok is False and s.extra["ocr_state"] == "загружается"
        assert s.extra["relay_ok"] is False and s.extra["relay"] == "тестовый режим"


# ------------------------------- Nomeroff: загрузка -------------------------------
class TestNomeroffLoading:
    def test_not_ready_does_not_block_and_triggers_background_load(self):
        """Пока модели не загружены — распознавание сразу отвечает «пусто», а загрузка
        идёт в фоне (поток камеры не блокируется, watchdog не срабатывает)."""
        started = threading.Event()
        release = threading.Event()
        rec = _DefaultNomeroffRecognizer()

        def slow_load():
            started.set()
            release.wait(2)
            return False

        rec.load = slow_load
        t0 = time.monotonic()
        assert rec(np.zeros((10, 10, 3), np.uint8)) == []
        assert time.monotonic() - t0 < 0.5
        assert started.wait(1)
        release.set()

    def test_retry_only_after_cooldown(self):
        clock = [1000.0]
        rec = _DefaultNomeroffRecognizer(retry_after_s=300, clock=lambda: clock[0])
        rec._failed_at = 1000.0
        calls = []
        rec.load = lambda: calls.append(1)
        rec.load_async()
        assert calls == [] and rec.state.startswith("ошибка")
        clock[0] = 1301.0
        rec.load_async()
        time.sleep(0.1)
        assert calls == [1]

    def test_engine_state_and_warmup_with_injected_recognizer(self):
        eng = NomeroffOcrEngine(recognizer=lambda img: ["А123ВС777"])
        assert eng.warmup() is True and eng.is_ready() is True and eng.state() == "готов"

    def test_inference_is_serialized_between_cameras(self):
        """Две камеры вызывают один движок одновременно — инференс идёт строго по очереди."""
        active = [0]
        overlap = [False]

        def recognizer(img):
            active[0] += 1
            if active[0] > 1:
                overlap[0] = True
            time.sleep(0.05)
            active[0] -= 1
            return ["А123ВС777"]

        eng = NomeroffOcrEngine(recognizer=recognizer)
        threads = [threading.Thread(target=eng.read_text, args=(np.zeros((5, 5, 3)),))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert overlap[0] is False


class TestDetectorThreadSafety:
    def test_predict_is_serialized(self):
        from gate.vision.detector import YoloDetector
        active = [0]
        overlap = [False]

        class Model:
            def predict(self, frame, conf, verbose):
                active[0] += 1
                if active[0] > 1:
                    overlap[0] = True
                time.sleep(0.03)
                active[0] -= 1
                return []

        det = YoloDetector("p.pt", model_loader=lambda p: Model())
        threads = [threading.Thread(target=det.detect_plate, args=(None,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert overlap[0] is False


# ------------------------------- бот: правка списков -------------------------------
@pytest.fixture()
def bot(tmp_path):
    db = SqliteDatabase(tmp_path / "g.db")
    wl = SqliteWhitelistRepository(db)
    admins = SqliteAdminRepository(db)
    wl.add(PlateNumber("А123ВС777"), "Папа")
    admins.add(ADMIN)
    restarts = []
    opened = []
    router = CommandRouter(
        owner_id=OWNER, whitelist=wl, gate_open=lambda s: opened.append(s),
        status_provider=lambda: HealthStatus(
            camera_ok=True, ocr_ok=False,
            extra={"cameras": {"въезд": "ok", "выезд": "down"}, "ocr_state": "загружается",
                   "relay_ok": False, "relay": "тестовый режим (relay.enabled=false)"}),
        history_provider=lambda n: [], photo_provider=lambda: (b"JPG", "📷 Камера: въезд"),
        admin_store=admins, restart_request=lambda: restarts.append(1),
    )
    yield {"router": router, "menu": MenuController(router), "wl": wl, "admins": admins,
           "restarts": restarts, "opened": opened}
    db.close()


class TestBotEditing:
    def test_add_with_note_and_list_shows_it(self, bot):
        r = bot["router"]
        assert "Мама" in r.cmd_add(OWNER, "в 555 вв 77 Мама")
        out = r.cmd_list(OWNER)
        assert "А123ВС777 — Папа" in out and "В555ВВ77 — Мама" in out

    def test_rename_plate_via_menu(self, bot):
        m = bot["menu"]
        r1 = m.handle_callback(OWNER, "wl_edit")
        assert ("✏️ А123ВС777", "wlren:А123ВС777") in [b for row in r1.inline for b in row]
        m.handle_callback(OWNER, "wlren:А123ВС777")
        r3 = m.handle_text(OWNER, "А123ВС799")
        assert "А123ВС777 → А123ВС799" in r3.text
        assert bot["wl"].list_entries() == [(PlateNumber("А123ВС799"), "Папа")]

    def test_rename_admin_via_menu(self, bot):
        m = bot["menu"]
        m.handle_callback(OWNER, f"admren:{ADMIN}")
        r = m.handle_text(OWNER, "777 Брат")
        assert f"{ADMIN} → 777" in r.text
        assert bot["admins"].list_entries() == [(777, "Брат")]

    def test_admin_cannot_use_edit_callbacks(self, bot):
        assert "запрещён" in bot["menu"].handle_callback(ADMIN, "wl_edit").text
        assert "запрещён" in bot["menu"].handle_callback(ADMIN, "restart_yes").text
        assert bot["restarts"] == []

    def test_menu_button_cancels_pending_input(self, bot):
        """Нажал «Добавить», передумал, нажал «Статус» — статус, а не попытка добавить номер."""
        m = bot["menu"]
        m.handle_callback(OWNER, "wl_add")
        r = m.handle_text(OWNER, BTN_STATUS)
        assert "Статус системы" in r.text
        assert len(bot["wl"].list_all()) == 1

    def test_restart_needs_confirmation(self, bot):
        m = bot["menu"]
        r = m.handle_callback(OWNER, "restart")
        assert any(d == "restart_yes" for row in r.inline for _, d in row)
        assert bot["restarts"] == []
        assert "перезапуск" in m.handle_callback(OWNER, "restart_yes").text.lower()
        assert bot["restarts"] == [1]

    def test_status_shows_each_camera_ocr_and_relay(self, bot):
        out = bot["router"].cmd_status(OWNER)
        assert "въезд: OK" in out and "выезд: ОШИБКА" in out
        assert "Распознавание: ОШИБКА (загружается)" in out
        assert "Реле: ⚠️ — тестовый режим" in out

    def test_photo_tuple_from_snapshot(self, bot):
        pr = bot["router"].cmd_photo(ADMIN)
        assert pr.photo == b"JPG" and pr.caption == "📷 Камера: въезд"

    def test_open_ignored_as_repeat_is_reported(self, bot):
        r = CommandRouter(OWNER, bot["wl"], lambda s: False, lambda: None, lambda n: [], lambda: None)
        assert "повтор" in r.cmd_open(OWNER)

    def test_admin_sees_list_but_no_edit_buttons(self, bot):
        r = bot["menu"].handle_text(ADMIN, BTN_LIST)
        assert "А123ВС777" in r.text and not r.inline


class TestTimeAndPlates:
    def test_time_shown_in_local_timezone(self):
        utc = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        assert _fmt_time(utc) == utc.astimezone().strftime("%d.%m.%Y %H:%M:%S")

    @pytest.mark.parametrize("text,plate,note", [
        ("А123ВС777 Папа", "А123ВС777", "Папа"),
        ("а 123 вс 777 Курьер дядя", "А123ВС777", "Курьер дядя"),
        ("А123ВС77 7 Мама", "А123ВС777", "Мама"),
        ("T198TT197", "Т198ТТ197", ""),
    ])
    def test_split_plate_and_note(self, text, plate, note):
        p, n = split_plate_and_note(text)
        assert p.value == plate and n == note

    def test_split_garbage(self):
        assert split_plate_and_note("мусор") == (None, "")


def test_stop_is_fast_even_while_camera_waits_to_reconnect(tmp_path):
    """Камера недоступна и ждёт переподключения (до 30 с) — остановка всё равно быстрая."""
    cam = RTSPCamera(str(tmp_path / "nope.avi"), min_delay_s=30.0, max_delay_s=30.0)
    src = LatestFrameSource(cam, read_timeout_s=0.2)
    src.open()
    time.sleep(0.3)                     # поток чтения уже спит перед переподключением
    t0 = time.monotonic()
    src.close()
    assert time.monotonic() - t0 < 2.0
