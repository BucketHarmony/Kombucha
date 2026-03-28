"""Tests for kombucha.redis_bus — IPC with FakeRedis."""

import json

import pytest

from kombucha.config import RedisConfig
from kombucha.redis_bus import RedisBus, FakeRedis
from kombucha.schemas import (
    SceneState, SceneObject, HardwareContext, SelfModelError,
    Event, SpeechUtterance, MotorCommand, SubsystemHealth,
)


class TestFakeRedis:
    def test_set_get(self, fake_redis):
        fake_redis.set("key", "value")
        assert fake_redis.get("key") == "value"

    def test_get_nonexistent(self, fake_redis):
        assert fake_redis.get("nonexistent") is None

    def test_delete(self, fake_redis):
        fake_redis.set("key", "value")
        fake_redis.delete("key")
        assert fake_redis.get("key") is None

    def test_rpush_lpop(self, fake_redis):
        fake_redis.rpush("list", "a", "b", "c")
        assert fake_redis.lpop("list") == "a"
        assert fake_redis.lpop("list") == "b"
        assert fake_redis.lpop("list") == "c"
        assert fake_redis.lpop("list") is None

    def test_lrange(self, fake_redis):
        fake_redis.rpush("list", "a", "b", "c")
        assert fake_redis.lrange("list", 0, -1) == ["a", "b", "c"]
        assert fake_redis.lrange("list", 0, 1) == ["a", "b"]

    def test_flushdb(self, fake_redis):
        fake_redis.set("key", "value")
        fake_redis.rpush("list", "item")
        fake_redis.flushdb()
        assert fake_redis.get("key") is None
        assert fake_redis.lrange("list", 0, -1) == []


class TestRedisBusScene:
    def test_set_get_scene(self, redis_bus):
        scene = SceneState(
            timestamp="2025-01-01T00:00:00",
            frame_delta=0.05,
            motion_detected=True,
            objects=[SceneObject(cls="person", track_id=1)],
            person_count=1,
        )
        redis_bus.set_scene(scene)
        restored = redis_bus.get_scene()
        assert restored is not None
        assert restored.frame_delta == 0.05
        assert len(restored.objects) == 1
        assert restored.objects[0].cls == "person"

    def test_get_scene_when_empty(self, redis_bus):
        assert redis_bus.get_scene() is None


class TestRedisBusHardware:
    def test_set_get_hardware(self, redis_bus):
        hw = HardwareContext(
            battery_v=11.77,
            cpu_temp_c=52.3,
            pan_position=45,
        )
        redis_bus.set_hardware(hw)
        restored = redis_bus.get_hardware()
        assert restored is not None
        assert restored.battery_v == 11.77

    def test_get_hardware_when_empty(self, redis_bus):
        assert redis_bus.get_hardware() is None


class TestRedisBusSelfModel:
    def test_set_get_self_model(self, redis_bus):
        sme = SelfModelError(frame_delta=0.05, anomaly=True, anomaly_reason="test")
        redis_bus.set_self_model(sme)
        restored = redis_bus.get_self_model()
        assert restored is not None
        assert restored.frame_delta == 0.05
        assert restored.anomaly is True


class TestRedisBusMotor:
    def test_set_get_motor(self, redis_bus):
        cmd = MotorCommand(drive=0.3, turn=10, pan=45)
        redis_bus.set_motor(cmd)
        restored = redis_bus.get_motor()
        assert restored is not None
        assert restored.drive == 0.3
        assert restored.turn == 10
        assert restored.pan == 45
        assert restored.tilt is None

    def test_get_motor_when_empty(self, redis_bus):
        assert redis_bus.get_motor() is None

    def test_set_motor_stop(self, redis_bus):
        cmd = MotorCommand()  # zero drive + zero turn = stop
        redis_bus.set_motor(cmd)
        restored = redis_bus.get_motor()
        assert restored is not None
        assert restored.drive == 0.0
        assert restored.turn == 0.0


class TestRedisBusSpeech:
    def test_append_and_drain_speech(self, redis_bus):
        u1 = SpeechUtterance(text="hello", confidence=0.9)
        u2 = SpeechUtterance(text="how are you", confidence=0.85)
        redis_bus.append_speech(u1)
        redis_bus.append_speech(u2)

        items = redis_bus.drain_speech()
        assert len(items) == 2
        assert items[0].text == "hello"
        assert items[1].text == "how are you"

        # Drain again should be empty
        assert redis_bus.drain_speech() == []

    def test_drain_speech_when_empty(self, redis_bus):
        assert redis_bus.drain_speech() == []


class TestRedisBusSpeechOut:
    def test_push_pop_speech_out(self, redis_bus):
        redis_bus.push_speech_out("I see you")
        redis_bus.push_speech_out("Hello there")
        assert redis_bus.pop_speech_out() == "I see you"
        assert redis_bus.pop_speech_out() == "Hello there"
        assert redis_bus.pop_speech_out() is None


class TestRedisBusDisplay:
    def test_set_get_display(self, redis_bus):
        redis_bus.set_display(["line 0", "line 1", "line 2", "line 3"])
        lines = redis_bus.get_display()
        assert lines == ["line 0", "line 1", "line 2", "line 3"]
        # Second get should return None (consumed)
        assert redis_bus.get_display() is None

    def test_set_get_lights(self, redis_bus):
        redis_bus.set_lights(128, 64)
        lights = redis_bus.get_lights()
        assert lights == {"base": 128, "head": 64}
        assert redis_bus.get_lights() is None


class TestRedisBusEvents:
    def test_publish_and_drain_events(self, redis_bus):
        evt1 = Event(event_type="person_entered", source="reflexive")
        evt2 = Event(event_type="motion_detected", source="reflexive")
        redis_bus.publish_event(evt1)
        redis_bus.publish_event(evt2)

        events = redis_bus.drain_events()
        assert len(events) == 2
        assert events[0].event_type == "person_entered"
        assert events[1].event_type == "motion_detected"

        # Drain again should be empty
        assert redis_bus.drain_events() == []


class TestRedisBusWake:
    def test_publish_and_check_wake(self, redis_bus):
        redis_bus.publish_wake("motion_detected")
        reason = redis_bus.check_wake()
        assert reason == "motion_detected"
        # Second check should return None (consumed)
        assert redis_bus.check_wake() is None


class TestRedisBusHealth:
    def test_set_get_status(self, redis_bus):
        health = {
            "camera": SubsystemHealth(name="camera", status="ok"),
            "serial": SubsystemHealth(name="serial", status="degraded", message="reconnecting"),
        }
        redis_bus.set_status("reflexive", health)
        restored = redis_bus.get_status("reflexive")
        assert restored["camera"].status == "ok"
        assert restored["serial"].status == "degraded"


class TestRedisBusMeta:
    def test_is_fake(self, redis_bus):
        assert redis_bus.is_fake is True

    def test_key_prefix(self, redis_bus):
        redis_bus.set_scene(SceneState(timestamp="test"))
        # Verify the internal key uses the prefix
        raw = redis_bus._redis.get("kombucha:scene")
        assert raw is not None
