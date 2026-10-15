"""Пути проекта.

Все относительные пути из config.json (модели, база, логи, фото, состояние
прокси) считаются от КОРНЯ ПРОЕКТА, а не от текущей папки запуска. Поэтому
система одинаково работает при запуске `python main.py` из корня, из systemd
(WorkingDirectory) и из утилит в tools/.
"""
from __future__ import annotations

from pathlib import Path

# gate/paths.py -> gate/ -> корень проекта
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_FILE = PROJECT_ROOT / "config" / "config.json"


def resolve(path: str | Path) -> Path:
    """Абсолютный путь: относительный считается от корня проекта."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def resolve_source(source: str) -> str:
    """Источник видео: URL (rtsp://, http://) и номер устройства — как есть,
    путь к видеофайлу — относительно корня проекта."""
    if not source or "://" in source or source.isdigit():
        return source
    return str(resolve(source))
