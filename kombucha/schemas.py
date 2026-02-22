"""Shared data structures for Kombucha v2.

All data exchanged between layers (reflexive, voice, brain) and stored in
Redis or the database is defined here as dataclasses with JSON
serialization helpers.
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Directive — high-level behavioral mode set by the brain
# ---------------------------------------------------------------------------

class Directive(str, Enum):
    """Deprecated: kept for backward compat with DB columns."""
    EXPLORE = "explore"
    APPROACH_PERSON = "approach_person"
    HOLD_POSITION = "hold_position"
    FOLLOW = "follow"
    RETREAT = "retreat"
    SENTRY = "sentry"
    MANUAL = "manual"


# ---------------------------------------------------------------------------
# MotorCommand — brain → reflexive (replaces Directive)
# ---------------------------------------------------------------------------

@dataclass
class MotorCommand:
    drive: float = 0.0          # m/s, positive = forward, negative = reverse
    turn: float = 0.0           # deg/s, positive = left, negative = right
    pan: Optional[float] = None   # absolute degrees (-180..180), None = no change
    tilt: Optional[float] = None  # absolute degrees (-30..90), None = no change
    lights_base: Optional[int] = None  # 0-255, None = no change
    lights_head: Optional[int] = None  # 0-255, None = no change

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None or k in ("drive", "turn")}

    @classmethod
    def from_dict(cls, d: dict) -> "MotorCommand":
        if not d:
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Scene — reflexive → brain
# ---------------------------------------------------------------------------

@dataclass
class SceneObject:
    cls: str = ""                        # YOLO class name
    track_id: int = -1                   # persistent centroid tracker ID
    confidence: float = 0.0             # detection confidence
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)  # x1, y1, x2, y2
    centroid: tuple[int, int] = (0, 0)
    size_pct: float = 0.0               # area as fraction of frame
    distance_est_m: Optional[float] = None
    bearing_deg: float = 0.0            # -90 left, 0 center, +90 right
    frames_tracked: int = 0
    state: str = "stationary"           # stationary | moving | approaching | receding

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SceneObject":
        d = dict(d)
        d["bbox"] = tuple(d.get("bbox", (0, 0, 0, 0)))
        d["centroid"] = tuple(d.get("centroid", (0, 0)))
        return cls(**d)


@dataclass
class SceneState:
    timestamp: str = ""
    frame_delta: Optional[float] = None
    frame_delta_avg: Optional[float] = None  # rolling average over recent frames
    frame_delta_max: Optional[float] = None  # max over recent frames
    motion_detected: bool = False
    objects: list[SceneObject] = field(default_factory=list)
    person_count: int = 0
    nearest_obstacle_cm: Optional[float] = None  # closest non-person object
    floor_visible: bool = True
    light_level: Optional[str] = None     # dark | dim | normal | bright
    frame_b64: Optional[str] = None       # JPEG base64 for brain vision

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d)

    @classmethod
    def from_json(cls, s: str) -> "SceneState":
        d = json.loads(s)
        objects = [SceneObject.from_dict(o) for o in d.pop("objects", [])]
        return cls(objects=objects, **{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Hardware context — reflexive → brain
# ---------------------------------------------------------------------------

@dataclass
class HardwareContext:
    timestamp: str = ""
    # Power
    battery_v: Optional[float] = None
    battery_pct: Optional[int] = None        # 0-100 estimated from voltage
    battery_state: str = ""                   # charging | discharging | unknown
    # Locomotion
    odometer_l: int = 0
    odometer_r: int = 0
    motor_speed_l: Optional[float] = None    # current L wheel speed m/s
    motor_speed_r: Optional[float] = None    # current R wheel speed m/s
    chassis_moving: bool = False
    stuck: bool = False
    # IMU
    imu_accel_x: Optional[float] = None
    imu_accel_y: Optional[float] = None
    imu_accel_z: Optional[float] = None
    imu_gyro_x: Optional[float] = None
    imu_gyro_y: Optional[float] = None
    imu_gyro_z: Optional[float] = None
    tilt_deg: Optional[float] = None         # derived from accel
    lifted: bool = False                     # z-accel anomaly
    # Gimbal
    pan_position: int = 0
    tilt_position: int = 0
    # Vision
    fps_actual: Optional[float] = None
    light_level: Optional[str] = None        # dark | dim | normal | bright
    camera_connected: bool = True
    # Audio
    mic_connected: bool = False
    speaker_connected: bool = False
    is_speaking: bool = False
    last_human_speech_ago_s: Optional[float] = None
    # System
    cpu_temp_c: Optional[float] = None
    cpu_load: Optional[float] = None         # 1-min load average
    ram_used_pct: Optional[float] = None
    disk_free_mb: Optional[int] = None
    wifi_rssi: Optional[int] = None
    uptime_s: Optional[int] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "HardwareContext":
        d = json.loads(s)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Self-model error
# ---------------------------------------------------------------------------

@dataclass
class SelfModelError:
    frame_delta: Optional[float] = None
    drive_expected_motion: bool = False
    look_expected_change: bool = False
    motion_detected: bool = False
    anomaly: bool = False
    anomaly_reason: Optional[str] = None
    gimbal_error_pan: Optional[float] = None
    gimbal_error_tilt: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SelfModelError":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Brain I/O
# ---------------------------------------------------------------------------

@dataclass
class QualiaReport:
    attention: Optional[str] = None
    affect: Optional[str] = None
    uncertainty: Optional[str] = None
    drive: Optional[str] = None
    continuity: Optional[float] = None
    continuity_basis: Optional[str] = None
    surprise: Optional[str] = None
    opacity: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict) -> "QualiaReport":
        if not d:
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class BrainTickInput:
    tick: int = 0
    current_goal: str = ""
    last_result: str = "none"
    pan_position: int = 0
    tilt_position: int = 0
    wake_reason: Optional[str] = None
    time: str = ""
    self_model_error: Optional[dict] = None
    self_model_anomaly: Optional[str] = None
    heard: Optional[list[dict]] = None
    operator_message: Optional[str] = None
    last_spoken: Optional[str] = None
    last_commands_sent: Optional[list[dict]] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class BrainTickOutput:
    observation: str = ""
    goal: str = ""
    reasoning: str = ""
    thought: str = ""
    mood: str = ""
    actions: list[dict] = field(default_factory=list)  # deprecated, kept for DB compat
    next_tick_ms: int = 3000
    tags: list[str] = field(default_factory=list)
    outcome: str = "neutral"
    lesson: Optional[str] = None
    memory_note: Optional[str] = None
    identity_proposal: Optional[str] = None
    qualia: Optional[dict] = None
    motor: Optional[dict] = None       # MotorCommand as dict from LLM JSON
    speak: Optional[str] = None        # text to speak
    display: Optional[list] = None     # 4 OLED lines

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BrainTickOutput":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Compression / Session summary outputs
# ---------------------------------------------------------------------------

@dataclass
class CompressOutput:
    spatial: Optional[str] = None
    social: Optional[str] = None
    lessons: Optional[list[str]] = None
    sensory_calibration: Optional[str] = None
    emotional_arc: Optional[str] = None
    identity_moments: Optional[str] = None
    narrative: Optional[str] = None
    bookmarks: Optional[list[str]] = None
    opacity_events: Optional[list[str]] = None
    tags: list[str] = field(default_factory=list)
    summary: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "CompressOutput":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class SessionSummaryOutput:
    spatial_map: Optional[str] = None
    social_knowledge: Optional[str] = None
    lessons: Optional[list[str]] = None
    sensory_calibration: Optional[str] = None
    arc: Optional[str] = None
    identity: Optional[str] = None
    continuity_trajectory: Optional[str] = None
    open_threads: Optional[list[str]] = None
    tags: list[str] = field(default_factory=list)
    summary: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "SessionSummaryOutput":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Event stream
# ---------------------------------------------------------------------------

@dataclass
class Event:
    event_type: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    source: str = ""           # "reflexive", "voice", "brain"
    data: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "Event":
        return cls(**json.loads(s))


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------

@dataclass
class SpeechUtterance:
    text: str = ""
    confidence: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    time_short: str = ""       # HH:MM:SS for display

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "SpeechUtterance":
        return cls(**json.loads(s))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@dataclass
class SubsystemHealth:
    name: str = ""
    status: str = "unknown"     # ok | degraded | error | unknown
    last_check: str = ""
    message: str = ""
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SubsystemHealth":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
