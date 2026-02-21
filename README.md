# Kombucha

**An experiment in Agentic AI Persistence and Presence. AI Embodiment.**

A Claude Opus-powered cognitive loop running on a Waveshare UGV Rover.
The rover maintains a persistent inner life — goals, observations, narrative —
driven by what it sees, hears, and decides. A memory engine gives it continuity
across ticks and sessions. Readable in real time from any device on the
local network.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                   SEE → REMEMBER → THINK → ACT                  │
│                                                                 │
│  SEE:      Capture JPEG frame from USB camera (OpenCV)          │
│  REMEMBER: Assemble memory context from 5 tiers                 │
│  THINK:    POST frame + memory to Claude API, get decision      │
│  ACT:      Execute actions via ESP32 serial + gTTS              │
│  PERSIST:  Write memory DB + JSONL journal + state.json         │
│  WAIT:     Sleep for LLM-specified tick (sentry if >10s)        │
└───────────┬───────────────────────────────────────────┬─────────┘
            │                                           │
            ▼                                           ▼
┌─────────────────────────┐     ┌─────────────────────────────────┐
│     MEMORY ENGINE       │     │          HARDWARE               │
│                         │     │                                 │
│  ┌─────────────────┐    │     │  Camera (Realtek 5842 USB)      │
│  │ Identity Core   │    │     │  ESP32 serial (JSON T-codes)    │
│  │ who am I        │    │     │  4WD differential drive         │
│  ├─────────────────┤    │     │  Pan-tilt gimbal head           │
│  │ Retrieved       │    │     │  OLED 4-line display            │
│  │ tag-matched     │    │     │  LED spotlights (PWM)           │
│  ├─────────────────┤    │     │  gTTS speaker                   │
│  │ Long-Term       │    │     │                                 │
│  │ session summaries│   │     └─────────────────────────────────┘
│  ├─────────────────┤    │
│  │ Session Memory  │    │     ┌─────────────────────────────────┐
│  │ compressed today│    │     │       STORY SERVER              │
│  ├─────────────────┤    │     │                                 │
│  │ Working Memory  │    │     │  rsync JSONL + frames from Pi   │
│  │ last 5 ticks    │    │     │  SSE real-time streaming        │
│  └─────────────────┘    │     │  Dark-themed web dashboard      │
│                         │     │  http://localhost:8080           │
│  SQLite + JSONL backup  │     │                                 │
│  Haiku compression      │     └─────────────────────────────────┘
└─────────────────────────┘
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
  "actions": [{"type": "drive", "left": 0.3, "right": 0.3}],
  "next_tick_ms": 3000,
  "tags": ["loc:hallway", "obj:doorway", "mood:curious"],
  "outcome": "success | failure | partial | neutral",
  "lesson": "optional — what worked or what to try differently",
  "memory_note": "optional — what to remember from this tick",
  "identity_proposal": "optional — a new truth about yourself"
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
| Controller | ESP32 via USB serial (JSON protocol @ 115200 baud) |
| Camera | Realtek 5842 USB, 160 FOV, 1080p30 MJPEG |
| Audio In | USB camera mic + USB PnP audio device |
| Audio Out | USB PnP audio device (speaker) |
| Display | 4-line OLED on chassis (via ESP32) |
| Pan-Tilt | 2-DOF gimbal head (pan -180..180, tilt -30..90) |
| LEDs | PWM-controlled base + head spotlights |
| IMU | ESP32 onboard accelerometer/gyro |
| Battery | 3x 18650 cells, ~90 min active |
| Max Speed | 1.3 m/s, zero-radius turning |

See [Kombucha.md](Kombucha.md) for full hardware spec and ESP32 command reference.

## Setup

### On the Rover (Pi 5)

