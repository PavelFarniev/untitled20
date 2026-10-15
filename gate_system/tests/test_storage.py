"""Тесты атомарного JSON (database.py) и хранилища фото (PhotoStorage).

SQLite-репозитории — в tests/test_sqlite_storage.py."""
import json


from gate.storage import database
from gate.storage.history import PhotoStorage
from gate.core.models import PlateNumber


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
