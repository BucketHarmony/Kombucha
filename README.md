# Kombucha

**An experiment in Agentic AI Persistence and Presence. AI Embodiment.**

A Claude Opus-powered cognitive loop running on a Waveshare UGV Rover.
The rover maintains a persistent inner life — goals, observations, narrative —
driven by what it sees, hears, and decides. A memory engine gives it continuity
across ticks and sessions. Readable in real time from any device on the
local network.

## Architecture

Kombucha v2 runs as three independent processes communicating over Redis IPC:

```
┌───────────────────────────────────────────────────────────────────────┐
│                    THREE-PROCESS ARCHITECTURE                         │
│                                                                       │
│  ┌─────────────────┐  ┌──────────────────┐  ┌─────────────────────┐  │
│  │  REFLEXIVE LAYER │  │   BRAIN PROCESS   │  │    VOICE LAYER     │  │
│  │                  │  │                   │  │                    │  │
│  │  Camera capture  │  │  SEE → REMEMBER   │  │  STT (Whisper/     │  │
│  │  YOLO detection  │  │  → THINK → ACT    │  │       Vosk)        │  │
│  │  Object tracking │  │                   │  │  TTS (gTTS/Piper)  │  │
│  │  Self-model error│  │  Claude API calls  │  │  Echo gate         │  │
│  │  Motor execution │  │  Memory engine     │  │  Safety reflexes   │  │
│  │  ESP32 serial    │  │  Prompt registry   │  │                    │  │
│  │  System telemetry│  │  Operator chat     │  │                    │  │
│  └────────┬─────────┘  └─────────┬─────────┘  └────────┬───────────┘  │
│           │                      │                      │              │
│           └──────────────┬───────┴──────────────────────┘              │
│                          │                                             │
│                    ┌─────▼──────┐                                      │
│                    │   REDIS    │                                      │
│                    │   IPC BUS  │                                      │
│                    │            │                                      │
│                    │  Scene     │  FakeRedis fallback for              │
│                    │  Hardware  │  single-process / testing            │
│                    │  Motor Cmd │                                      │
│                    │  Speech    │                                      │
│                    │  Events    │                                      │
│                    │  Health    │                                      │
│                    └────────────┘                                      │
└───────────────────────────────────────────────────────────────────────┘
```

### Tick Loop (Brain)

```
SEE → REMEMBER → THINK → ACT → REMEMBER → COMPRESS → PERSIST → WAIT
```

1. **SEE**: Capture JPEG frame via OpenCV
2. **REMEMBER**: Assemble 5-tier memory context from SQLite DB
3. **THINK**: POST frame + memory to Claude API, parse JSON response
4. **ACT**: Send motor commands to ESP32, speak, update display
5. **REMEMBER**: Insert tick as working memory + append JSONL journal
6. **COMPRESS**: Every 10 ticks, Haiku compresses old memories (async)
7. **PERSIST**: Update state.json atomically
8. **WAIT**: Sleep for LLM-specified `next_tick_ms` (sentry mode with motion detection if >10s)

## Motor Commands

The brain sends direct motor commands each tick (no directive vocabulary):

```json
{
  "motor": {
    "drive": 0.3,
    "turn": 10,
    "pan": 45,
    "tilt": -10,
    "lights_head": 128
  }
}
```

| Field | Range | Description |
|-------|-------|-------------|
| `drive` | -1.3 to 1.3 m/s | Forward/reverse speed |
| `turn` | deg/s | Rotation (positive = left) |
| `pan` | -180 to 180 | Gimbal pan (null = no change) |
| `tilt` | -30 to 90 | Gimbal tilt (null = no change) |
| `lights_base` | 0-255 | Base LED PWM (null = no change) |
| `lights_head` | 0-255 | Head LED PWM (null = no change) |

The reflexive layer converts motor commands to differential drive T-codes:
```
left  = drive - (turn_rad * wheel_base / 2)
right = drive + (turn_rad * wheel_base / 2)
```

## Memory Engine

The memory engine gives Kombucha continuity across ticks and sessions.
See [MEMORY_ENGINE.md](MEMORY_ENGINE.md) for the full design spec.

| Tier | Contents | Update | Budget |
|------|----------|--------|--------|
| **Identity Core** | Persistent self-knowledge statements | Rarely (operator-curated + agent proposals) | ~200-500 tokens |
| **Retrieved** | Tag-matched memories from past sessions | Every tick (relevance scoring) | ~500-1500 tokens |
| **Long-Term** | One paragraph per prior session | At session shutdown (Haiku) | ~500-1000 tokens |
| **Session** | Compressed narrative of today | Every 10 ticks (Haiku) | ~300-500 tokens |
| **Working** | Last 5 full tick entries | Every tick (FIFO) | ~1500-2500 tokens |

