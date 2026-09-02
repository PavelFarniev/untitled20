"""Юнит-тесты логирования (logger.py)."""
import logging
import sys
import threading

from logger import setup_logging


class TestSetupLogging:
    def test_creates_log_file_and_handlers(self, tmp_path):
        logs_dir = tmp_path / "logs"
        log = setup_logging(str(logs_dir), level="DEBUG")
        log.info("проверка записи в файл")

        for h in logging.getLogger().handlers:
            h.flush()

        log_file = logs_dir / "gate_system.log"
        assert log_file.exists()
        assert "проверка записи в файл" in log_file.read_text(encoding="utf-8")

    def test_level_applied(self, tmp_path):
        setup_logging(str(tmp_path / "logs"), level="WARNING")
        assert logging.getLogger().level == logging.WARNING

    def test_has_file_and_console_handlers(self, tmp_path):
        setup_logging(str(tmp_path / "logs"))
        handler_types = {type(h).__name__ for h in logging.getLogger().handlers}
        assert "RotatingFileHandler" in handler_types
        assert "StreamHandler" in handler_types

    def test_exception_hooks_installed(self, tmp_path):
        setup_logging(str(tmp_path / "logs"))
        # Глобальные перехватчики исключений должны быть заменены нашими.
        assert sys.excepthook is not sys.__excepthook__
        assert threading.excepthook is not None

    def test_rotation_limits_applied(self, tmp_path):
        from logging.handlers import RotatingFileHandler
        setup_logging(str(tmp_path / "logs"), max_mb=2.0, backups=4)
        handlers = [h for h in logging.getLogger().handlers
                    if isinstance(h, RotatingFileHandler)]
        assert handlers
        h = handlers[0]
        assert h.maxBytes == int(2.0 * 1024 * 1024)
        assert h.backupCount == 4
