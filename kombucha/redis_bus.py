"""Redis IPC bus for Kombucha v2.

Provides publish/subscribe, key-value, and list operations for
inter-layer communication. Falls back to an in-memory FakeRedis
when Redis is not available or during testing.
"""

import json
import logging
import time
from typing import Optional

from kombucha.config import RedisConfig
from kombucha.schemas import (
    SceneState, HardwareContext, SelfModelError,
    Event, SpeechUtterance, MotorCommand, SubsystemHealth,
)

log = logging.getLogger("kombucha.redis")


class FakeRedis:
    """In-memory Redis substitute for testing and single-process mode."""

    def __init__(self):
        self._data: dict[str, str] = {}
        self._lists: dict[str, list[str]] = {}
        self._subscribers: dict[str, list] = {}
        self._ttls: dict[str, float] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value
        if ex is not None:
            self._ttls[key] = time.time() + ex

    def get(self, key: str) -> Optional[str]:
        if key in self._ttls and time.time() > self._ttls[key]:
            del self._data[key]
            del self._ttls[key]
            return None
        return self._data.get(key)

    def delete(self, *keys: str) -> None:
        for key in keys:
            self._data.pop(key, None)
            self._ttls.pop(key, None)
            self._lists.pop(key, None)

    def rpush(self, key: str, *values: str) -> int:
        if key not in self._lists:
            self._lists[key] = []
        self._lists[key].extend(values)
        return len(self._lists[key])

    def lpop(self, key: str) -> Optional[str]:
        lst = self._lists.get(key, [])
        if lst:
            return lst.pop(0)
        return None

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        lst = self._lists.get(key, [])
        if end == -1:
            return lst[start:]
        return lst[start:end + 1]

    def ltrim(self, key: str, start: int, end: int) -> None:
        lst = self._lists.get(key, [])
        if end == -1:
            self._lists[key] = lst[start:]
        else:
            self._lists[key] = lst[start:end + 1]

    def publish(self, channel: str, message: str) -> int:
        callbacks = self._subscribers.get(channel, [])
        for cb in callbacks:
            cb(message)
        return len(callbacks)

    def hset(self, key: str, mapping: dict) -> None:
        if key not in self._data:
            self._data[key] = {}
        if isinstance(self._data[key], dict):
            self._data[key].update({k: str(v) for k, v in mapping.items()})

    def hgetall(self, key: str) -> dict:
        val = self._data.get(key, {})
        if isinstance(val, dict):
            return val
        return {}

    def flushdb(self) -> None:
        self._data.clear()
        self._lists.clear()
        self._subscribers.clear()
        self._ttls.clear()


