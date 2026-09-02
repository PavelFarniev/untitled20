"""Тесты сборки номера по кадрам (aggregator.py)."""
from aggregator import PlateAggregator


class TestPlateAggregator:
    def test_assembles_from_real_observed_reads(self):
        # Ровно те строки, что выдавал Nomeroff на смазанном ночном ролике:
        # основная часть верна на одних кадрах, регион — на других.
        reads = ["O02MC177", "X002MC", "X002MC", "O02MC177",
                 "", "O02MC1777", "C22MC1777", ""]
        agg = PlateAggregator(confirm=2)
        result = None
        for r in reads:
            got = agg.add(r)
            if got is not None:
                result = got
        assert result is not None
        assert result.value == "Х002МС177"   # собрано голосованием

    def test_single_clear_read_with_confirm_1(self):
        agg = PlateAggregator(confirm=1)
        assert agg.add("A123BC777").value == "А123ВС777"

    def test_not_emitted_before_confirm(self):
        agg = PlateAggregator(confirm=3)
        assert agg.add("A123BC777") is None
        assert agg.add("A123BC777") is None
        assert agg.add("A123BC777").value == "А123ВС777"  # третий раз — выдаёт

    def test_not_emitted_twice(self):
        agg = PlateAggregator(confirm=1)
        assert agg.add("A123BC777").value == "А123ВС777"
        assert agg.add("A123BC777") is None   # тот же номер сразу повторно не выдаётся

    def test_reemitted_after_cooldown(self):
        t = [0.0]
        agg = PlateAggregator(confirm=1, reemit_after_s=30, clock=lambda: t[0])
        assert agg.add("A123BC777").value == "А123ВС777"
        assert agg.add("A123BC777") is None       # в пределах паузы — нет
        t[0] = 31                                 # прошло больше паузы
        assert agg.add("A123BC777").value == "А123ВС777"  # снова можно (машина вернулась)

    def test_region_voting_beats_noise(self):
        # Регион 177 встречается чаще, чем ошибочные варианты — он и побеждает.
        agg = PlateAggregator(confirm=2)
        for r in ["X002MC177", "X002MC17", "X002MC177"]:
            got = agg.add(r)
        assert got is not None and got.value == "Х002МС177"

    def test_garbage_does_not_emit(self):
        agg = PlateAggregator(confirm=2)
        for r in ["???", "RUS", "12", ""]:
            assert agg.add(r) is None

    def test_reset_clears_state(self):
        agg = PlateAggregator(confirm=1)
        agg.add("A123BC777")
        agg.reset()
        assert agg.add("A123BC777").value == "А123ВС777"  # снова можно выдать
