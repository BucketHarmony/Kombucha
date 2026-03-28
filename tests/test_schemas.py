"""Tests for kombucha.schemas — serialization roundtrips, field defaults."""

import json
from datetime import datetime

import pytest

from kombucha.schemas import (
    SceneObject, SceneState, HardwareContext, SelfModelError,
    BrainTickInput, BrainTickOutput, QualiaReport,
    CompressOutput, SessionSummaryOutput,
    Event, SpeechUtterance, SubsystemHealth, MotorCommand,
)


class TestMotorCommand:
    def test_defaults(self):
        m = MotorCommand()
        assert m.drive == 0.0
        assert m.turn == 0.0
        assert m.pan is None
        assert m.tilt is None
        assert m.lights_base is None
        assert m.lights_head is None

    def test_to_dict_includes_drive_turn_excludes_none(self):
        m = MotorCommand(drive=0.3, turn=10)
        d = m.to_dict()
        assert d == {"drive": 0.3, "turn": 10}
        assert "pan" not in d
        assert "lights_base" not in d

    def test_to_dict_includes_set_optionals(self):
        m = MotorCommand(drive=0.0, turn=0.0, pan=45, tilt=-10, lights_head=128)
        d = m.to_dict()
        assert d["drive"] == 0.0
        assert d["turn"] == 0.0
        assert d["pan"] == 45
        assert d["tilt"] == -10
        assert d["lights_head"] == 128
        assert "lights_base" not in d

    def test_from_dict(self):
        d = {"drive": 0.5, "turn": -15, "pan": 90}
        m = MotorCommand.from_dict(d)
        assert m.drive == 0.5
        assert m.turn == -15
        assert m.pan == 90
        assert m.tilt is None

    def test_from_dict_empty(self):
        m = MotorCommand.from_dict({})
        assert m.drive == 0.0
        assert m.turn == 0.0

    def test_from_dict_none(self):
        m = MotorCommand.from_dict(None)
        assert m.drive == 0.0

    def test_from_dict_ignores_extra_keys(self):
        d = {"drive": 0.3, "unknown_field": "ignored"}
        m = MotorCommand.from_dict(d)
        assert m.drive == 0.3

    def test_roundtrip(self):
        m = MotorCommand(drive=0.4, turn=20, pan=-90, tilt=30, lights_base=100, lights_head=200)
        d = m.to_dict()
        m2 = MotorCommand.from_dict(d)
        assert m2.drive == 0.4
        assert m2.turn == 20
        assert m2.pan == -90
        assert m2.tilt == 30
        assert m2.lights_base == 100
        assert m2.lights_head == 200


class TestSceneObject:
    def test_defaults(self):
        obj = SceneObject()
        assert obj.cls == ""
        assert obj.track_id == -1
        assert obj.bbox == (0, 0, 0, 0)
        assert obj.state == "stationary"

    def test_roundtrip(self):
        obj = SceneObject(
            cls="person", track_id=5, bbox=(10, 20, 100, 200),
            centroid=(55, 110), size_pct=0.15, distance_est_m=2.5,
            bearing_deg=-15.0, frames_tracked=10, state="approaching",
        )
        d = obj.to_dict()
        restored = SceneObject.from_dict(d)
        assert restored.cls == "person"
        assert restored.track_id == 5
        assert restored.bbox == (10, 20, 100, 200)
        assert restored.centroid == (55, 110)
        assert restored.state == "approaching"

    def test_from_dict_coerces_lists_to_tuples(self):
        d = {"cls": "cat", "bbox": [1, 2, 3, 4], "centroid": [10, 20]}
        obj = SceneObject.from_dict(d)
        assert isinstance(obj.bbox, tuple)
        assert isinstance(obj.centroid, tuple)


