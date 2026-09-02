"""Юнит-тесты доменного слоя (models.py)."""
from datetime import datetime, timezone

import pytest

from models import (
    AccessResult,
    BBox,
    CheckResult,
    Event,
    HealthStatus,
    OcrResult,
    OpenSource,
    PlateNumber,
    Vehicle,
)


class TestPlateNumber:
    def test_normalization_uppercase_and_strip(self):
        # Пробелы обрезаются, регистр приводится к верхнему.
        assert PlateNumber("  а123вс777 ").value == "А123ВС777"

    def test_internal_spaces_removed(self):
        assert PlateNumber("А 123 ВС 777").value == "А123ВС777"

    def test_str_returns_value(self):
        assert str(PlateNumber("м001мм77")) == "М001ММ77"

    def test_frozen_and_hashable(self):
        # Неизменяемость нужна для использования как ключа/в множествах.
        p = PlateNumber("Е777ОР50")
        assert p in {PlateNumber("Е777ОР50")}
        with pytest.raises(Exception):
            p.value = "X"  # type: ignore[misc]

    def test_equality_after_normalization(self):
        assert PlateNumber("а123вс777") == PlateNumber("А123ВС777")


class TestBBox:
    def test_width_height(self):
        b = BBox(10, 20, 40, 80, confidence=0.9)
        assert b.width == 30
        assert b.height == 60
        assert b.confidence == 0.9


class TestOcrResult:
    def test_is_readable_true(self):
        r = OcrResult(plate=PlateNumber("А123ВС777"), confidence=0.8, raw_text="A123BC777")
        assert r.is_readable is True

    def test_is_readable_false(self):
        r = OcrResult(plate=None, confidence=0.1, raw_text="???")
        assert r.is_readable is False


class TestEnums:
    def test_open_source_values(self):
        assert {s.value for s in OpenSource} == {"auto", "telegram", "manual"}

    def test_check_result_values(self):
        assert {c.value for c in CheckResult} == {"granted", "denied", "unreadable"}


class TestEvent:
    def test_now_factory_sets_utc_timestamp(self):
        e = Event.now(PlateNumber("А123ВС777"), CheckResult.GRANTED,
                      gate_opened=True, source=OpenSource.AUTO, photo_path="p.jpg")
        assert e.timestamp.tzinfo == timezone.utc
        assert e.plate.value == "А123ВС777"
        assert e.result is CheckResult.GRANTED
        assert e.gate_opened is True
        assert e.source is OpenSource.AUTO
        assert e.photo_path == "p.jpg"

    def test_now_defaults(self):
        e = Event.now(None, CheckResult.DENIED)
        assert e.plate is None
        assert e.gate_opened is False
        assert e.source is None
        assert isinstance(e.timestamp, datetime)


class TestAccessResultAndHealth:
    def test_access_result_fields(self):
        ev = Event.now(None, CheckResult.DENIED)
        ar = AccessResult(CheckResult.DENIED, False, ev)
        assert ar.suppressed_by_antireplay is False

    def test_health_status_defaults(self):
        h = HealthStatus()
        assert h.camera_ok is False
        assert h.extra == {}

    def test_vehicle_optional_fields(self):
        v = Vehicle(vehicle_box=BBox(0, 0, 10, 10))
        assert v.plate_box is None
        assert v.ocr is None
