"""Tests for kombucha.health — HealthMonitor."""

import sqlite3

import pytest

from kombucha.health import HealthMonitor


class TestHealthMonitor:
    def test_camera_none_is_error(self):
        monitor = HealthMonitor()
        result = monitor.check_camera(None)
        assert result.status == "error"

    def test_serial_none_is_error(self):
        monitor = HealthMonitor()
        result = monitor.check_serial(None)
        assert result.status == "error"

    def test_memory_none_is_error(self):
        monitor = HealthMonitor()
        result = monitor.check_memory(None)
        assert result.status == "error"

    def test_memory_ok_with_db(self, tmp_path):
        monitor = HealthMonitor()
        db = sqlite3.connect(str(tmp_path / "test.db"))
        db.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY)")
        result = monitor.check_memory(db)
        assert result.status == "ok"
        assert result.metrics["total_memories"] == 0
        db.close()

    def test_report_all_empty(self):
        monitor = HealthMonitor()
        assert monitor.report_all() == {}

    def test_report_all_after_checks(self):
        monitor = HealthMonitor()
        monitor.check_camera(None)
        monitor.check_serial(None)
        report = monitor.report_all()
        assert "camera" in report
        assert "serial" in report

    def test_is_degraded_when_error(self):
        monitor = HealthMonitor()
        monitor.check_camera(None)
        assert monitor.is_degraded() is True

    def test_not_degraded_when_empty(self):
        monitor = HealthMonitor()
        assert monitor.is_degraded() is False


class TestHealthAudio:
    def test_audio_no_stt_is_degraded(self):
        monitor = HealthMonitor()
        result = monitor.check_audio(stt_listener=None)
        assert result.status == "degraded"
        assert result.metrics["mic_connected"] is False

    def test_audio_with_stt_is_ok(self):
        monitor = HealthMonitor()

        class FakeListener:
            def is_alive(self):
                return True

        result = monitor.check_audio(stt_listener=FakeListener())
        assert result.status == "ok"
        assert result.metrics["mic_connected"] is True

    def test_audio_dead_stt_is_degraded(self):
        monitor = HealthMonitor()

        class DeadListener:
            def is_alive(self):
                return False

        result = monitor.check_audio(stt_listener=DeadListener())
        assert result.status == "degraded"


class TestHealthApi:
    def test_api_ok_on_success(self):
        monitor = HealthMonitor()
        result = monitor.check_api(last_call_ok=True, consecutive_errors=0)
        assert result.status == "ok"

    def test_api_degraded_on_few_errors(self):
        monitor = HealthMonitor()
        result = monitor.check_api(last_call_ok=False, consecutive_errors=2)
        assert result.status == "degraded"

    def test_api_error_on_many_errors(self):
        monitor = HealthMonitor()
        result = monitor.check_api(last_call_ok=False, consecutive_errors=3)
        assert result.status == "error"


class TestHealthRedis:
    def test_redis_none_is_error(self):
        monitor = HealthMonitor()
        result = monitor.check_redis(None)
        assert result.status == "error"

    def test_redis_fake_is_degraded(self):
        monitor = HealthMonitor()

        class FakeBus:
            is_fake = True

        result = monitor.check_redis(FakeBus())
        assert result.status == "degraded"

    def test_redis_real_is_ok(self):
        monitor = HealthMonitor()

        class RealBus:
            is_fake = False

        result = monitor.check_redis(RealBus())
        assert result.status == "ok"


class TestHealthVision:
    def test_vision_both_available_ok(self):
        monitor = HealthMonitor()
        result = monitor.check_vision(detector_available=True, tracker_available=True)
        assert result.status == "ok"

    def test_vision_no_detector_degraded(self):
        monitor = HealthMonitor()
        result = monitor.check_vision(detector_available=False, tracker_available=True)
        assert result.status == "degraded"

    def test_vision_no_tracker_degraded(self):
        monitor = HealthMonitor()
        result = monitor.check_vision(detector_available=True, tracker_available=False)
        assert result.status == "degraded"