class RedisBus:
    """Redis IPC wrapper for Kombucha inter-layer communication."""

    def __init__(self, config: RedisConfig, client=None):
        self._prefix = config.key_prefix
        self._config = config
        if client is not None:
            self._redis = client
        else:
            try:
                import redis as redis_lib
                self._redis = redis_lib.Redis(
                    host=config.host,
                    port=config.port,
                    db=config.db,
                    decode_responses=True,
                )
                self._redis.ping()
                log.info(f"Redis connected: {config.host}:{config.port}")
            except Exception:
                log.warning("Redis not available, using in-memory FakeRedis")
                self._redis = FakeRedis()

    def _key(self, name: str) -> str:
        return f"{self._prefix}{name}"

    @property
    def is_fake(self) -> bool:
        return isinstance(self._redis, FakeRedis)

    # --- Scene (reflexive → brain) ---

    def set_scene(self, scene: SceneState) -> None:
        self._redis.set(
            self._key("scene"),
            scene.to_json(),
            ex=self._config.scene_ttl_s,
        )

    def get_scene(self) -> Optional[SceneState]:
        raw = self._redis.get(self._key("scene"))
        if raw is None:
            return None
        return SceneState.from_json(raw)

    # --- Hardware (reflexive → brain) ---

    def set_hardware(self, hw: HardwareContext) -> None:
        self._redis.set(
            self._key("hardware"),
            hw.to_json(),
            ex=self._config.hardware_ttl_s,
        )

    def get_hardware(self) -> Optional[HardwareContext]:
        raw = self._redis.get(self._key("hardware"))
        if raw is None:
            return None
        return HardwareContext.from_json(raw)

    # --- Self-model (reflexive → brain) ---

    def set_self_model(self, sme: SelfModelError) -> None:
        self._redis.set(
            self._key("self_model"),
            json.dumps(sme.to_dict()),
            ex=self._config.scene_ttl_s,
        )

    def get_self_model(self) -> Optional[SelfModelError]:
        raw = self._redis.get(self._key("self_model"))
        if raw is None:
            return None
        return SelfModelError.from_dict(json.loads(raw))

    # --- Motor command (brain → reflexive) ---

    def set_motor(self, cmd: MotorCommand) -> None:
        self._redis.set(
            self._key("motor"),
            json.dumps(cmd.to_dict()),
            ex=self._config.motor_ttl_s,
        )

    def get_motor(self) -> Optional[MotorCommand]:
        raw = self._redis.get(self._key("motor"))
        if raw is None:
            return None
        return MotorCommand.from_dict(json.loads(raw))

    # --- Speech (voice → brain) ---

    def append_speech(self, utterance: SpeechUtterance) -> None:
        self._redis.rpush(self._key("speech_in"), utterance.to_json())

    def drain_speech(self) -> list[SpeechUtterance]:
        items = self._redis.lrange(self._key("speech_in"), 0, -1)
        if items:
            self._redis.delete(self._key("speech_in"))
        return [SpeechUtterance.from_json(i) for i in items]

    # --- Speech out (brain → voice) ---

    def push_speech_out(self, text: str) -> None:
        self._redis.rpush(self._key("speech_out"), text)

    def pop_speech_out(self) -> Optional[str]:
        return self._redis.lpop(self._key("speech_out"))

    # --- Display / Lights (brain → reflexive) ---

    def set_display(self, lines: list[str]) -> None:
        self._redis.set(self._key("display"), json.dumps(lines), ex=30)

    def get_display(self) -> Optional[list[str]]:
        raw = self._redis.get(self._key("display"))
        if raw is None:
            return None
        self._redis.delete(self._key("display"))
        return json.loads(raw)

    def set_lights(self, base: int, head: int) -> None:
        self._redis.set(
            self._key("lights"),
            json.dumps({"base": base, "head": head}),
            ex=30,
        )

    def get_lights(self) -> Optional[dict]:
        raw = self._redis.get(self._key("lights"))
        if raw is None:
            return None
        self._redis.delete(self._key("lights"))
        return json.loads(raw)

    # --- Events ---

    def publish_event(self, event: Event) -> None:
        self._redis.rpush(self._key("events"), event.to_json())
        self._redis.ltrim(self._key("events"), -100, -1)

    def drain_events(self) -> list[Event]:
        items = self._redis.lrange(self._key("events"), 0, -1)
        if items:
            self._redis.delete(self._key("events"))
        return [Event.from_json(i) for i in items]

    # --- Wake signal ---

    def publish_wake(self, reason: str) -> None:
        self._redis.set(self._key("wake"), reason, ex=10)

    def check_wake(self) -> Optional[str]:
        raw = self._redis.get(self._key("wake"))
        if raw:
            self._redis.delete(self._key("wake"))
        return raw

    # --- Health status ---

    def set_status(self, layer: str, health: dict[str, SubsystemHealth]) -> None:
        mapping = {k: json.dumps(v.to_dict()) for k, v in health.items()}
        self._redis.hset(self._key(f"status:{layer}"), mapping=mapping)

    def get_status(self, layer: str) -> dict[str, SubsystemHealth]:
        raw = self._redis.hgetall(self._key(f"status:{layer}"))
        return {k: SubsystemHealth.from_dict(json.loads(v)) for k, v in raw.items()}