class TestSceneState:
    def test_defaults(self):
        scene = SceneState()
        assert scene.objects == []
        assert scene.person_count == 0
        assert scene.frame_delta is None

    def test_json_roundtrip(self):
        obj = SceneObject(cls="person", track_id=1)
        scene = SceneState(
            timestamp="2025-01-01T00:00:00",
            frame_delta=0.05,
            motion_detected=True,
            objects=[obj],
            person_count=1,
        )
        json_str = scene.to_json()
        restored = SceneState.from_json(json_str)
        assert restored.timestamp == "2025-01-01T00:00:00"
        assert restored.frame_delta == 0.05
        assert restored.motion_detected is True
        assert len(restored.objects) == 1
        assert restored.objects[0].cls == "person"

    def test_extended_fields(self):
        scene = SceneState(
            frame_delta=0.05,
            frame_delta_avg=0.03,
            frame_delta_max=0.08,
            nearest_obstacle_cm=150.0,
            floor_visible=True,
            light_level="dim",
        )
        json_str = scene.to_json()
        restored = SceneState.from_json(json_str)
        assert restored.frame_delta_avg == 0.03
        assert restored.frame_delta_max == 0.08
        assert restored.nearest_obstacle_cm == 150.0
        assert restored.light_level == "dim"

    def test_from_json_ignores_extra_keys(self):
        json_str = json.dumps({"frame_delta": 0.01, "unknown_field": True})
        scene = SceneState.from_json(json_str)
        assert scene.frame_delta == 0.01

    def test_empty_scene_roundtrip(self):
        scene = SceneState()
        json_str = scene.to_json()
        restored = SceneState.from_json(json_str)
        assert restored.objects == []


class TestHardwareContext:
    def test_defaults(self):
        hw = HardwareContext()
        assert hw.battery_v is None
        assert hw.cpu_temp_c is None
        assert hw.battery_pct is None
        assert hw.battery_state == ""
        assert hw.motor_speed_l is None
        assert hw.chassis_moving is False
        assert hw.stuck is False
        assert hw.imu_accel_x is None
        assert hw.tilt_deg is None
        assert hw.lifted is False
        assert hw.fps_actual is None
        assert hw.light_level is None
        assert hw.mic_connected is False
        assert hw.cpu_load is None
        assert hw.uptime_s is None

    def test_json_roundtrip(self):
        hw = HardwareContext(
            timestamp="2025-01-01T00:00:00",
            battery_v=11.77,
            cpu_temp_c=52.3,
            odometer_l=100,
            odometer_r=102,
            pan_position=45,
            tilt_position=-15,
        )
        json_str = hw.to_json()
        restored = HardwareContext.from_json(json_str)
        assert restored.battery_v == 11.77
        assert restored.pan_position == 45

    def test_extended_fields_roundtrip(self):
        hw = HardwareContext(
            battery_pct=75,
            battery_state="discharging",
            motor_speed_l=0.3,
            motor_speed_r=0.31,
            chassis_moving=True,
            imu_accel_z=0.98,
            tilt_deg=5.2,
            fps_actual=8.5,
            light_level="normal",
            cpu_load=1.5,
            uptime_s=3600,
        )
        json_str = hw.to_json()
        restored = HardwareContext.from_json(json_str)
        assert restored.battery_pct == 75
        assert restored.battery_state == "discharging"
        assert restored.motor_speed_l == 0.3
        assert restored.chassis_moving is True
        assert restored.imu_accel_z == 0.98
        assert restored.tilt_deg == 5.2
        assert restored.fps_actual == 8.5
        assert restored.light_level == "normal"
        assert restored.cpu_load == 1.5
        assert restored.uptime_s == 3600

    def test_from_json_ignores_extra_keys(self):
        json_str = json.dumps({"battery_v": 11.5, "future_field": "ignored"})
        hw = HardwareContext.from_json(json_str)
        assert hw.battery_v == 11.5


class TestSelfModelError:
    def test_defaults(self):
        sme = SelfModelError()
        assert sme.anomaly is False
        assert sme.frame_delta is None

    def test_roundtrip(self):
        sme = SelfModelError(
            frame_delta=0.05,
            drive_expected_motion=True,
            motion_detected=True,
            anomaly=False,
        )
        d = sme.to_dict()
        restored = SelfModelError.from_dict(d)
        assert restored.frame_delta == 0.05
        assert restored.drive_expected_motion is True

    def test_from_dict_ignores_extra_keys(self):
        d = {"frame_delta": 0.1, "unknown_field": "ignored"}
        sme = SelfModelError.from_dict(d)
        assert sme.frame_delta == 0.1


class TestQualiaReport:
    def test_defaults(self):
        q = QualiaReport()
        assert q.continuity is None
        assert q.opacity is None

    def test_to_dict_excludes_none(self):
        q = QualiaReport(attention="visual", affect="curious")
        d = q.to_dict()
        assert "attention" in d
        assert "affect" in d
        assert "continuity" not in d

    def test_from_dict_empty(self):
        q = QualiaReport.from_dict({})
        assert q.attention is None

    def test_from_dict_none(self):
        q = QualiaReport.from_dict(None)
        assert q.attention is None


