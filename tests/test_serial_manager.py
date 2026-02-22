"""Tests for kombucha.serial_manager — T-code validation and SerialManager."""

import json

import pytest

from kombucha.config import SerialConfig
from kombucha.serial_manager import (
    SerialManager, validate_tcode, _clamp, ESP32_INIT_CMDS,
)


# ===========================================================================
# SerialManager in debug mode
# ===========================================================================

class TestSerialManagerDebug:
    def test_connect_in_debug_mode(self):
        config = SerialConfig()
        sm = SerialManager(config, debug_mode=True)
        assert sm.connect() is True
        assert sm.is_connected is True

    def test_send_in_debug_mode(self):
        config = SerialConfig()
        sm = SerialManager(config, debug_mode=True)
        sm.connect()
        assert sm.send({"T": 0}) is True
        assert sm.last_command == {"T": 0}

    def test_read_telemetry_in_debug_mode(self):
        """Debug mode still reads system metrics (may fail on non-Linux)."""
        config = SerialConfig()
        sm = SerialManager(config, debug_mode=True)
        sm.connect()
        telemetry = sm.read_telemetry()
        assert isinstance(telemetry, dict)
        # No ESP32 data in debug mode
        assert "odometer_l" not in telemetry

    def test_close_in_debug_mode(self):
        config = SerialConfig()
        sm = SerialManager(config, debug_mode=True)
        sm.connect()
        sm.close()
        # After close, port is None but debug mode still reports connected
        assert sm.is_connected is True  # debug_mode always True

    def test_reconnect_in_debug_mode(self):
        config = SerialConfig()
        sm = SerialManager(config, debug_mode=True)
        assert sm.reconnect() is True

    def test_is_connected_false_without_connect(self):
        config = SerialConfig()
        sm = SerialManager(config, debug_mode=False)
        assert sm.is_connected is False

    def test_send_fails_without_port(self):
        config = SerialConfig()
        sm = SerialManager(config, debug_mode=False)
        # No port opened, send should return False
        assert sm.send({"T": 0}) is False


# ===========================================================================
# ESP32 init commands
# ===========================================================================

class TestESP32Init:
    def test_init_cmds_are_valid_json(self):
        for cmd in ESP32_INIT_CMDS:
            assert isinstance(cmd, dict)
            assert "T" in cmd
            json.dumps(cmd)  # should not raise

    def test_init_cmd_count(self):
        assert len(ESP32_INIT_CMDS) == 5