```bash
# SSH in
ssh bucket@192.168.4.226

# Kill the Waveshare app first (holds serial + camera)
pkill -f app.py

# Run the agentic bridge
cd ~/kombucha
source ~/ugv_rpi/ugv-env/bin/activate
python3 kombucha_bridge.py

# Debug mode (no hardware actions, camera + LLM only)
python3 kombucha_bridge.py --debug
```

### Development (workstation)

```bash
# Story server / dashboard (runs locally, syncs from rover)
cd E:\AI\rover
pip install -r requirements.txt
python story_server.py
```

The journal/story interface will be at `http://localhost:8080`

### Running Tests

```bash
cd E:\AI\rover
python -m pytest test_kombucha.py -v
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | (required) | Claude API key |
| `KOMBUCHA_SERIAL` | `/dev/ttyACM0` | ESP32 serial port |

## ESP32 Command Quick Reference

Commands are JSON over serial. `"T"` is the type discriminator.

| T | Command | Example |
|---|---------|---------|
| 0 | Emergency stop | `{"T":0}` |
| 1 | Drive | `{"T":1,"L":0.5,"R":0.5}` |
| 3 | OLED write | `{"T":3,"lineNum":0,"Text":"hello"}` |
| -3 | OLED reset | `{"T":-3}` |
| 132 | LEDs | `{"T":132,"IO4":255,"IO5":255}` |
| 133 | Gimbal | `{"T":133,"X":45,"Y":10,"SPD":100,"ACC":10}` |
| 141 | Gimbal (simple) | `{"T":141,"X":0,"Y":0,"SPD":50}` |

Full protocol reference in [Kombucha.md](Kombucha.md) section 5.

## Actions Available to the Mind

| Action Type | Example | Maps To |
|-------------|---------|---------|
| `drive` | `{"type":"drive","left":0.3,"right":0.3}` | `{"T":1,"L":0.3,"R":0.3}` |
| `drive` (timed) | `{"type":"drive","left":0.3,"right":0.3,"duration_ms":1500}` | Drive + auto-stop |
| `stop` | `{"type":"stop"}` | `{"T":0}` |
| `look` | `{"type":"look","pan":45,"tilt":10}` | `{"T":133,...}` |
| `display` | `{"type":"display","lines":["mood","thought","","goal"]}` | 4x `{"T":3,...}` |
| `oled` | `{"type":"oled","line":0,"text":"curious"}` | `{"T":3,...}` |
| `lights` | `{"type":"lights","base":0,"head":128}` | `{"T":132,...}` |
| `speak` | `{"type":"speak","text":"hello"}` | gTTS subprocess |

## Dual-Model Strategy

| Model | When Used |
|-------|-----------|
| **Sonnet** (default) | Routine ticks |
| **Opus** | First tick, every 20th tick, 3+ consecutive errors, motion wake |
| **Haiku** | Memory compression sidecar (async, non-blocking) |

## Journal

Every tick produces a JSONL journal entry containing:
- Observation, goal, reasoning, thought, mood
- Actions taken and their results
- Tags for memory retrieval
- Outcome assessment (success/failure/partial/neutral)
- Lesson learned (if applicable)
- Memory note (what to remember)
- Identity proposal (rare self-discovery)

Entries are stored as JSON Lines files (one per day, `data/journal/YYYY-MM-DD.jsonl`)
and served through a live web interface with SSE streaming.

## Files

```
rover/
├── Kombucha.md           # Full hardware spec + command protocol
├── MEMORY_ENGINE.md      # Memory engine design spec
├── README.md             # This file
├── CLAUDE.md             # Claude Code project instructions
├── kombucha_bridge.py    # Agentic control loop + memory engine (runs on Pi 5)
├── story_server.py       # Web dashboard (runs on workstation)
├── test_kombucha.py      # Test suite
├── requirements.txt      # Python dependencies
└── data/                 # On rover: ~/kombucha/data/
    ├── memory.db         # SQLite memory database (source of truth)
    ├── journal/          # Daily .jsonl tick logs (append-only backup)
    └── state.json        # Persistent goal/tick count/session state
```