Storage: SQLite (`data/memory.db`) with WAL mode. JSONL journal as append-only backup.

## Mind Output Schema

Each tick, the mind returns:

```json
{
  "observation": "what I see — specific and vivid",
  "goal": "current goal phrase",
  "reasoning": "why I'm doing this",
  "thought": "inner monologue — contemplative, poetic",
  "mood": "one word",
  "motor": {"drive": 0.3, "turn": 0, "pan": 45},
  "speak": "optional text to say aloud",
  "display": ["line0", "line1", "line2", "line3"],
  "next_tick_ms": 3000,
  "tags": ["loc:hallway", "obj:doorway", "mood:curious"],
  "outcome": "success | failure | partial | neutral",
  "lesson": "optional — what worked or what to try differently",
  "memory_note": "optional — what to remember from this tick",
  "identity_proposal": "optional — a new truth about yourself",
  "qualia": {
    "attention": "visual",
    "affect": "curious",
    "uncertainty": 0.3,
    "continuity": 0.8,
    "continuity_basis": "I remember exploring this hallway",
    "opacity": null
  }
}
```

## Tick Speeds

The LLM controls its own tick rate via `next_tick_ms` (2000-60000).

| Range | Mode | Behavior |
|-------|------|----------|
| 2000-4000 | Engaged | Actively tracking/engaging something |
| 5000-10000 | Active | Exploring, driving, goal pursuit |
| 10000-60000 | Sentry | Motion-detection sleep, wake on movement |

## Hardware

| Component | Detail |
|-----------|--------|
| Platform | Waveshare UGV Rover PI ROS2, 4-wheel 4WD |
| Compute | Raspberry Pi 5, 4GB RAM, Debian 13 Trixie |
| Controller | ESP32 via GPIO UART `/dev/ttyAMA0` @ 115200 baud |
| Camera | Realtek 5842 USB, 160 FOV, 640x480 MJPEG |
| Audio In | USB camera mic + USB PnP audio device |
| Audio Out | USB PnP audio device (speaker) |
| Display | 4-line OLED on chassis (via ESP32) |
| Pan-Tilt | 2-DOF gimbal head (pan -180..180, tilt -30..90) |
| LEDs | PWM-controlled base + head spotlights |
| IMU | ESP32 onboard accelerometer/gyro |
| Battery | 3S LiPo (9.0V empty, 12.6V full), ~90 min active |
| Max Speed | 1.3 m/s, zero-radius turning |

See [Kombucha.md](Kombucha.md) for full hardware spec and ESP32 command reference.

## Health Monitoring

Each layer reports subsystem health to Redis:

| Subsystem | Source | Checks |
|-----------|--------|--------|
| Camera | Reflexive | OpenCV capture open |
| Serial | Reflexive | ESP32 port connected |
| Vision | Reflexive | YOLO detector + tracker available |
| Redis | Reflexive | Real Redis vs FakeRedis fallback |
| Audio | Voice | STT listener thread alive |
| API | Brain | Consecutive Claude API failures |
| Memory | Brain | SQLite DB accessible |

Status levels: `ok` / `degraded` / `error` / `unknown`

## Setup

### On the Rover (Pi 5)

```bash
# SSH in (use mDNS, IP changes on reboot)
ssh bucket@kombucha.local

# Kill the Waveshare app first (holds serial + camera)
pkill -f app.py

# Run the brain (single-process mode)
cd ~/kombucha
source ~/ugv_rpi/ugv-env/bin/activate
python3 kombucha_brain.py

# Debug mode (camera + LLM live, no hardware actions)
python3 kombucha_brain.py --debug

# Multi-process mode (3 terminals)
python3 kombucha_reflexive.py &
python3 kombucha_voice.py &
python3 kombucha_brain.py
```

### Development (workstation)

```bash
# Story server / dashboard (runs locally, syncs from rover)
pip install -r requirements.txt
python story_server.py
```

The journal/story interface will be at `http://localhost:8080`

### Running Tests

```bash
# v2 modular tests
python -m pytest tests/ -v

# Legacy monolithic tests
python -m pytest test_kombucha.py -v
```

### Configuration

Config is loaded from `config.yaml` with environment variable overrides:

