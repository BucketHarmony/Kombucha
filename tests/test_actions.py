"""Tests for kombucha.actions — action translation and execution."""

import pytest

from kombucha.serial_manager import validate_tcode, _clamp
from kombucha.actions import translate_action, execute_actions


# ===========================================================================
# T-Code Validation
# ===========================================================================

class TestTCodeValidation:
    def test_validate_drive(self):
        cmd = validate_tcode(1, {"L": 0.5, "R": 0.3})
        assert cmd == {"T": 1, "L": 0.5, "R": 0.3}

    def test_validate_drive_clamps(self):
        cmd = validate_tcode(1, {"L": 5.0, "R": -5.0})
        assert cmd["L"] == 1.3
        assert cmd["R"] == -1.3

    def test_validate_stop(self):
        cmd = validate_tcode(0, {})
        assert cmd == {"T": 0}

    def test_validate_oled(self):
        cmd = validate_tcode(3, {"lineNum": 0, "Text": "hello"})
        assert cmd == {"T": 3, "lineNum": 0, "Text": "hello"}

    def test_validate_oled_truncates_text(self):
        cmd = validate_tcode(3, {"lineNum": 0, "Text": "x" * 30})
        assert len(cmd["Text"]) == 20

    def test_validate_gimbal(self):
        cmd = validate_tcode(133, {"X": 45, "Y": 30, "SPD": 100, "ACC": 10})
        assert cmd == {"T": 133, "X": 45, "Y": 30, "SPD": 100, "ACC": 10}

    def test_validate_gimbal_clamps(self):
        cmd = validate_tcode(133, {"X": 200, "Y": -60, "SPD": 300, "ACC": 100})
        assert cmd["X"] == 180
        assert cmd["Y"] == -30
        assert cmd["SPD"] == 200
        assert cmd["ACC"] == 50

    def test_validate_led(self):
        cmd = validate_tcode(132, {"IO4": 128, "IO5": 64})
        assert cmd == {"T": 132, "IO4": 128, "IO5": 64}

    def test_validate_led_clamps(self):
        cmd = validate_tcode(132, {"IO4": 300, "IO5": -10})
        assert cmd["IO4"] == 255
        assert cmd["IO5"] == 0

    def test_validate_unknown_tcode_returns_none(self):
        assert validate_tcode(999, {}) is None

    def test_validate_bad_params_returns_none(self):
        assert validate_tcode(1, {"L": "not_a_number"}) is None


# ===========================================================================
# Clamp
# ===========================================================================

class TestClamp:
    def test_clamp_within_range(self):
        assert _clamp(5, 0, 10) == 5

    def test_clamp_below(self):
        assert _clamp(-5, 0, 10) == 0

    def test_clamp_above(self):
        assert _clamp(15, 0, 10) == 10


# ===========================================================================
# Action Translation
# ===========================================================================

class TestActionTranslation:
    def test_display_generates_4_commands(self):
        state = {}
        cmds = translate_action({"type": "display", "lines": ["a", "b", "c", "d"]}, state)
        assert len(cmds) == 4
        assert all(c["T"] == 3 for c in cmds)

    def test_oled_single_line(self):
        state = {}
        cmds = translate_action({"type": "oled", "line": 2, "text": "hello"}, state)
        assert len(cmds) == 1
        assert cmds[0]["lineNum"] == 2

    def test_speak_returns_empty(self):
        state = {}
        cmds = translate_action({"type": "speak", "text": "hello"}, state)
        assert cmds == []

    def test_unknown_action_returns_empty(self):
        state = {}
        cmds = translate_action({"type": "unknown_type"}, state)
        assert cmds == []

    def test_non_dict_returns_empty(self):
        state = {}
        cmds = translate_action("drive", state)
        assert cmds == []
