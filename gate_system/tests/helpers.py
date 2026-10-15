"""Общие помощники тестов: настоящие SQLite-репозитории во временной папке."""
from gate.core.models import PlateNumber
from gate.storage.admins import SqliteAdminRepository
from gate.storage.database import SqliteDatabase
from gate.storage.history import SqliteHistoryRepository
from gate.storage.whitelist import SqliteWhitelistRepository


def make_repos(tmp_path, plates=(), admins=()):
    """Вернуть (whitelist, admins, history) поверх одной базы в tmp_path."""
    db = SqliteDatabase(tmp_path / "test.db")
    whitelist = SqliteWhitelistRepository(db)
    for p in plates:
        whitelist.add(PlateNumber(p))
    admin_repo = SqliteAdminRepository(db)
    for a in admins:
        admin_repo.add(a)
    return whitelist, admin_repo, SqliteHistoryRepository(db)
