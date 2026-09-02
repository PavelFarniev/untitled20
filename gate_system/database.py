"""Низкоуровневый доступ к хранилищу.

Сейчас — атомарное чтение/запись JSON. На следующих этапах здесь появится
слой SQLite, при этом репозитории (whitelist.py, history.py) менять не придётся:
они работают через порты, а не напрямую с этим модулем.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: str | Path, default: Any) -> Any:
    """Прочитать JSON-файл, вернуть default при отсутствии/повреждении."""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json_atomic(path: str | Path, data: Any) -> None:
    """Атомарно записать JSON (через временный файл + os.replace).

    Защищает от повреждения файла при внезапном отключении питания посреди
    записи — критично для системы, работающей 24/7.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
