"""Тесты RTSPCamera на видео-заглушке (без реального RTSP).

Источником для cv2.VideoCapture может быть и видеофайл — это позволяет
протестировать логику чтения и переподключения детерминированно.
"""
import cv2
import numpy as np
import pytest

from camera import RTSPCamera


@pytest.fixture()
def video_file(tmp_path):
    """Сгенерировать короткий тестовый видеоролик (10 кадров 64x48)."""
    path = tmp_path / "clip.avi"
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    writer = cv2.VideoWriter(str(path), fourcc, 10.0, (64, 48))
    assert writer.isOpened(), "VideoWriter не открылся"
    for i in range(10):
        frame = np.full((48, 64, 3), i * 20, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return str(path)


class TestReading:
    def test_reads_all_frames(self, video_file):
        cam = RTSPCamera(video_file)
        cam.open()
        assert cam.is_alive() is True

        count = 0
        while True:
            frame = cam.read()
            if frame is None:
                break
            assert frame.shape == (48, 64, 3)
            count += 1
        cam.close()
        assert count == 10

    def test_read_before_open_returns_none(self, video_file):
        cam = RTSPCamera(video_file)
        assert cam.read() is None
        assert cam.is_alive() is False


class TestBadSource:
    def test_open_bad_source_not_alive(self, tmp_path):
        cam = RTSPCamera(str(tmp_path / "does_not_exist.avi"))
        cam.open()
        assert cam.is_alive() is False
        assert cam.read() is None


class TestReconnectBackoff:
    def test_exponential_backoff_on_failure(self, tmp_path):
        delays = []
        cam = RTSPCamera(
            str(tmp_path / "missing.avi"),
            min_delay_s=1.0,
            max_delay_s=8.0,
            sleeper=delays.append,  # не спим реально, только фиксируем задержку
        )
        for _ in range(5):
            cam.reconnect()
        # Задержка растёт 1→2→4→8 и упирается в максимум 8.
        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]

    def test_backoff_resets_after_success(self, video_file):
        delays = []
        cam = RTSPCamera(video_file, min_delay_s=1.0, max_delay_s=30.0,
                         sleeper=delays.append)
        # Искусственно «раздуем» текущую задержку, как после серии сбоев.
        cam._current_delay = 16.0
        cam.reconnect()
        assert cam.is_alive() is True
        # После успешного подключения следующая задержка снова минимальная.
        assert cam._current_delay == 1.0
        cam.close()


class TestSourceMasking:
    def test_password_masked_in_logs(self):
        cam = RTSPCamera("rtsp://admin:secret@192.168.1.10:554/stream1")
        masked = cam._safe_source()
        assert "secret" not in masked
        assert "admin" in masked and "***" in masked
