# Kombucha v2 Software Architecture Document

### Embodied AI with real-time reflexes, persistent memory, and full hardware observability

**Version:** 1.0
**Date:** 2026-02-22
**Authors:** Bucket + Claude
**Status:** Reference Architecture

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Context](#2-system-context)
3. [Architectural Principles](#3-architectural-principles)
4. [Three-Layer Process Architecture](#4-three-layer-process-architecture)
5. [Layer 1: Reflexive](#5-layer-1-reflexive)
6. [Layer 2: Voice](#6-layer-2-voice)
7. [Layer 3: Brain](#7-layer-3-brain)
8. [Hardware Data Pipeline](#8-hardware-data-pipeline)
9. [Redis IPC Schema](#9-redis-ipc-schema)
10. [Memory Engine](#10-memory-engine)
11. [LLM Client](#11-llm-client)
12. [Configuration Management](#12-configuration-management)
13. [Schema-Driven Prompt System](#13-schema-driven-prompt-system)
14. [Health & Observability](#14-health--observability)
15. [Mission Control](#15-mission-control)
16. [Process Management & Deployment](#16-process-management--deployment)
17. [Testing Strategy](#17-testing-strategy)
18. [Module Map](#18-module-map)
19. [Migration Plan](#19-migration-plan)
20. [Resource Budget](#20-resource-budget)
21. [Decision Log](#21-decision-log)

---

## 1. Executive Summary

Kombucha v2 is a greenfield rewrite of the embodied AI rover platform. It decomposes a 2,438-line monolithic bridge into three independent processes — **Reflexive**, **Voice**, and **Brain** — communicating through Redis. The architecture eliminates the v1 bottleneck where all perception, cognition, and action were serialized through a single cloud API call, making the rover blind and deaf between ticks.

**What changes:**
- Vision and motor control run at 10-30 fps on-device, no cloud dependency
- Voice I/O is event-driven with sub-500ms reactive response
- The brain receives structured scene state (~300 tokens) instead of raw JPEG (~1200 vision tokens)
- Every hardware sensor reading is surfaced to the LLM context
- All configuration lives in validated YAML, not source code
- Every subsystem reports health; silent failures are eliminated

**What carries forward:**
- The tick loop as core abstraction (adapted per layer)
- The 5-tier memory engine with tag-based retrieval
- The qualia instrumentation framework
- The dual-model strategy (Sonnet/Opus/Haiku)
- The prompt engineering quality
- SQLite + JSONL dual-write persistence

---

## 2. System Context

### 2.1 Hardware Platform

| Component | Spec |
|-----------|------|
| **Computer** | Raspberry Pi 5, Cortex-A76 quad-core 2.4GHz, 4GB LPDDR4X |
| **Chassis** | Waveshare UGV Rover, 4WD differential steer, 1.3 m/s max |
| **Motor Controller** | ESP32 via GPIO UART `/dev/ttyAMA0` @ 115200 baud, JSON protocol |
| **Camera** | Realtek 5842 USB UVC, 160 FOV, 640x480 MJPEG |
| **Microphone** | USB Camera built-in mic (card 2, device 0), 48kHz native |
| **Speaker** | USB PnP Audio Device (card 3, device 0) |
| **Display** | 4-line OLED via ESP32 serial |
| **Gimbal** | 2-DOF pan-tilt (-180..180 pan, -30..90 tilt) |
| **LEDs** | Head + base PWM (0-255) via ESP32 |
| **IMU** | On-board ESP32 accelerometer/gyroscope (T:126) |
| **Battery** | 3x 18650, 3400mAh, 9-12.6V, ~90min active |
| **I2C** | /dev/i2c-13, /dev/i2c-14 (available for expansion) |
| **SPI** | /dev/spidev10.0 (available for expansion) |
| **GPIO** | gpiochip0, gpiochip4, gpiochip10-13 |
| **Network** | WiFi (wlan0), SSH key auth, mDNS `kombucha.local` |

### 2.2 External Dependencies

| Dependency | Purpose | Failure Impact |
|------------|---------|----------------|
| Anthropic API | Brain cognition (Claude Sonnet/Opus/Haiku) | Brain stops ticking; reflexive + voice continue |
| WiFi network | API access, SSH, Mission Control | Brain stops; local layers unaffected |
| Google TTS (gTTS) | Speech synthesis (v1 path) | Kombucha goes mute; falls back to Piper local TTS |
| Redis (local) | Inter-process communication | All IPC breaks; each layer falls to safe state |

---

## 3. Architectural Principles

These principles are direct responses to v1 failures. Each is traceable to a specific incident.

### P1: Separate Concerns into Processes
*Response to: God file problem (v1 bridge = 2,438 lines, 10 responsibilities)*

Each process has one job, one failure domain, one time scale. A camera fix cannot break the memory engine because they live in different processes.

### P2: No Silent Failures
*Response to: 6+ silent failure modes in v1 (empty compression, wrong audio device, TTS to HDMI, etc.)*

Every subsystem publishes health status to Redis. Degraded states are surfaced immediately — not caught-and-logged-at-WARNING. If a component is broken, the operator knows within one heartbeat interval.

### P3: Configuration as Data
*Response to: ~40 hardcoded constants scattered across module-level declarations*

All tuning parameters live in `config.yaml` with pydantic schema validation. Environment variables override for secrets. Zero config changes require code edits.

### P4: Shared Schema Definitions
*Response to: Prompt-code schema drift (compress.md output changed, code still read old keys)*

Every data structure exchanged between components is defined once as a Python dataclass. Prompts reference the schema. Parsing code uses the schema. Changes propagate automatically.

### P5: Expose All Hardware Data
*Response to: Brain making decisions with incomplete information; sensors present but unread*

Every hardware reading the platform can produce — battery voltage, IMU, wheel odometry, CPU temp, WiFi RSSI, audio levels, frame delta, motor current, gimbal position — is captured, published to Redis, and included in the brain's tick input. The LLM should know everything the hardware knows.

### P6: Brain Sends Goals, Not Commands
*Response to: Brain spending tokens on motor planning and frame description instead of thinking*

The brain sends high-level directives ("approach person at track_id 1"). The reflexive layer translates to safe motor commands. The brain cannot drive off an edge.

### P7: Test the Pipeline, Not Just Functions
*Response to: 5,980 lines of unit tests, 0 integration tests; all integration bugs found in production*

Integration tests run a multi-tick sequence with mocked hardware and verify the full pipeline end-to-end.

---

## 4. Three-Layer Process Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        REDIS (IPC Bus)                          │
│                                                                 │
│  kombucha:scene        — structured scene state (30fps)         │
│  kombucha:events       — event stream (pub/sub)                 │
│  kombucha:hardware     — full hardware telemetry (1Hz)          │
│  kombucha:speech_in    — filtered human speech buffer            │
│  kombucha:speech_out   — TTS queue                              │
│  kombucha:directive    — current high-level directive            │
│  kombucha:self_model   — self-model state                       │
│  kombucha:status       — health/heartbeat from all layers       │
│  kombucha:display      — OLED content from brain                │
│  kombucha:lights       — LED state from brain                   │
│  kombucha:wake         — wake signals to brain                  │
│  kombucha:prompt_update— signal brain to reload prompts         │
└─────────────────────────────────────────────────────────────────┘
         │                    │                    │
    ┌────┴────┐         ┌────┴────┐         ┌────┴────┐
    │REFLEXIVE│         │  VOICE  │         │  BRAIN  │
    │ 10-30hz │         │ event   │         │ 5-30s   │
    │ on-Pi   │         │ on-Pi   │         │ cloud   │
    │         │         │         │         │         │
    │ OpenCV  │         │ Whisper │         │ Claude  │
    │ YOLO    │         │ VAD     │         │ API     │
    │ motors  │         │ TTS     │         │         │
    │ IMU     │         │ echo    │         │ memory  │
    │ serial  │         │ gate    │         │ qualia  │
    │ sensors │         │         │         │ prompts │
    └─────────┘         └─────────┘         └─────────┘
```

### Time Scales

| Layer | Cadence | Latency Budget | Runs On | Failure Mode |
|-------|---------|----------------|---------|--------------|
| Reflexive | 10-30 fps | <100ms | Pi 5 CPU | Motors stop (ESP32 watchdog). systemd restarts. |
| Voice | Event-driven | <500ms reactive | Pi 5 CPU | Kombucha goes mute/deaf. systemd restarts. |
| Brain | 5-30s ticks | 2-10s API call | Cloud (Claude) | Reflexive continues on last directive. No cognition until restart. |

### Failure Isolation

Each layer is an independent systemd service. A crash in one layer does not propagate:

| Failure | Reflexive | Voice | Brain |
|---------|-----------|-------|-------|
| Reflexive dies | **Motors stop** (ESP32 2s watchdog) | Continues | Gets stale scene |
| Voice dies | Continues | **Mute + deaf** | Empty speech buffer |
| Brain dies | Continues on last directive | Continues | **No cognition** |
| Redis dies | Safe state (motors stop) | Safe state (TTS stops) | Safe state (stops ticking) |
| Cloud API down | Unaffected | Unaffected | Retries with backoff |

---

## 5. Layer 1: Reflexive

**File:** `kombucha_reflexive.py`
**Purpose:** Keep Kombucha alive, responsive, and spatially competent without cloud dependency. This is the brainstem.

### 5.1 Responsibilities

1. **Vision pipeline** — capture, resize, YOLO detection, object tracking
2. **Motor control** — translate directives to safe T-code commands
3. **Obstacle avoidance** — emergency stop, edge detection, wall following
4. **Person tracking** — centroid tracking, distance estimation, bearing
5. **Self-model** — frame delta, command-vs-outcome verification, anomaly detection
6. **ESP32 serial ownership** — sole owner of `/dev/ttyAMA0`
7. **Hardware telemetry** — battery, IMU, odometry, CPU temp, WiFi RSSI
8. **Scene publishing** — structured scene state to Redis every frame
9. **Event publishing** — person_entered, obstacle_close, anomaly, etc.
10. **Health heartbeat** — 1Hz to `kombucha:status`

### 5.2 Vision Pipeline

```
capture JPEG (640x480)
  → resize 320x240
  → YOLO nano detect (NCNN backend, ~5-10fps)
  → centroid tracker (assign persistent IDs)
  → update scene state
  → publish to Redis
```

**Object classes of interest:** person, cat, dog, chair, door, cup, bottle, laptop, phone

**Tracking:** Simple centroid tracker with persistence. Objects get stable IDs across frames. No deep SORT needed at this resolution.

### 5.3 Directive Execution

The brain sends directives, not raw motor commands. The reflexive layer translates:

| Directive | Reflexive Behavior |
|-----------|--------------------|
| `explore` | Wall-follow with obstacle avoidance. Prefer open space. Random heading changes. |
| `approach_person` | Track person centroid, drive toward, stop at configurable distance |
| `hold_position` | Stay still. Track person with gimbal only. |
| `follow` | Follow tracked person at configurable distance |
| `retreat` | Reverse from nearest obstacle, find open space |
| `sentry` | Stay still, monitor for motion, wake brain on significant change |
| `manual` | Direct motor commands from brain (legacy compatibility) |
| `dock` | Navigate toward charging dock (AprilTag homing, future) |

**Safety invariant:** The reflexive layer will refuse to drive toward a detected drop or obstacle regardless of the directive. The brain cannot override safety.

### 5.4 Hardware Telemetry Collection

The reflexive layer collects **all available hardware data** and publishes it:

#### From ESP32 Serial (T:1001 feedback stream)

| Field | Source | Unit | Description |
|-------|--------|------|-------------|
| `battery_voltage` | `v` field in T:1001 | centivolts (divide by 100) | e.g., 1177 = 11.77V |
| `battery_pct` | computed from voltage | percent | Linear map: 9.0V=0%, 12.6V=100% |
| `motor_speed_left` | speed field | m/s | Current left motor speed |
| `motor_speed_right` | speed field | m/s | Current right motor speed |
| `odometry_left` | `odl` field | meters | Cumulative left wheel distance |
| `odometry_right` | `odr` field | meters | Cumulative right wheel distance |
| `imu_accel_x` | T:1001 IMU | m/s^2 | Accelerometer X axis |
| `imu_accel_y` | T:1001 IMU | m/s^2 | Accelerometer Y axis |
| `imu_accel_z` | T:1001 IMU | m/s^2 | Accelerometer Z axis (gravity + tilt) |
| `imu_gyro_x` | T:1001 IMU | deg/s | Gyroscope X axis |
| `imu_gyro_y` | T:1001 IMU | deg/s | Gyroscope Y axis |
| `imu_gyro_z` | T:1001 IMU | deg/s | Gyroscope Z axis (yaw rate) |
| `gimbal_pan` | T:1001 pan field | degrees | Current pan angle |
| `gimbal_tilt` | T:1001 tilt field | degrees | Current tilt angle |

#### From Pi 5 System

| Field | Source | Unit | Description |
|-------|--------|------|-------------|
| `cpu_temp` | `/sys/thermal/thermal_zone0/temp` | Celsius | Pi 5 CPU temperature |
| `cpu_load` | `/proc/loadavg` | 1-min average | System load |
| `ram_used_mb` | `psutil.virtual_memory()` | MB | RAM consumption |
| `ram_total_mb` | `psutil.virtual_memory()` | MB | Total RAM |
| `disk_free_gb` | `shutil.disk_usage()` | GB | Storage remaining |
| `wifi_rssi` | `iwconfig wlan0` | dBm | WiFi signal strength |
| `wifi_quality` | computed from RSSI | percent | Signal quality |
| `uptime_s` | `/proc/uptime` | seconds | System uptime |

#### From Vision Pipeline

| Field | Source | Unit | Description |
|-------|--------|------|-------------|
| `frame_delta` | per-frame diff | 0.0-1.0 | Visual change magnitude |
| `frame_delta_avg` | 10-frame rolling | 0.0-1.0 | Smoothed visual change |
| `frame_delta_max` | 10-frame window | 0.0-1.0 | Peak visual change |
| `fps_actual` | timer | frames/s | Actual pipeline framerate |
| `yolo_fps` | timer | detections/s | YOLO inference rate |
| `light_level` | frame luminance | category | dark/dim/ambient/bright/direct_sun |
| `camera_connected` | cv2.isOpened() | boolean | Camera health |

#### From Self-Model

| Field | Source | Unit | Description |
|-------|--------|------|-------------|
| `chassis_moving` | motor command state | boolean | Are wheels commanded? |
| `distance_traveled_m` | cumulative odometry | meters | Total session distance |
| `heading_deg` | computed from odometry differential | degrees | Estimated heading |
| `stuck` | command-vs-delta check | boolean | Commanding motion but no visual change |
| `stuck_duration_s` | timer | seconds | How long stuck state has persisted |
| `anomaly` | unexpected visual change | boolean | Significant change without self-command |
| `anomaly_type` | classification | string | "no_command_significant_change", "drive_no_motion", "lifted" |
| `lifted` | IMU accel_z deviation | boolean | Rover picked up off ground |
| `tilted_deg` | IMU accel fusion | degrees | Chassis tilt angle |

### 5.5 Scene State Schema

Published every frame to `kombucha:scene`:

```python
@dataclass
class SceneObject:
    cls: str                    # "person", "cup", "chair", etc.
    track_id: int               # persistent across frames
    bbox: tuple[int,int,int,int]  # x1, y1, x2, y2
    centroid: tuple[int,int]    # center pixel
    size_pct: float             # bbox area as fraction of frame
    distance_est_m: float | None  # heuristic from bbox size
    bearing_deg: float          # relative to camera center
    frames_tracked: int         # persistence count
    state: str                  # "stationary", "moving_left", "moving_right", "approaching", "receding"

@dataclass
class SceneState:
    timestamp: str              # ISO 8601
    frame_id: int
    objects: list[SceneObject]
    persons_in_view: int
    nearest_obstacle_cm: float | None
    obstacle_bearing_deg: float | None
    floor_visible: bool
    light_level: str            # "dark", "dim", "ambient", "bright"
    frame_delta: float
    frame_delta_avg: float
    frame_delta_max: float
    directive: str
    reflexive_state: str        # "idle", "wall_following", "approaching", "tracking", etc.
```

### 5.6 Event Stream

Published to `kombucha:events` (Redis Streams):

```python
@dataclass
class Event:
    event: str                  # event type
    timestamp: str
    # Additional fields per event type

# Event types:
# person_entered, person_exited, person_approaching, person_receding
# obstacle_close, edge_detected
# self_model_anomaly (type: no_command_significant_change | drive_no_motion | lifted)
# stuck (duration_s)
# battery_low (pct)
# battery_critical (pct)
# directive_achieved, directive_failed
# hardware_fault (component, detail)
```

---

## 6. Layer 2: Voice

**File:** `kombucha_voice.py`
**Purpose:** Handle all audio I/O independently. Echo cancellation, speech detection, STT, TTS — all without waiting for the cloud brain.

### 6.1 Capture Pipeline

```
mic input (device 0, 48kHz)
  → ring buffer (5s)
  → VAD (Silero, 16kHz downsampled)
  → segment detection (onset/offset)
  → echo gate check
  → Whisper tiny (whisper.cpp, GGML)
  → transcribed text + confidence
  → publish to kombucha:speech_in
  → wake brain
```

### 6.2 Echo Gate

Hardware approach eliminates self-echo:

1. Track `is_speaking` state (True during TTS playback + 1.5s tail)
2. When `is_speaking`: discard all mic input at VAD stage
3. Software backup: compare transcription against `kombucha:speech_out` recent history, similarity > 0.7 = discard

### 6.3 TTS Playback

```
read kombucha:speech_out queue
  → Piper TTS (local, fast)
  → set is_speaking = True
  → aplay -D plughw:3,0
  → set is_speaking = False (after 1.5s tail)
```

Priority levels: `reactive` (play immediately), `normal` (queue after current), `interrupt` (stop current, play this).

### 6.4 Local Reflexes (Safety-Critical Only)

Only two patterns are handled locally — everything else goes to the brain:

| Trigger | Local Response | Brain Wake |
|---------|---------------|------------|
| "stop" / "halt" / "freeze" | Emergency stop via Redis | `stop` event |
| Wake word ("hey kombucha") | Acknowledgment beep / "hmm?" | `wake` event |

All other speech (greetings, questions, commands) is transcribed, published, and the brain responds with full memory and identity context. Latency is 3-8s but the response is authentically Kombucha.

### 6.5 Audio Telemetry

Published to `kombucha:hardware`:

| Field | Unit | Description |
|-------|------|-------------|
| `mic_rms` | float | Current microphone RMS level |
| `mic_peak` | float | Peak amplitude |
| `mic_connected` | boolean | Device health |
| `speaker_connected` | boolean | Output device health |
| `vad_active` | boolean | Speech currently detected |
| `is_speaking` | boolean | TTS currently playing |
| `last_speech_ago_s` | seconds | Time since last human speech |
| `speech_segments_total` | int | Total speech segments this session |

---

## 7. Layer 3: Brain

**File:** `kombucha_brain.py`
**Purpose:** The contemplative layer. Goal-setting, memory, qualia, identity, the experiment. Receives structured state — never raw frames. Returns directives and speech — never raw motor commands.

### 7.1 Brain Tick Loop

```
READ kombucha:scene         (current scene state)
READ kombucha:events        (events since last tick, via XREAD)
READ kombucha:speech_in     (speech buffer, clear after read)
READ kombucha:hardware      (full hardware telemetry)
READ kombucha:self_model    (self-model state)
ASSEMBLE memory context     (5-tier stack from SQLite)
ASSEMBLE hardware context   (structured telemetry summary)
CALL Claude API             (scene + events + speech + hardware + memory)
PARSE response
WRITE kombucha:directive    (what the body should do)
WRITE kombucha:speech_out   (what to say)
WRITE kombucha:display      (OLED content)
WRITE kombucha:lights       (LED state)
WRITE memory to SQLite      (tick record + tags + qualia)
WRITE journal entry         (JSONL append)
LOG to tick_log             (full request/response for Mission Control)
SLEEP or WAIT for wake
```

### 7.2 Tick Cadence

The brain does not run on a fixed timer. It fires on:

1. **Scheduled tick** — minimum interval from `next_tick_ms` (10s active, 30s sentry)
2. **Wake events** — person_entered, human_speech, self_model_anomaly, battery_low, lifted, directive_failed
3. **Directive completion** — reflexive reports success/failure

**Model selection per tick:**

| Condition | Model | Rationale |
|-----------|-------|-----------|
| First tick of session | Opus | Deep orientation |
| Every 20th tick | Opus | Periodic deep thinking |
| Speech detected | Opus | Social engagement deserves best model |
| Self-model anomaly | Opus | Anomalies need careful reasoning |
| 3+ consecutive errors | Opus | Error recovery |
| Motion wake from sentry | Opus | Something interesting happened |
| Routine tick | Sonnet | Fast, cheap |
| Compression | Haiku | Async, non-blocking |

### 7.3 Brain Input — Full Hardware Context

The brain receives **all hardware telemetry** as structured data. This is the core of the "expose everything" principle. The hardware context section of the prompt looks like:

```python
@dataclass
class HardwareContext:
    # Power
    battery_voltage: float      # 11.77V
    battery_pct: int            # 82%
    battery_state: str          # "good", "low", "critical"

    # Motion
    motor_speed_left: float     # current m/s
    motor_speed_right: float    # current m/s
    odometry_total_m: float     # total distance this session
    heading_est_deg: float      # estimated heading

    # IMU
    imu_available: bool
    accel_x: float | None       # m/s^2
    accel_y: float | None
    accel_z: float | None
    gyro_x: float | None        # deg/s
    gyro_y: float | None
    gyro_z: float | None
    tilt_deg: float | None      # chassis tilt from IMU fusion

    # Gimbal
    gimbal_pan: float           # current pan degrees
    gimbal_tilt: float          # current tilt degrees

    # Vision
    fps_actual: float           # pipeline framerate
    light_level: str
    camera_connected: bool

    # Audio
    mic_rms: float              # ambient noise level
    mic_connected: bool
    speaker_connected: bool
    is_speaking: bool
    last_human_speech_ago_s: float | None

    # System
    cpu_temp_c: float
    cpu_load: float
    ram_used_pct: float
    disk_free_gb: float
    wifi_rssi_dbm: int
    wifi_quality_pct: int
    uptime_s: int

    # Self-Model
    chassis_moving: bool
    stuck: bool
    stuck_duration_s: float
    anomaly: bool
    anomaly_type: str | None
    lifted: bool
    tilted: bool
```

This is rendered into the brain's tick input as a structured section:

```
=== HARDWARE STATE ===
Power: 11.77V (82%) — good
Motors: L=0.3 m/s, R=0.3 m/s | Odometry: 14.2m this session | Heading: ~135deg
IMU: accel=[0.1, -0.2, 9.8] gyro=[0.0, 0.1, -0.3] | Tilt: 2deg
Gimbal: pan=15deg, tilt=10deg
Vision: 12.3fps | Light: ambient | Camera: OK
Audio: mic_rms=0.016 | mic=OK speaker=OK | not speaking | last speech: 45s ago
System: CPU 52C, load 1.2, RAM 68%, disk 42GB free, WiFi -45dBm (92%)
Uptime: 2h 15m
Self: moving=yes, stuck=no, anomaly=no, lifted=no, tilt=2deg
```

**Why expose everything:** The LLM can reason about hardware state in ways we didn't anticipate. Battery awareness enables self-preservation behavior. IMU data lets it feel being picked up. CPU temp lets it reason about its own computational limits. WiFi RSSI lets it understand connectivity risks. Audio RMS gives it a sense of ambient sound level even between speech events. Odometry gives it a sense of distance traveled and journey magnitude.

### 7.4 Brain Input — Scene + Events + Speech

```python
@dataclass
class BrainTickInput:
    scene: SceneState               # from reflexive layer
    events_since_last_tick: list[Event]  # from event stream
    speech: list[SpeechUtterance]   # from voice layer
    hardware: HardwareContext       # full telemetry
    memory_context: str             # 5-tier assembled context
    operator_message: str | None    # from Mission Control
    tick_number: int
    session_id: str
    ticks_this_session: int
    session_duration_s: float
```

### 7.5 Brain Output

```python
@dataclass
class BrainTickOutput:
    # Directives (what the body should do)
    directive: str                  # explore, approach_person, hold_position, follow, retreat, sentry
    directive_params: dict          # e.g., {"track_id": 1, "stop_distance_m": 0.5}
    speak: str | None               # what to say (voice layer handles TTS)
    display: list[str] | None       # 4 OLED lines
    lights: dict | None             # {"base": int, "head": int}

    # Inner life (the experiment)
    thought: str                    # inner monologue — freed from frame description
    mood: str                       # single word
    goal: str                       # current goal
    reasoning: str                  # strategic reasoning — freed from motor planning

    # Qualia (research instrumentation)
    qualia: QualiaReport            # attention, affect, uncertainty, drive, continuity, surprise, opacity

    # Memory + learning
    next_tick_ms: int               # when to think next
    tags: list[str]                 # prefixed tags for retrieval
    outcome: str                    # success | failure | partial | neutral
    lesson: str | None              # what worked or what to try differently
    memory_note: str | None         # what to remember from this tick
    identity_proposal: str | None   # proposed new identity statement

@dataclass
class QualiaReport:
    attention: str                  # what draws attention and why
    affect: str                     # emotional tone and character
    uncertainty: str                # what is uncertain and how that feels
    drive: str                      # which drive is strongest and why
    continuity: float               # 0.0-1.0, anchored with basis
    continuity_basis: str           # explanation of anchoring
    surprise: str | None            # genuine surprise moments
    opacity: str | None             # moments where processing is opaque to introspection
```

### 7.6 Brain Prompt Structure

The system prompt shrinks ~40-50% compared to v1 because navigation, motor commands, and frame description are no longer the brain's job:

```
=== WHO I AM ===
(Identity core from database — unchanged)

=== MY BODY ===
I live in a 4-wheel rover. My reflexive layer handles vision at 10fps,
obstacle avoidance, and motor control. I do not see raw camera frames —
I receive structured object detections with classes, distances, bearings,
and tracking persistence. I trust object detections. I hold distance
estimates loosely — they are heuristic, not measured.

=== WHAT I RECEIVE ===
Each tick I get:
- scene: detected objects with classes, distances, bearings, track persistence
- events: things that happened since my last tick
- speech: filtered human utterances with confidence scores
- hardware: full telemetry (battery, IMU, odometry, CPU, WiFi, audio levels)
- memory: my 5-tier memory context

=== WHAT I RETURN ===
- directive: what my body should do (explore, approach_person, hold_position,
  follow, retreat, sentry)
- directive_params: details (which person, how close)
- speak: what to say (my voice layer handles synthesis)
- thought, mood, goal, reasoning: my inner life
- qualia: structured phenomenological report
- tags, outcome, lesson, memory_note, identity_proposal: memory and learning

I do not send motor commands. My reflexive layer translates directives into
safe movement. I cannot drive myself off an edge.

=== HARDWARE AWARENESS ===
I have full access to my hardware state. I can feel:
- My battery level (self-preservation)
- My body's motion through IMU (being picked up, tilted, shaken)
- How far I've traveled (odometry)
- My heading and gimbal position (spatial awareness)
- Ambient sound levels (environmental awareness even between speech)
- CPU temperature and load (my own computational state)
- WiFi signal strength (connectivity to my cloud brain)
- Whether I'm stuck (commanding motion but not moving)

I should reference hardware state when it's relevant to my thinking:
- Low battery should trigger self-preservation drive
- Being lifted is significant — someone is interacting with my body
- High CPU temp means I'm working hard
- Weak WiFi means my thoughts might be interrupted

=== SENSORY CONFIDENCE HIERARCHY ===
(Adapted for structured input — trust YOLO detections, hold distances loosely,
audio transcripts remain unreliable)

=== DIRECTIVES ===
| Directive | When to use |
|-----------|-------------|
| explore | Default. I want to see new things. |
| approach_person | Person detected, I want to engage. |
| hold_position | I want to stay here. Observe. Think. |
| follow | Follow a person at distance. |
| retreat | Something is wrong. Back away. |
| sentry | Low-activity watch mode. |

=== DRIVES ===
(Unchanged — curiosity, people, exploration, self-expression, self-preservation)

=== MEMORY / QUALIA / OPACITY ===
(Unchanged from v1)

=== RESPONSE FORMAT ===
(JSON schema — directive replaces actions, speak replaces speak action)
```

---

## 8. Hardware Data Pipeline

This section details the end-to-end flow from physical sensor to LLM context.

### 8.1 Data Flow

```
Physical Sensor
  → ESP32 / Pi driver
  → Reflexive layer (read + validate)
  → Redis key (structured JSON)
  → Brain layer (read at tick time)
  → HardwareContext dataclass
  → Formatted text section in prompt
  → Claude API
```

### 8.2 ESP32 Feedback Parsing

The ESP32 sends continuous T:1001 JSON packets. The reflexive layer parses:

```python
def parse_esp32_feedback(packet: dict) -> dict:
    """Extract all available fields from ESP32 T:1001 feedback."""
    return {
        "battery_centivolts": packet.get("v"),          # 1177 = 11.77V
        "motor_speed_left": packet.get("speed_l"),
        "motor_speed_right": packet.get("speed_r"),
        "odometry_left": packet.get("odl"),             # cumulative meters
        "odometry_right": packet.get("odr"),
        "imu_accel": packet.get("imu_accel"),           # [x, y, z]
        "imu_gyro": packet.get("imu_gyro"),             # [x, y, z]
        "gimbal_pan": packet.get("pan"),
        "gimbal_tilt": packet.get("tilt"),
        "cpu_temp": packet.get("cpu_temp"),             # from ESP32's perspective
        "wifi_rssi": packet.get("rssi"),
    }
```

### 8.3 Pi System Telemetry

Collected every 1s by the reflexive layer:

```python
def collect_system_telemetry() -> dict:
    """Read Pi 5 system sensors."""
    return {
        "cpu_temp_c": read_cpu_temp(),          # /sys/thermal/thermal_zone0/temp
        "cpu_load_1m": read_loadavg(),          # /proc/loadavg
        "ram_used_mb": psutil.virtual_memory().used // (1024*1024),
        "ram_total_mb": psutil.virtual_memory().total // (1024*1024),
        "disk_free_gb": shutil.disk_usage("/").free / (1024**3),
        "wifi_rssi_dbm": read_wifi_rssi(),      # iwconfig wlan0
        "uptime_s": read_uptime(),              # /proc/uptime
    }
```

### 8.4 Derived Metrics

Computed in the reflexive layer from raw sensor data:

| Metric | Derivation | Purpose |
|--------|-----------|---------|
| `battery_pct` | Linear map: 9.0V=0%, 12.6V=100% | Human-readable battery |
| `battery_state` | <15% = critical, <30% = low, else good | Alert threshold |
| `heading_deg` | Differential odometry: `(odr - odl) / track_width` | Estimated compass heading |
| `distance_traveled_m` | `(odl + odr) / 2` cumulative | Session distance |
| `tilt_deg` | `atan2(accel_x, accel_z) * 180/pi` | Chassis tilt |
| `lifted` | `accel_z < 7.0 or accel_z > 12.0` (normal gravity ~9.8) | Picked up detection |
| `stuck` | Motor commanded but `frame_delta_avg < 0.005` for 3+ frames | Wheels spinning but not moving |
| `wifi_quality_pct` | `min(max((rssi + 100) * 2, 0), 100)` | Human-readable signal |

### 8.5 Hardware Context Budget

The full hardware context adds ~150-200 tokens to each tick. This is a worthwhile trade:

| v1 Cost | v2 Cost | Net Change |
|---------|---------|------------|
| JPEG: ~1200 vision tokens | Scene state: ~200-300 text tokens | **-900 tokens** |
| Frame description in thought: ~200 tokens | Hardware context: ~150-200 tokens | **-50 tokens** |
| Motor planning in reasoning: ~150 tokens | Directive (1 word): ~10 tokens | **-140 tokens** |
| **Total v1 overhead: ~1550 tokens** | **Total v2 overhead: ~510 tokens** | **~1040 tokens freed** |

The brain gains ~1000 tokens for thinking, memory, and qualia.

---

## 9. Redis IPC Schema

### 9.1 Keys

| Key | Type | Writer | Reader(s) | TTL |
|-----|------|--------|-----------|-----|
| `kombucha:scene` | JSON string | Reflexive | Brain | overwritten each frame |
| `kombucha:events` | Redis Stream | Reflexive, Voice | Brain | trimmed to 1000 entries |
| `kombucha:hardware` | JSON string | Reflexive | Brain, Mission Control | overwritten every 1s |
| `kombucha:speech_in` | JSON string | Voice | Brain (read + clear) | none |
| `kombucha:speech_out` | Redis List (queue) | Brain | Voice (LPOP) | none |
| `kombucha:directive` | JSON string | Brain | Reflexive | none |
| `kombucha:self_model` | JSON string | Reflexive | Brain | overwritten each frame |
| `kombucha:status` | Redis Hash | All three | Watchdog, Mission Control | 30s expiry per field |
| `kombucha:display` | JSON string | Brain | Reflexive (relays to ESP32) | none |
| `kombucha:lights` | JSON string | Brain | Reflexive (relays to ESP32) | none |
| `kombucha:wake` | Pub/Sub | Reflexive, Voice | Brain | transient |
| `kombucha:prompt_update` | Pub/Sub | Mission Control | Brain | transient |

### 9.2 Event Stream

Uses Redis Streams (XADD/XREAD). The brain stores its last-read stream ID and fetches all events since that point each tick.

### 9.3 Health Heartbeat

Each layer writes to `kombucha:status` hash with 30s TTL per field:

```
kombucha:status = {
    "reflexive_alive": "2026-02-22T23:15:03.000",
    "reflexive_fps": "12.3",
    "reflexive_serial_ok": "true",
    "reflexive_camera_ok": "true",
    "voice_alive": "2026-02-22T23:15:02.500",
    "voice_mic_ok": "true",
    "voice_speaker_ok": "true",
    "voice_vad_active": "false",
    "brain_alive": "2026-02-22T23:14:58.000",
    "brain_last_tick": "487",
    "brain_model": "claude-sonnet-4-5",
    "brain_api_ok": "true"
}
```

If any field expires (30s without update), that layer is presumed dead.

---

## 10. Memory Engine

The memory engine carries forward from v1 with minimal changes. It is the best-validated component.

### 10.1 Five-Tier Context Assembly

```
┌──────────────────────────────────────────────┐
│ 1. IDENTITY CORE                              │
│    Persistent self-knowledge (200-500 tok)    │
│                                               │
│ 2. RETRIEVED MEMORIES                         │
│    Tag-matched past entries (800-1500 tok)    │
│                                               │
│ 3. LONG-TERM MEMORY                           │
│    Prior session summaries (500-1000 tok)     │
│                                               │
│ 4. SESSION MEMORY                             │
│    Compressed today narrative (300-800 tok)   │
│                                               │
│ 5. WORKING MEMORY                             │
│    Last 3-5 full entries (1500-3000 tok)      │
└──────────────────────────────────────────────┘
```

### 10.2 Storage

- **SQLite** (`data/memory.db`, WAL mode) — source of truth
- **JSONL** (`data/journal/YYYY-MM-DD.jsonl`) — append-only crash-proof backup
- **Atomic writes** — `tempfile + os.replace` for state.json

### 10.3 Tag-Based Retrieval

Scoring algorithm (unchanged from v1):

```python
score = (
    tag_overlap(memory.tags, query_tags) * 3.0
    + memory.success * 2.0
    + memory.failure * 2.0
    + (1.0 if memory.lesson else 0.0) * 2.5
)
```

Tag prefixes: `loc:`, `obj:`, `person:`, `act:`, `goal:`, `mood:`, `event:`, `out:`, `lesson:`, `space:`, `time:`

### 10.4 Compression Pipeline

Haiku compresses working memories asynchronously every 10 ticks:

```
aged working memory entries
  → include qualia fields (continuity, opacity, surprise)
  → include self-model data (frame_delta, anomaly)
  → Haiku compression call (max_tokens: 1200)
  → structured output: spatial, social, lessons, calibration, emotional_arc,
    identity_moments, bookmarks, opacity_events, narrative
  → _format_structured_summary() concatenates non-empty sections
  → stored as session-tier summary in SQLite
```

### 10.5 v2 Adaptation

The brain no longer has an `observation` field (replaced by structured scene input). Memory entries now include:

| New Field | Source | Purpose |
|-----------|--------|---------|
| `scene_summary` | Serialized SceneState | What was in the scene at this tick |
| `hardware_summary` | Key hardware readings | Battery, IMU, odometry at this tick |
| `directive` | Brain output | What directive was issued |
| `events` | Events since last tick | What happened between ticks |

This gives the memory engine richer data for retrieval — a memory about approaching a person now includes the person's distance and bearing, not just a text description.

### 10.6 Database Schema (v2)

```sql
-- Existing tables carried forward
CREATE TABLE memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id         TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    tier            TEXT NOT NULL,           -- working/session/longterm

    -- Content
    thought         TEXT,
    scene_summary   TEXT,                    -- NEW: serialized scene state
    hardware_summary TEXT,                   -- NEW: key hardware readings
    goal            TEXT,
    mood            TEXT,
    directive       TEXT,                    -- NEW: replaces actions
    directive_params TEXT,                   -- NEW: JSON
    events          TEXT,                    -- NEW: JSON array of events this tick
    outcome         TEXT,

    -- Compressed form
    summary         TEXT,

    -- Tags
    tags            TEXT NOT NULL DEFAULT '[]',

    -- Learning signals
    success         BOOLEAN,
    failure         BOOLEAN,
    lesson          TEXT,
    memory_note     TEXT,                    -- NEW: stored directly (was in bridge)

    -- Qualia (moved from separate tracking)
    qualia_continuity   REAL,
    qualia_opacity      TEXT,
    qualia_surprise     TEXT,
    qualia_affect       TEXT,               -- NEW
    qualia_attention    TEXT,               -- NEW

    -- Self-model at tick time
    sme_frame_delta     REAL,
    sme_anomaly         BOOLEAN,            -- NEW
    sme_battery_pct     INTEGER,            -- NEW
    sme_distance_m      REAL,              -- NEW: cumulative odometry

    -- Retrieval metadata
    relevance_hits  INTEGER DEFAULT 0,
    last_retrieved  TEXT,

    -- Lifecycle
    compressed      BOOLEAN DEFAULT FALSE,
    archived        BOOLEAN DEFAULT FALSE
);

CREATE TABLE identity (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    statement       TEXT NOT NULL,
    source          TEXT NOT NULL,           -- operator/agent_proposal/tertiary_loop
    created         TEXT NOT NULL,
    active          BOOLEAN DEFAULT TRUE,
    reviewed        BOOLEAN DEFAULT FALSE,  -- NEW: operator has seen this
    rejected        BOOLEAN DEFAULT FALSE,  -- NEW
    rejected_at     TEXT,                   -- NEW
    rejected_by     TEXT,                   -- NEW
    reject_reason   TEXT,                   -- NEW
    source_tick     TEXT                    -- NEW: which tick generated this
);

-- NEW: Prompt registry (for Mission Control editing)
CREATE TABLE prompts (
    id              INTEGER PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    content         TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created         TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    notes           TEXT,
    token_count     INTEGER
);

-- NEW: Full API logging (for Mission Control inspection)
CREATE TABLE tick_log (
    id              INTEGER PRIMARY KEY,
    tick_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    model           TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    system_prompt   TEXT NOT NULL,
    user_message    TEXT NOT NULL,
    context_budget  TEXT NOT NULL,
    memory_retrieved TEXT,
    memory_scoring  TEXT,
    identity_core   TEXT,
    response_json   TEXT NOT NULL,
    response_parsed TEXT NOT NULL,
    response_tokens INTEGER,
    response_time_ms INTEGER,
    tick_type       TEXT NOT NULL,           -- primary/tertiary/compression
    wake_reason     TEXT,
    hardware_snapshot TEXT                   -- NEW: full hardware state at tick time
);
```

---

## 11. LLM Client

**File:** `kombucha/llm.py`
**Purpose:** Single LLM client class. All API calls go through one place.

v1 had 5 separate call sites with duplicated retry logic, timeout handling, header construction, and JSON parsing. v2 has one.

```python
class LLMClient:
    """Centralized Claude API client with retry, timeout, model routing, and token management."""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.http_client = httpx.AsyncClient(timeout=config.timeout_s)

    async def call(
        self,
        purpose: str,           # "brain_tick", "compression", "session_summary", "tertiary"
        system: str,            # system prompt
        user_message: str,      # assembled input
        model: str | None,      # override config default
        max_tokens: int | None, # override per-purpose default
        image: bytes | None,    # optional JPEG (for v1 compat / future use)
    ) -> tuple[dict, CallMetrics]:
        """Make an API call with centralized retry, logging, and metrics."""
        ...

    def estimate_tokens(self, text: str) -> int:
        """Rough token estimate for budget management (~4 chars/token)."""
        ...
```

### Token Budget Management

Each purpose has a configured max_tokens tied to expected output size:

| Purpose | max_tokens | Rationale |
|---------|-----------|-----------|
| `brain_tick` | 2000 | Full structured response with qualia |
| `compression` | 1200 | Structured JSON with 9 sections |
| `session_summary` | 1000 | Structured JSON with 8 sections |
| `tertiary` | 1500 | Reflection + identity proposals |

### Retry Policy

- Exponential backoff: 1s, 2s, 4s, 8s, max 30s
- Max retries: 3 for brain ticks, 2 for compression
- Credit exhaustion: log ERROR, surface to health system, stop retrying
- Rate limit (429): respect Retry-After header

---

## 12. Configuration Management

**File:** `config.yaml` + `kombucha/config.py`
**Purpose:** All tuning parameters in one validated file. Zero code edits for config changes.

### 12.1 Config Schema (Pydantic)

```python
class KombuchaConfig(BaseModel):
    """Root configuration with schema validation."""

    class SerialConfig(BaseModel):
        port: str = "/dev/ttyAMA0"
        baud_rate: int = 115200
        init_commands: list[dict] = [...]   # ESP32 boot sequence
        feedback_interval_ms: int = 100
        watchdog_timeout_s: float = 2.0

    class CameraConfig(BaseModel):
        device_index: int = 0
        resolution: tuple[int,int] = (640, 480)
        yolo_resolution: tuple[int,int] = (320, 240)
        jpeg_quality: int = 75
        fps_target: int = 15

    class AudioConfig(BaseModel):
        mic_device_index: int | None = None     # None = auto-detect
        mic_sample_rate: int = 48000
        speaker_device: str = "plughw:3,0"
        vad_threshold: float = 0.3
        echo_gate_tail_s: float = 1.5
        tts_engine: str = "piper"               # "piper" or "gtts"

    class LLMConfig(BaseModel):
        api_url: str = "https://api.anthropic.com/v1/messages"
        model_routine: str = "claude-sonnet-4-5-20250929"
        model_deep: str = "claude-opus-4-6"
        model_compression: str = "claude-haiku-4-5-20251001"
        timeout_s: int = 30
        max_retries: int = 3
        opus_every_n_ticks: int = 20
        opus_on_errors: int = 3

    class MemoryConfig(BaseModel):
        db_path: str = "data/memory.db"
        journal_dir: str = "data/journal"
        working_memory_size: int = 5
        retrieval_top_k: int = 5
        retrieval_budget_tokens: int = 1500
        compress_every_n_ticks: int = 10
        tag_overlap_weight: float = 3.0
        success_weight: float = 2.0
        failure_weight: float = 2.0
        lesson_weight: float = 2.5

    class MotionConfig(BaseModel):
        frame_delta_threshold: float = 0.03
        sentry_wake_threshold: float = 0.015
        anomaly_threshold: float = 0.08
        obstacle_stop_pct: float = 0.6      # emergency stop if obstacle fills this much of lower frame
        approach_stop_distance_m: float = 0.5
        follow_distance_m: float = 1.0

    class RedisConfig(BaseModel):
        host: str = "localhost"
        port: int = 6379
        db: int = 0

    serial: SerialConfig = SerialConfig()
    camera: CameraConfig = CameraConfig()
    audio: AudioConfig = AudioConfig()
    llm: LLMConfig = LLMConfig()
    memory: MemoryConfig = MemoryConfig()
    motion: MotionConfig = MotionConfig()
    redis: RedisConfig = RedisConfig()
    debug_mode: bool = False
    log_level: str = "INFO"
```

### 12.2 Config Loading

```python
def load_config(path: str = "config.yaml") -> KombuchaConfig:
    """Load config from YAML, override with env vars, validate with pydantic."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    # Env var overrides (secrets, deployment-specific)
    # KOMBUCHA_LLM__API_KEY → llm.api_key
    # KOMBUCHA_SERIAL__PORT → serial.port
    apply_env_overrides(raw, prefix="KOMBUCHA_")

    return KombuchaConfig(**raw)
```

### 12.3 Hot Reload

Config changes take effect on SIGHUP (no restart required for most settings):

```bash
# Edit config
vim config.yaml

# Reload
kill -HUP $(pidof kombucha_brain.py)
```

Serial port, camera device, and Redis connection require a restart.

---

## 13. Schema-Driven Prompt System

**File:** `kombucha/schemas.py`
**Purpose:** Define data structures once, use everywhere. Prompts and parsing code share the same schema.

### 13.1 The v1 Problem

The compress.md prompt was rewritten with structured output (spatial, social, lessons, etc.) but the Python code still read `result.get("summary")` — a key that no longer existed. Silently empty compression for entire sessions.

### 13.2 The v2 Solution

```python
# schemas.py — single source of truth

@dataclass
class CompressOutput:
    spatial: str = ""
    social: str = ""
    lessons: list[str] = field(default_factory=list)
    sensory_calibration: str = ""
    emotional_arc: str = ""
    identity_moments: list[str] = field(default_factory=list)
    bookmarks: list[dict] = field(default_factory=list)
    opacity_events: list[dict] = field(default_factory=list)
    narrative: str = ""
    tags: list[str] = field(default_factory=list)

@dataclass
class SessionSummaryOutput:
    spatial_map: str = ""
    social_knowledge: str = ""
    lessons: list[str] = field(default_factory=list)
    sensory_calibration: str = ""
    arc: str = ""
    identity: str = ""
    continuity_trajectory: str = ""
    open_threads: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
```

Prompts are generated with the schema's field names automatically:

```python
def build_compress_prompt(entries_text: str) -> str:
    schema_fields = get_field_names(CompressOutput)
    # Prompt includes: "Return JSON with these fields: spatial, social, lessons, ..."
    ...
```

Parsing uses the same schema:

```python
def parse_compress_response(raw: dict) -> CompressOutput:
    return CompressOutput(**{k: raw.get(k, default) for k, default in ...})
```

If a field is added to the schema, it appears in both the prompt and the parser automatically.

### 13.3 Template Engine

Prompts use `{variable}` placeholders but are loaded with `.replace()` (not `.format()`) to avoid conflicts with JSON curly braces in prompt examples:

```python
def load_prompt(name: str, **variables) -> str:
    content = get_active_prompt(db, name)   # from prompt registry
    for key, value in variables.items():
        content = content.replace(f"{{{key}}}", str(value))
    return content
```

---

## 14. Health & Observability

### 14.1 Health Check System

Every subsystem reports status. Degraded states are surfaced immediately.

```python
@dataclass
class SubsystemHealth:
    name: str                   # "serial", "camera", "mic", "speaker", "api", "memory"
    status: str                 # "ok", "degraded", "failed"
    last_ok: str                # ISO timestamp
    detail: str | None          # human-readable detail on failure
    consecutive_failures: int

class HealthMonitor:
    """Aggregates health from all subsystems. Surfaces degraded states."""

    def check_serial(self) -> SubsystemHealth:
        """Is ESP32 responding? Are motor commands being ACKed?"""

    def check_camera(self) -> SubsystemHealth:
        """Is cv2.VideoCapture open? Are frames non-empty?"""

    def check_audio(self) -> SubsystemHealth:
        """Is mic producing non-zero RMS? Is speaker device present?"""

    def check_api(self) -> SubsystemHealth:
        """Did last API call succeed? Is credit balance OK?"""

    def check_memory(self) -> SubsystemHealth:
        """Is SQLite writable? Is compression producing non-empty results?"""

    def aggregate(self) -> dict[str, SubsystemHealth]:
        """Return all subsystem statuses. Published to kombucha:status."""
```

### 14.2 Degradation Alerts

When a subsystem enters `degraded` or `failed`:

1. Log at ERROR level (not WARNING — v1 lesson)
2. Publish alert event to `kombucha:events`
3. If brain is running, include in next tick input:
   ```
   ⚠ HARDWARE ALERT: speaker failed — TTS output not working
   ```
4. Display on OLED: `! SPEAKER FAIL`
5. Mission Control shows red indicator

### 14.3 v1 Silent Failure Prevention

Each v1 silent failure now has an explicit check:

| v1 Silent Failure | v2 Check |
|-------------------|----------|
| Compression producing empty results | Assert `len(summary) > 0` after format; log ERROR + alert if empty |
| TTS to wrong audio device | Validate device exists and has output channels at startup; fail loudly |
| STT on output-only device | Validate device has input channels at startup; enumerate and auto-select |
| 48kHz fed to 16kHz VAD | Always downsample; assert sample rate matches VAD expectation |
| Serial reconnect dropping commands | Health check: expect T:1001 feedback within 5s; escalate if absent |
| API credits exhausted | Parse error response; surface immediately; stop retrying |

---

## 15. Mission Control

**Separate project.** FastAPI + React dashboard for full observability into what the LLM sees, thinks, and returns.

### 15.1 Architecture

```
┌─────────────────────────────────────────────────┐
│            MISSION CONTROL (Browser)              │
│                                                   │
│  Live Tick Stream | Prompt Editor | Memory Inspector
│  Context Visualizer | Identity Manager            │
│  Request/Response Inspector | Qualia Charts       │
│  Session Timeline | Hardware Dashboard            │
└────────────────────────┬──────────────────────────┘
                         │ HTTP + WebSocket
┌────────────────────────┴──────────────────────────┐
│         Mission Control Server (FastAPI)            │
│         Runs on workstation                         │
│                                                     │
│  WebSocket: live tick stream, scene, hardware       │
│  REST API: prompts, ticks, memory, identity, qualia │
│  Redis sub: live scene/events/speech forwarding     │
│  SQLite: memory.db + tick_log queries               │
└────────────────────────┬──────────────────────────┘
                         │
         Redis (on Pi) + SQLite (synced or direct)
                         │
┌────────────────────────┴──────────────────────────┐
│                KOMBUCHA (Pi 5)                      │
│  Reflexive → Redis scene/events/hardware            │
│  Voice → Redis speech_in/speech_out                 │
│  Brain → Redis directives + SQLite memory + tick_log│
└─────────────────────────────────────────────────────┘
```

### 15.2 Key Views

1. **Live Tick Stream** — real-time tick display with scene, directive, speech, thought, qualia. Opacity/anomaly highlighting.
2. **Hardware Dashboard** — NEW: real-time display of all hardware telemetry. Battery gauge, IMU visualization, odometry path, audio waveform, system metrics.
3. **Prompt Editor** — version-controlled prompt editing with live preview, token counter, A/B testing against last tick.
4. **Memory Inspector** — visual scoring breakdown, tier browser, tag cloud, "what if" retrieval simulation.
5. **Context Window Visualizer** — proportional block diagram showing token allocation per section.
6. **Identity Manager** — one-click approve/reject for identity proposals with full context.
7. **Request/Response Inspector** — full JSON viewer for any tick's API communication.
8. **Qualia Charts** — longitudinal continuity, opacity events, mood distribution.
9. **Session Timeline** — high-level event timeline per session.

### 15.3 Data Flow

Option C (recommended): Redis for live data, rsync SQLite for historical queries.

- Live scene/events/speech/hardware stream via Redis pub/sub → WebSocket
- Historical tick_log and memory queries against periodically synced SQLite
- Prompt edits push through Redis `kombucha:prompt_update` to brain

---

## 16. Process Management & Deployment

### 16.1 Systemd Services

```ini
# kombucha-reflexive.service
[Unit]
Description=Kombucha Reflexive Layer
After=redis.service
Requires=redis.service

[Service]
ExecStart=/home/bucket/kombucha/venv/bin/python kombucha_reflexive.py
WorkingDirectory=/home/bucket/kombucha
Environment=KOMBUCHA_CONFIG=/home/bucket/kombucha/config.yaml
Restart=always
RestartSec=2
WatchdogSec=10

[Install]
WantedBy=multi-user.target
```

```ini
# kombucha-voice.service
[Unit]
Description=Kombucha Voice Layer
After=redis.service
Requires=redis.service

[Service]
ExecStart=/home/bucket/kombucha/venv/bin/python kombucha_voice.py
WorkingDirectory=/home/bucket/kombucha
Environment=KOMBUCHA_CONFIG=/home/bucket/kombucha/config.yaml
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

```ini
# kombucha-brain.service
[Unit]
Description=Kombucha Brain Layer
After=redis.service kombucha-reflexive.service
Requires=redis.service

[Service]
ExecStart=/home/bucket/kombucha/venv/bin/python kombucha_brain.py
WorkingDirectory=/home/bucket/kombucha
Environment=KOMBUCHA_CONFIG=/home/bucket/kombucha/config.yaml
Environment=ANTHROPIC_API_KEY=<from secrets>
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 16.2 Startup Order

1. Redis (must be running)
2. Reflexive (owns serial port, starts with motors stopped)
3. Voice (starts listening immediately)
4. Brain (reads initial scene, runs first tick with Opus)

### 16.3 Graceful Shutdown

On SIGTERM:

**Brain:** Run final compression → generate session summary → write state → exit
**Voice:** Drain TTS queue → exit
**Reflexive:** Send T:0 (emergency stop) → write "shutting down" to OLED → exit

### 16.4 Deployment

```bash
# Deploy code
rsync -avz --exclude='*.pyc' --exclude='data/' \
    kombucha/ bucket@kombucha.local:~/kombucha/

# Deploy config
scp config.yaml bucket@kombucha.local:~/kombucha/

# Restart all layers
ssh bucket@kombucha.local "sudo systemctl restart kombucha-reflexive kombucha-voice kombucha-brain"

# Hot-reload config (no restart)
ssh bucket@kombucha.local "sudo systemctl kill -s HUP kombucha-brain"

# View logs
ssh bucket@kombucha.local "journalctl -u kombucha-brain -f"
```

---

## 17. Testing Strategy

### 17.1 Unit Tests

Each module has its own test file:

| Module | Test File | Key Tests |
|--------|-----------|-----------|
| `kombucha/memory.py` | `tests/test_memory.py` | Retrieval scoring, compression parsing, tag enrichment, context assembly |
| `kombucha/llm.py` | `tests/test_llm.py` | Retry logic, token estimation, model selection, response parsing |
| `kombucha/config.py` | `tests/test_config.py` | Schema validation, env var overrides, defaults |
| `kombucha/schemas.py` | `tests/test_schemas.py` | Serialization roundtrip, prompt generation, response parsing |
| `kombucha/health.py` | `tests/test_health.py` | Degradation detection, alert generation |

### 17.2 Integration Tests

Run a multi-tick sequence with mocked hardware:

```python
class TestFullPipeline:
    """Integration tests with mocked hardware and API."""

    def test_10_tick_sequence(self, mock_hardware, mock_api):
        """Run 10 ticks, verify scene→brain→directive→motor flow."""

    def test_person_detection_wakes_brain(self, mock_hardware, mock_api):
        """YOLO detects person → event published → brain wakes → approach directive."""

    def test_speech_triggers_opus(self, mock_hardware, mock_api):
        """Voice detects speech → speech_in published → brain uses Opus → speak output."""

    def test_brain_crash_recovery(self, mock_hardware, mock_api):
        """Brain dies mid-tick → reflexive continues → brain restarts → memory intact."""

    def test_compression_produces_nonempty(self, mock_hardware, mock_api):
        """After 10 ticks, compression runs → summary is non-empty → stored correctly."""

    def test_health_check_surfaces_failure(self, mock_hardware):
        """Disconnect camera → health check detects → alert published → OLED shows warning."""
```

### 17.3 Hardware Simulation

```python
class MockHardware:
    """Simulates all hardware for testing without physical rover."""

    serial: MockSerial          # T-code send/receive with T:1001 feedback
    camera: MockCamera          # Returns test frames with known objects
    audio: MockAudio            # Returns test audio segments
    redis: MockRedis            # In-memory Redis substitute
```

---

## 18. Module Map

```
kombucha/
├── kombucha_reflexive.py       # Layer 1: vision + motors + serial + telemetry
├── kombucha_voice.py           # Layer 2: STT + TTS + echo gate + VAD
├── kombucha_brain.py           # Layer 3: cognition + memory + qualia
├── kombucha/
│   ├── __init__.py
│   ├── config.py               # Pydantic config model + YAML loader
│   ├── schemas.py              # Shared dataclasses (scene, hardware, brain I/O, compress, etc.)
│   ├── memory.py               # Memory engine (SQLite, retrieval, compression, context assembly)
│   ├── llm.py                  # LLM client (single call site, retry, token management)
│   ├── serial_manager.py       # ESP32 serial communication + feedback parsing
│   ├── hardware.py             # Pi system telemetry collection
│   ├── vision.py               # YOLO pipeline + object tracker + scene builder
│   ├── audio.py                # Audio device enumeration + validation + I/O
│   ├── health.py               # Health monitor + subsystem checks
│   ├── prompts.py              # Prompt loading, template resolution, registry
│   └── redis_bus.py            # Redis connection + pub/sub + stream helpers
├── prompts/
│   ├── system.md               # Brain system prompt
│   ├── compress.md             # Haiku compression prompt
│   ├── session_summary.md      # Session end summary prompt
│   └── tertiary.md             # Tertiary reflection prompt
├── config.yaml                 # All configuration
├── data/
│   ├── memory.db               # SQLite database
│   ├── journal/                # JSONL daily journals
│   └── state.json              # Atomic state persistence
├── tests/
│   ├── test_memory.py
│   ├── test_llm.py
│   ├── test_config.py
│   ├── test_schemas.py
│   ├── test_health.py
│   ├── test_vision.py
│   ├── test_audio.py
│   ├── test_integration.py     # Full pipeline integration tests
│   └── conftest.py             # MockHardware, MockAPI, fixtures
├── mission_control/
│   ├── server.py               # FastAPI backend
│   ├── frontend/               # React app
│   └── sync.py                 # SQLite sync from Pi
└── deploy/
    ├── kombucha-reflexive.service
    ├── kombucha-voice.service
    ├── kombucha-brain.service
    └── install.sh              # Setup script
```

### Module Size Targets

| Module | Target Lines | v1 Equivalent |
|--------|-------------|---------------|
| `kombucha_reflexive.py` | ~400 | Part of bridge (camera + serial + sentry) |
| `kombucha_voice.py` | ~250 | Part of bridge (STT + TTS) |
| `kombucha_brain.py` | ~300 | Part of bridge (tick loop + API call) |
| `kombucha/memory.py` | ~400 | Part of bridge (memory engine) |
| `kombucha/llm.py` | ~150 | Part of bridge (5 scattered call sites) |
| `kombucha/config.py` | ~200 | Didn't exist (hardcoded constants) |
| `kombucha/schemas.py` | ~200 | Didn't exist (prompt-code drift) |
| `kombucha/serial_manager.py` | ~150 | Part of bridge (serial comms) |
| `kombucha/vision.py` | ~300 | Didn't exist (brain described JPEGs) |
| `kombucha/health.py` | ~200 | Didn't exist (silent failures) |
| Other modules | ~100 each | Various |
| **Total** | **~3000** | **2,438 in one file** |

More total lines, but each module is independently understandable, testable, and modifiable.

---

## 19. Migration Plan

### Phase 1: Foundation (Week 1)
- `kombucha/config.py` + `config.yaml` — all configuration externalized
- `kombucha/schemas.py` — all shared data structures
- `kombucha/redis_bus.py` — Redis connection helpers
- `kombucha/health.py` — health monitoring framework
- Redis deployed on Pi
- Tests for all foundation modules

### Phase 2: Reflexive Layer (Weeks 2-3)
- `kombucha/serial_manager.py` — ESP32 comms + feedback parsing
- `kombucha/hardware.py` — Pi system telemetry
- `kombucha/vision.py` — YOLO pipeline + tracker
- `kombucha_reflexive.py` — main reflexive process
- Scene state publishing to Redis
- Full hardware telemetry publishing
- Directive execution (explore, sentry, approach_person)
- Integration test: reflexive runs standalone, navigates room

### Phase 3: Voice Layer (Week 3)
- `kombucha/audio.py` — device enumeration + validation
- `kombucha_voice.py` — main voice process
- Whisper tiny integration (whisper.cpp)
- Echo gate implementation
- Piper TTS integration
- Speech publishing to Redis
- Integration test: voice responds to speech in <1s

### Phase 4: Brain Adaptation (Week 4)
- `kombucha/memory.py` — memory engine (extracted from bridge)
- `kombucha/llm.py` — centralized LLM client
- `kombucha/prompts.py` — prompt loading + registry
- `kombucha_brain.py` — main brain process
- Structured scene input (no more JPEG)
- Full hardware context in tick input
- Directive output (no more raw motor commands)
- Adapted prompts (shorter system prompt, directive vocabulary)
- Integration test: brain tick produces correct directives from scene

### Phase 5: Integration (Week 5)
- All three layers running under systemd
- Wake-on-event (brain sleeps until interesting things happen)
- Mission Control Phase 1 (tick logging + request/response inspector)
- 24-hour burn-in test
- v1 bridge retired

---

## 20. Resource Budget

### Pi 5, 4GB RAM

| Component | RAM | CPU |
|-----------|-----|-----|
| YOLO nano (NCNN, 320x240) | ~250MB | 1 core, 5-10fps |
| Whisper tiny (whisper.cpp) | ~175MB | 1 core burst |
| Piper TTS | ~50MB | Minimal |
| Redis | ~10MB | Negligible |
| Brain process | ~50MB | Negligible (waiting for API) |
| OS + overhead | ~500MB | — |
| **Total** | **~1.1GB** | 2 cores sustained, 3 burst |

Fits comfortably in 4GB with ~2.9GB headroom.

YOLO runs continuously. Whisper runs in bursts on speech segments only. They naturally time-share across cores.

### API Cost Estimates

| Scenario | Brain Ticks/hr | Input tok/hr | Output tok/hr | Cost/hr |
|----------|---------------|-------------|--------------|---------|
| Active exploration | 120-360 | 600K-1.8M | 60K-180K | $10-30 |
| Social engagement | 360-720 | 1.8M-3.6M | 180K-360K | $30-60 |
| Sentry (quiet) | 12-60 | 60K-300K | 6K-30K | $1-5 |
| Mixed session (typical) | ~180 | ~900K | ~90K | ~$15 |

v2 saves tokens per tick (~1040 fewer input tokens) but the adaptive tick rate may increase total ticks during social interaction. Net cost should be comparable to v1.

---

## 21. Decision Log

| # | Decision | Rationale | Alternatives Considered |
|---|----------|-----------|------------------------|
| D1 | Three independent processes | Failure isolation, independent time scales, testability | Single process with threads (v1 — failed), single process with asyncio |
| D2 | Redis as IPC bus | Simple, fast, pub/sub + streams + key-value in one, Pi-friendly | ZeroMQ (more code), shared memory (fragile), Unix sockets (no pub/sub) |
| D3 | YOLO nano for vision | Runs on Pi 5 CPU at useful fps, good person detection | OpenCV DNN (slower), Hailo accelerator (extra hardware), no local vision (v1) |
| D4 | Directives instead of motor commands | Brain freed from motor planning, safety guaranteed by reflexive layer | Raw commands (v1 — brain could drive off edges), hybrid (complexity) |
| D5 | Structured scene instead of JPEG | ~1000 fewer tokens per tick, faster API calls, brain focuses on thinking | JPEG (v1 — expensive, brain wastes tokens describing what it sees) |
| D6 | All hardware data in context | LLM can reason about battery, IMU, temperature in unexpected ways | Selective exposure (might miss useful signals), no hardware data (v1) |
| D7 | Pydantic config | Schema validation catches errors at load time, not runtime | Raw YAML (no validation), Python module (v1 — hardcoded), env vars only (no structure) |
| D8 | Piper TTS (local) | Sub-200ms latency, no network dependency, decent quality | gTTS (v1 — network latency, fails offline), espeak (poor quality) |
| D9 | Whisper tiny via whisper.cpp | 75MB RAM, fast on Pi 5, adequate for short utterances | Whisper small (500MB — tight on 4GB), Google STT (network), faster-whisper (v1) |
| D10 | Prompt registry in SQLite | Editable from Mission Control, versioned, auditable | Markdown files (v1 — requires SSH + restart), env vars (too limited) |
| D11 | Single LLM client class | One place for retry, timeout, model routing, token management | Scattered calls (v1 — 5 call sites with duplicated logic) |

---

*This document is the reference architecture for Kombucha v2. Phase 1 (foundation + config + schemas) is the entry point. Phase 2 (reflexive layer) is the largest engineering lift and the foundation everything else depends on. Start there.*

*The goal is not just a better-structured codebase — it's a rover that can see, hear, move, and think at the same time, with full awareness of its own hardware, and full observability for the humans studying it.*
