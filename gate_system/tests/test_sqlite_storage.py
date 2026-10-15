"""Тесты SQLite-хранилища: белый список, админы, история."""
import threading

import pytest

from gate.core.models import CheckResult, Event, OpenSource, PlateNumber
from gate.storage.admins import SqliteAdminRepository
from gate.storage.database import SqliteDatabase
from gate.storage.history import SqliteHistoryRepository
from gate.storage.whitelist import SqliteWhitelistRepository


@pytest.fixture()
def db(tmp_path):
    d = SqliteDatabase(tmp_path / "gate.db")
    yield d
    d.close()


# ------------------------------- белый список -------------------------------
class TestWhitelist:
    def test_add_and_check(self, db):
        wl = SqliteWhitelistRepository(db)
        wl.add(PlateNumber("А123ВС777"))
        assert wl.is_allowed(PlateNumber("А123ВС777"))
        assert not wl.is_allowed(PlateNumber("М001ММ77"))

    def test_latin_input_matches_cyrillic_recognition(self, db):
        """Номер, набранный латиницей, совпадает с распознанным кириллицей."""
        wl = SqliteWhitelistRepository(db)
        wl.add(PlateNumber("T198TT197"))
        assert wl.is_allowed(PlateNumber("Т198ТТ197"))

    def test_note_and_duplicate(self, db):
        wl = SqliteWhitelistRepository(db)
        wl.add(PlateNumber("А123ВС777"), "Папа")
        wl.add(PlateNumber("А123ВС777"))           # дубль без подписи — подпись сохраняется
        assert wl.list_entries() == [(PlateNumber("А123ВС777"), "Папа")]

    def test_rename_keeps_note(self, db):
        wl = SqliteWhitelistRepository(db)
        wl.add(PlateNumber("А123ВС777"), "Папа")
        assert wl.rename(PlateNumber("А123ВС777"), PlateNumber("А123ВС799")) is True
        assert wl.list_entries() == [(PlateNumber("А123ВС799"), "Папа")]
        assert not wl.is_allowed(PlateNumber("А123ВС777"))

    def test_rename_missing_returns_false(self, db):
        assert SqliteWhitelistRepository(db).rename(PlateNumber("А123ВС777"),
                                                    PlateNumber("В111ВВ11")) is False

    def test_remove(self, db):
        wl = SqliteWhitelistRepository(db)
        wl.add(PlateNumber("А123ВС777"))
        wl.remove(PlateNumber("А123ВС777"))
        assert wl.list_all() == []

    def test_survives_reopen(self, tmp_path):
        """Данные на диске: после перезапуска (новое соединение) список на месте."""
        d1 = SqliteDatabase(tmp_path / "g.db")
        SqliteWhitelistRepository(d1).add(PlateNumber("А123ВС777"))
        d1.close()
        d2 = SqliteDatabase(tmp_path / "g.db")
        assert SqliteWhitelistRepository(d2).is_allowed(PlateNumber("А123ВС777"))
        d2.close()

    def test_concurrent_adds_from_threads(self, db):
        """Две камеры + бот пишут одновременно — без ошибок и потерь."""
        wl = SqliteWhitelistRepository(db)
        plates = [f"А{i:03d}ВС77" for i in range(60)]

        def worker(chunk):
            for p in chunk:
                wl.add(PlateNumber(p))

        threads = [threading.Thread(target=worker, args=(plates[i::3],)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(wl.list_all()) == 60


# ------------------------------- админы -------------------------------
class TestAdmins:
    def test_add_contains_remove(self, db):
        a = SqliteAdminRepository(db)
        a.add(100, "Мама")
        assert a.contains(100) and a.list_entries() == [(100, "Мама")]
        a.remove(100)
        assert not a.contains(100)

    def test_rename(self, db):
        a = SqliteAdminRepository(db)
        a.add(100, "Мама")
        assert a.rename(100, 200) is True
        assert a.list_entries() == [(200, "Мама")]
        assert a.rename(999, 1) is False


# ------------------------------- история -------------------------------
class TestHistory:
    def test_append_and_recent_newest_first(self, db):
        h = SqliteHistoryRepository(db)
        h.append(Event.now(PlateNumber("А123ВС777"), CheckResult.GRANTED,
                           gate_opened=True, source=OpenSource.AUTO, photo_path="p.jpg"))
        h.append(Event.now(PlateNumber("М001ММ77"), CheckResult.DENIED))
        r = h.recent(10)
        assert [e.plate.value for e in r] == ["М001ММ77", "А123ВС777"]
        assert r[1].gate_opened is True and r[1].source is OpenSource.AUTO
        assert r[1].photo_path == "p.jpg"
        assert r[0].timestamp.tzinfo is not None   # время с часовым поясом (UTC)

    def test_prune_keeps_last_n(self, db):
        h = SqliteHistoryRepository(db, max_records=10)
        for _ in range(120):
            h.append(Event.now(None, CheckResult.UNREADABLE))
        h.prune()
        assert h.count() == 10

    def test_corrupted_row_skipped(self, db):
        """Повреждённая запись не ломает «Историю»."""
        h = SqliteHistoryRepository(db)
        h.append(Event.now(PlateNumber("А123ВС777"), CheckResult.GRANTED))
        db.execute("INSERT INTO events(ts, result) VALUES ('не дата', 'мусор')")
        assert [e.plate.value for e in h.recent(10)] == ["А123ВС777"]
