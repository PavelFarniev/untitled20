"""Тесты хранилищ: database.py, whitelist.py, history.py, PhotoStorage."""
import json
import threading

import pytest

import database
from whitelist import JsonWhitelistRepository, SqliteWhitelistRepository
from history import HistoryStore, PhotoStorage
from models import CheckResult, Event, OpenSource, PlateNumber


# --------------------------------- database ---------------------------------
class TestDatabase:
    def test_read_missing_returns_default(self, tmp_path):
        assert database.read_json(tmp_path / "no.json", {"x": 1}) == {"x": 1}

    def test_read_corrupted_returns_default(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{ not json", encoding="utf-8")
        assert database.read_json(p, []) == []

    def test_atomic_write_roundtrip(self, tmp_path):
        p = tmp_path / "sub" / "data.json"          # каталог создаётся автоматически
        database.write_json_atomic(p, {"allowed": ["А123ВС777"]})
        assert json.loads(p.read_text(encoding="utf-8")) == {"allowed": ["А123ВС777"]}

    def test_atomic_write_no_tmp_leftover(self, tmp_path):
        p = tmp_path / "data.json"
        database.write_json_atomic(p, [1, 2, 3])
        # После атомарной записи временных .tmp файлов не остаётся.
        assert list(tmp_path.glob("*.tmp")) == []


# --------------------------------- whitelist ---------------------------------
class TestJsonWhitelist:
    def test_seed_and_is_allowed(self, tmp_path):
        p = tmp_path / "allowed.json"
        p.write_text(json.dumps({"allowed": ["А123ВС777"]}), encoding="utf-8")
        wl = JsonWhitelistRepository(str(p))
        assert wl.is_allowed(PlateNumber("а123вс777")) is True   # нормализация
        assert wl.is_allowed(PlateNumber("Х999ХХ99")) is False

    def test_add_and_remove(self, tmp_path):
        p = tmp_path / "allowed.json"
        wl = JsonWhitelistRepository(str(p))
        wl.add(PlateNumber("М001ММ77"))
        assert wl.is_allowed(PlateNumber("М001ММ77")) is True
        wl.remove(PlateNumber("М001ММ77"))
        assert wl.is_allowed(PlateNumber("М001ММ77")) is False

    def test_add_is_idempotent(self, tmp_path):
        p = tmp_path / "allowed.json"
        wl = JsonWhitelistRepository(str(p))
        wl.add(PlateNumber("Е777ОР50"))
        wl.add(PlateNumber("Е777ОР50"))
        assert [x.value for x in wl.list_all()] == ["Е777ОР50"]

    def test_list_all_returns_plate_objects(self, tmp_path):
        p = tmp_path / "allowed.json"
        p.write_text(json.dumps({"allowed": ["А123ВС777", "М001ММ77"]}), encoding="utf-8")
        wl = JsonWhitelistRepository(str(p))
        assert all(isinstance(x, PlateNumber) for x in wl.list_all())

    def test_thread_safety(self, tmp_path):
        p = tmp_path / "allowed.json"
        wl = JsonWhitelistRepository(str(p))
        plates = [PlateNumber(f"А{i:03d}ВС77") for i in range(50)]

        def worker(pl):
            wl.add(pl)

        threads = [threading.Thread(target=worker, args=(pl,)) for pl in plates]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(wl.list_all()) == 50


class TestSqliteWhitelistPlaceholder:
    def test_not_implemented_yet(self, tmp_path):
        # Контракт зафиксирован, реализация — следующий этап.
        with pytest.raises(NotImplementedError):
            SqliteWhitelistRepository(str(tmp_path / "db.sqlite"))


# ---------------------------------- history ----------------------------------
class TestHistoryStore:
    def test_append_and_recent_roundtrip(self, tmp_path):
        store = HistoryStore(str(tmp_path / "hist.json"))
        ev = Event.now(PlateNumber("А123ВС777"), CheckResult.GRANTED,
                       photo_path="photos/x.jpg", gate_opened=True, source=OpenSource.AUTO)
        store.append(ev)
        got = store.recent(10)
        assert len(got) == 1
        e = got[0]
        assert e.plate.value == "А123ВС777"
        assert e.result is CheckResult.GRANTED
        assert e.gate_opened is True
        assert e.source is OpenSource.AUTO
        assert e.photo_path == "photos/x.jpg"

    def test_recent_returns_newest_first(self, tmp_path):
        store = HistoryStore(str(tmp_path / "hist.json"))
        for i in range(3):
            store.append(Event.now(PlateNumber(f"А00{i}ВС77"), CheckResult.DENIED))
        vals = [e.plate.value for e in store.recent(10)]
        assert vals == ["А002ВС77", "А001ВС77", "А000ВС77"]

    def test_recent_limit(self, tmp_path):
        store = HistoryStore(str(tmp_path / "hist.json"))
        for i in range(5):
            store.append(Event.now(None, CheckResult.DENIED))
        assert len(store.recent(2)) == 2

    def test_max_records_trim(self, tmp_path):
        store = HistoryStore(str(tmp_path / "hist.json"), max_records=3)
        for i in range(6):
            store.append(Event.now(None, CheckResult.DENIED, note=str(i)))
        recent = store.recent(10)
        assert len(recent) == 3
        # Остаются последние три записи (5,4,3).
        assert [e.note for e in recent] == ["5", "4", "3"]

    def test_event_without_plate(self, tmp_path):
        store = HistoryStore(str(tmp_path / "hist.json"))
        store.append(Event.now(None, CheckResult.UNREADABLE))
        assert store.recent(1)[0].plate is None


# -------------------------------- PhotoStorage -------------------------------
class TestPhotoStorage:
    def test_save_returns_path_with_plate(self, tmp_path):
        saved = {}

        def fake_writer(path, frame):
            saved["path"] = path
            return True

        ps = PhotoStorage(str(tmp_path / "photos"), writer=fake_writer)
        path = ps.save(frame=object(), plate=PlateNumber("А123ВС777"))
        assert path is not None
        assert path.endswith("_А123ВС777.jpg")
        assert saved["path"] == path

    def test_save_unknown_when_no_plate(self, tmp_path):
        ps = PhotoStorage(str(tmp_path / "photos"), writer=lambda p, f: True)
        path = ps.save(frame=object(), plate=None)
        assert path.endswith("_unknown.jpg")

    def test_save_none_frame_returns_none(self, tmp_path):
        ps = PhotoStorage(str(tmp_path / "photos"), writer=lambda p, f: True)
        assert ps.save(frame=None) is None

    def test_writer_failure_returns_none(self, tmp_path):
        ps = PhotoStorage(str(tmp_path / "photos"), max_files=0, writer=lambda p, f: False)
        assert ps.save(frame=object()) is None

    def test_prune_keeps_only_newest(self, tmp_path):
        import os
        d = tmp_path / "photos"
        d.mkdir()
        for i in range(6):
            f = d / f"{i:03d}.jpg"
            f.write_bytes(b"x")
            os.utime(f, (i, i))  # возрастающее время модификации
        PhotoStorage(str(d), max_files=3)._prune()
        remaining = sorted(p.name for p in d.glob("*.jpg"))
        assert remaining == ["003.jpg", "004.jpg", "005.jpg"]  # старые удалены

    def test_prune_disabled_with_zero(self, tmp_path):
        d = tmp_path / "photos"
        d.mkdir()
        for i in range(5):
            (d / f"{i}.jpg").write_bytes(b"x")
        PhotoStorage(str(d), max_files=0)._prune()
        assert len(list(d.glob("*.jpg"))) == 5