class TestBrainTickInput:
    def test_to_dict_excludes_none(self):
        inp = BrainTickInput(tick=5, current_goal="explore")
        d = inp.to_dict()
        assert d["tick"] == 5
        assert d["current_goal"] == "explore"
        assert "heard" not in d
        assert "operator_message" not in d

    def test_full_input(self):
        inp = BrainTickInput(
            tick=10,
            current_goal="find the door",
            heard=[{"time": "12:00", "text": "hello"}],
            wake_reason="motion_detected",
        )
        d = inp.to_dict()
        assert d["heard"] == [{"time": "12:00", "text": "hello"}]
        assert d["wake_reason"] == "motion_detected"


class TestBrainTickOutput:
    def test_defaults(self):
        out = BrainTickOutput()
        assert out.outcome == "neutral"
        assert out.actions == []
        assert out.next_tick_ms == 3000
        assert out.motor is None
        assert out.speak is None
        assert out.display is None

    def test_from_dict_with_motor(self):
        d = {
            "observation": "dark hallway",
            "goal": "explore",
            "mood": "curious",
            "motor": {"drive": 0.3, "turn": 0},
            "speak": "I see a hallway",
            "display": ["curious", "exploring", "", "hallway"],
            "next_tick_ms": 5000,
            "tags": ["loc:hallway"],
            "outcome": "success",
        }
        out = BrainTickOutput.from_dict(d)
        assert out.observation == "dark hallway"
        assert out.motor == {"drive": 0.3, "turn": 0}
        assert out.speak == "I see a hallway"
        assert out.display == ["curious", "exploring", "", "hallway"]
        assert out.outcome == "success"

    def test_from_dict_ignores_unknown_keys(self):
        d = {"observation": "test", "unknown_field": "ignored"}
        out = BrainTickOutput.from_dict(d)
        assert out.observation == "test"


class TestCompressOutput:
    def test_from_dict(self):
        d = {
            "spatial": "Explored hallway",
            "lessons": ["Drive slow in tight spaces"],
            "tags": ["loc:hallway", "lesson:drive"],
        }
        out = CompressOutput.from_dict(d)
        assert out.spatial == "Explored hallway"
        assert len(out.lessons) == 1
        assert len(out.tags) == 2


class TestSessionSummaryOutput:
    def test_from_dict(self):
        d = {
            "arc": "Explored the house",
            "open_threads": ["What's behind the bedroom door?"],
            "tags": ["loc:house"],
        }
        out = SessionSummaryOutput.from_dict(d)
        assert out.arc == "Explored the house"
        assert len(out.open_threads) == 1


class TestEvent:
    def test_json_roundtrip(self):
        evt = Event(
            event_type="person_entered",
            source="reflexive",
            data={"track_id": 3, "bearing_deg": -15.0},
        )
        json_str = evt.to_json()
        restored = Event.from_json(json_str)
        assert restored.event_type == "person_entered"
        assert restored.source == "reflexive"
        assert restored.data["track_id"] == 3

    def test_timestamp_auto_populated(self):
        evt = Event(event_type="test")
        assert evt.timestamp != ""


class TestSpeechUtterance:
    def test_json_roundtrip(self):
        u = SpeechUtterance(
            text="Hello Kombucha",
            confidence=0.95,
            time_short="14:30:00",
        )
        json_str = u.to_json()
        restored = SpeechUtterance.from_json(json_str)
        assert restored.text == "Hello Kombucha"
        assert restored.confidence == 0.95

    def test_timestamp_auto_populated(self):
        u = SpeechUtterance(text="hi")
        assert u.timestamp != ""


class TestSubsystemHealth:
    def test_defaults(self):
        h = SubsystemHealth()
        assert h.status == "unknown"

    def test_roundtrip(self):
        h = SubsystemHealth(
            name="camera",
            status="ok",
            last_check="2025-01-01T00:00:00",
            message="Frame capture working",
            metrics={"fps": 10, "dropped": 0},
        )
        d = h.to_dict()
        restored = SubsystemHealth.from_dict(d)
        assert restored.name == "camera"
        assert restored.status == "ok"
        assert restored.metrics["fps"] == 10
