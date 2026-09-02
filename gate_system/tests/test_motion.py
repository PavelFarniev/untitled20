"""Тесты определения движения номера (motion.py)."""
from motion import MotionGate
from models import BBox

W = H = 1000  # удобный размер кадра: 1 пиксель = 0.001 нормированно


def box_at(cx, cy, half=10):
    return BBox(cx - half, cy - half, cx + half, cy + half)


class TestMotionGate:
    def test_stationary_not_moving(self):
        mg = MotionGate(threshold=0.02, confirm_frames=2)
        for _ in range(5):
            mg.observe([box_at(500, 500)], W, H)
        assert mg.is_moving() is False

    def test_moving_after_confirm(self):
        mg = MotionGate(threshold=0.02, confirm_frames=2)
        mg.observe([box_at(500, 500)], W, H)   # старт, prev нет
        mg.observe([box_at(560, 500)], W, H)   # сдвиг 0.06 → 1-й кадр движения
        assert mg.is_moving() is False          # ещё не подтверждено
        mg.observe([box_at(620, 500)], W, H)   # 2-й кадр движения
        assert mg.is_moving() is True

    def test_jitter_below_threshold_ignored(self):
        mg = MotionGate(threshold=0.02, confirm_frames=1)
        mg.observe([box_at(500, 500)], W, H)
        mg.observe([box_at(505, 500)], W, H)   # сдвиг 0.005 < 0.02 → не движение
        assert mg.is_moving() is False

    def test_occlusion_does_not_count_as_motion(self):
        mg = MotionGate(threshold=0.02, confirm_frames=1)
        mg.observe([box_at(500, 500)], W, H)
        mg.observe([], W, H)                    # человек перекрыл номер — кадр без рамки
        mg.observe([box_at(500, 500)], W, H)   # номер вернулся на то же место
        assert mg.is_moving() is False

    def test_stop_resets_streak(self):
        mg = MotionGate(threshold=0.02, confirm_frames=2)
        mg.observe([box_at(500, 500)], W, H)
        mg.observe([box_at(560, 500)], W, H)
        mg.observe([box_at(620, 500)], W, H)
        assert mg.is_moving() is True
        mg.observe([box_at(620, 500)], W, H)   # остановилась
        assert mg.is_moving() is False          # серия движения сброшена

    def test_no_boxes_no_crash(self):
        mg = MotionGate()
        mg.observe([], W, H)
        assert mg.is_moving() is False