```bash
# Environment variables (KOMBUCHA_ prefix, double underscore for nesting)
export ANTHROPIC_API_KEY="sk-..."
export KOMBUCHA_SERIAL__PORT="/dev/ttyAMA0"
export KOMBUCHA_DEBUG_MODE="true"
```

### Prompt Hot-Reload

System prompts live in `prompts/`. Send SIGHUP to the brain process to reload without restart:

```bash
kill -HUP $(pgrep -f kombucha_brain)
```

## ESP32 Command Quick Reference

Commands are JSON over serial (`/dev/ttyAMA0`). `"T"` is the type discriminator.

| T | Command | Example |
|---|---------|---------|
| 0 | Emergency stop | `{"T":0}` |
| 1 | Drive | `{"T":1,"L":0.5,"R":0.5}` |
| 3 | OLED write | `{"T":3,"lineNum":0,"Text":"hello"}` |
| -3 | OLED reset | `{"T":-3}` |
| 132 | LEDs | `{"T":132,"IO4":255,"IO5":255}` |
| 133 | Gimbal | `{"T":133,"X":45,"Y":10,"SPD":100,"ACC":10}` |
| 1001 | Feedback (ESP32 → Pi) | Battery, odometry, IMU, motor speeds |

Full protocol reference in [Kombucha.md](Kombucha.md) section 5.

## Dual-Model Strategy

| Model | When Used |
|-------|-----------|
| **Sonnet** (default) | Routine ticks |
| **Opus** | First tick, every 20th tick, 3+ errors, motion wake, operator chat |
| **Haiku** | Memory compression + session summaries (async) |

## Telemetry

The reflexive layer collects and publishes hardware telemetry each frame:

| Category | Fields |
|----------|--------|
| Power | battery_v, battery_pct, battery_state |
| Locomotion | odometer_l/r, motor_speed_l/r, chassis_moving, stuck |
| IMU | accel x/y/z, gyro x/y/z, tilt_deg, lifted |
| Vision | fps_actual, light_level, frame_delta, nearest_obstacle_cm |
| System | cpu_temp_c, cpu_load, ram_used_pct, disk_free_mb, wifi_rssi, uptime_s |

## Files

```
rover/
├── kombucha_brain.py         # Brain process (tick loop, Claude API, memory)
├── kombucha_reflexive.py     # Reflexive layer (camera, serial, motor execution)
├── kombucha_voice.py         # Voice layer (STT, TTS, echo gate)
├── kombucha/                 # Shared library modules
│   ├── config.py             # YAML + env config loading
│   ├── schemas.py            # Dataclasses (SceneState, HardwareContext, MotorCommand, ...)
│   ├── redis_bus.py          # Redis IPC bus + FakeRedis fallback
│   ├── memory.py             # Memory engine (SQLite, 5-tier context, compression)
│   ├── vision.py             # Camera, YOLO detection, tracking, self-model error
│   ├── serial_manager.py     # ESP32 serial + T-code validation
│   ├── audio.py              # TTS (gTTS/Piper) + STT (Whisper/Vosk)
│   ├── llm.py                # Claude API client + model selection
│   ├── actions.py            # Display/OLED/speak action translation
│   ├── health.py             # Subsystem health monitoring
│   └── prompts.py            # Prompt loading from filesystem
├── prompts/                  # System prompts (hot-reloadable via SIGHUP)
│   ├── system.md             # Main brain system prompt
│   ├── compress.md           # Memory compression prompt
│   └── session_summary.md    # Session summary prompt
├── tests/                    # v2 modular test suite
│   ├── test_schemas.py
│   ├── test_redis_bus.py
│   ├── test_health.py
│   ├── test_vision.py
│   ├── test_actions.py
│   ├── test_serial_manager.py
│   ├── test_audio.py
│   ├── test_memory.py
│   ├── test_llm.py
│   └── test_config.py
├── test_kombucha.py          # Legacy monolithic test suite
├── config.yaml               # Runtime configuration
├── story_server.py           # Web dashboard (runs on workstation)
├── Kombucha.md               # Hardware spec + ESP32 protocol
├── MEMORY_ENGINE.md          # Memory engine design spec
├── SAD.md                    # Software Architecture Document
├── CLAUDE.md                 # Claude Code project instructions
└── data/                     # On rover: ~/kombucha/data/
    ├── memory.db             # SQLite memory database
    ├── journal/              # Daily .jsonl tick logs
    └── state.json            # Persistent state
```
