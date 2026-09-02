"""Тесты адаптера NomeroffOcrEngine (без установленного nomeroff-net).

Тяжёлая библиотека не нужна: логика распознавания инъектируется через
`recognizer`, поэтому проверяем разбор кандидатов и нормализацию номера.
"""
from nomeroff_ocr import NomeroffOcrEngine, _flatten_texts
from models import OcrResult


class TestFlattenTexts:
    def test_empty(self):
        assert _flatten_texts([]) == []

    def test_list_of_lists(self):
        # Nomeroff отдаёт тексты как список-на-изображение из списка номеров.
        assert _flatten_texts([["T198TT197"]]) == ["T198TT197"]

    def test_multiple_plates(self):
        assert _flatten_texts([["A123BC777", "M001MM77"]]) == ["A123BC777", "M001MM77"]


class TestNomeroffOcrEngine:
    def test_reads_and_normalizes_latin_plate(self):
        # Nomeroff обычно отдаёт номер латиницей — адаптер приводит к кириллице.
        eng = NomeroffOcrEngine(recognizer=lambda img: ["T198TT197"])
        res = eng.read_text(plate_image=object())
        assert res.is_readable
        assert res.plate.value == "Т198ТТ197"
        assert res.confidence == 1.0

    def test_picks_first_valid_candidate(self):
        eng = NomeroffOcrEngine(recognizer=lambda img: ["мусор", "А123ВС777"])
        res = eng.read_text(plate_image=object())
        assert res.plate.value == "А123ВС777"

    def test_no_valid_plate(self):
        eng = NomeroffOcrEngine(recognizer=lambda img: ["???"])
        res = eng.read_text(plate_image=object())
        assert res.plate is None
        assert res.raw_text == "???"

    def test_empty_result(self):
        eng = NomeroffOcrEngine(recognizer=lambda img: [])
        res = eng.read_text(plate_image=object())
        assert isinstance(res, OcrResult) and res.plate is None

    def test_recognizer_exception_is_swallowed(self):
        def boom(img):
            raise RuntimeError("model error")

        eng = NomeroffOcrEngine(recognizer=boom)
        res = eng.read_text(plate_image=object())
        assert res.plate is None and res.raw_text == ""
