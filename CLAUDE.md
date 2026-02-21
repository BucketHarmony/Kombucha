# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Kombucha is a Claude Opus-powered agentic AI embodiment experiment running on a Waveshare UGV Rover (4-wheel 4WD, Raspberry Pi 5). The rover has a persistent inner life — goals, observations, narrative — driven by a SEE → REMEMBER → THINK → ACT tick loop with a memory engine that gives it continuity across ticks and sessions. Successor to the Bitters prototype (Petoi Bittle X + Claude Sonnet on a Pi Zero 2 W).

Design docs: README.md (overview), Kombucha.md (hardware/protocol), MEMORY_ENGINE.md (memory system).

## The Rover (Kombucha)

- **Host**: Raspberry Pi 5, 4GB RAM, Debian 13 Trixie, Python 3.13.5
- **Hostname**: kombucha, IP 192.168.4.226, user `bucket`, SSH key auth
- **Motor controller**: ESP32 via USB serial `/dev/ttyACM0` @ 115200 baud, JSON protocol
- **Camera**: Realtek 5842 USB UVC, 160 FOV, 640x480 working res, MJPEG
- **Audio**: USB mic (capture) + USB PnP device (capture + playback)
- **Display**: 4-line OLED on chassis via ESP32
- **Pan-tilt**: 2-DOF gimbal (pan -180..180, tilt -30..90)
- **Waveshare stack**: ~/ugv_rpi/ with Flask web UI on port 5000, JupyterLab on 8888

## Tick Loop Architecture

```
SEE → REMEMBER → THINK → ACT → REMEMBER → COMPRESS → PERSIST → WAIT
```

1. **SEE**: Capture JPEG frame via OpenCV (`cv2.VideoCapture`)
2. **REMEMBER**: Assemble 5-tier memory context from SQLite DB
3. **THINK**: POST frame + memory context to Claude API, parse JSON response
4. **ACT**: Translate high-level actions → validated ESP32 T-codes, execute
5. **REMEMBER**: Insert tick as working memory in DB + append JSONL journal
6. **COMPRESS**: Every 10 ticks, Haiku compresses old memories (async, non-blocking)
7. **PERSIST**: Update state.json atomically (tempfile + os.replace)
8. **WAIT**: Sleep for LLM-specified `next_tick_ms` (sentry mode with motion detection if >10s)

## Memory Engine

SQLite database (`~/kombucha/data/memory.db`) with WAL mode. Two tables:

- **`memories`**: tick_id, timestamp, session_id, tier (working/session/longterm), thought, observation, goal, mood, actions, outcome, tags (JSON array), success, failure, lesson, memory_note, compressed, archived
- **`identity`**: statement, source (operator/agent_proposal), active flag

Context assembly order per tick: Identity Core → Retrieved Memories → Long-Term → Session → Working Memory → Current Tick Input

Tag prefixes: `loc:`, `obj:`, `person:`, `act:`, `goal:`, `mood:`, `event:`, `out:`, `lesson:`, `space:`, `time:`

Retrieval scoring: tag_overlap * 3.0 + success * 2.0 + failure * 2.0 + lesson * 2.5

JSONL journal (`data/journal/YYYY-MM-DD.jsonl`) is the append-only backup.

## Mind Output Schema

```json
{
  "observation": "what I see",
  "goal": "current goal",
  "reasoning": "why",
  "thought": "inner monologue",
  "mood": "one word",
  "actions": [{"type": "drive", "left": 0.3, "right": 0.3}],
  "next_tick_ms": 3000,
  "tags": ["loc:hallway", "mood:curious"],
  "outcome": "success | failure | partial | neutral",
  "lesson": "optional",
  "memory_note": "optional",
  "identity_proposal": "optional"
}
```

## ESP32 Serial Protocol

Commands are `json.dumps(cmd) + "\n"` over serial. Key commands:

- `{"T":0}` — emergency stop
- `{"T":1,"L":speed,"R":speed}` — differential drive (floats, max ~1.3 m/s)
- `{"T":133,"X":pan,"Y":tilt,"SPD":speed,"ACC":accel}` — gimbal absolute position
- `{"T":3,"lineNum":0-3,"Text":"..."}` — OLED write
- `{"T":-3}` — OLED reset
- `{"T":132,"IO4":0-255,"IO5":0-255}` — LED PWM (base, head)

The Waveshare app.py process holds the serial port exclusively. Must kill it before running the bridge.

## Action Types

| Type | Fields | Maps To |
|------|--------|---------|
| `drive` | `left`, `right`, optional `duration_ms` | T:1 + optional timed stop |
| `stop` | — | T:0 |
| `look` | `pan`, `tilt` | T:133 |
| `display` | `lines` (4 strings) | 4x T:3 |
| `oled` | `line`, `text` | T:3 |
| `lights` / `light` | `base`, `head` | T:132 |
| `speak` | `text` | gTTS subprocess |

## Dual-Model Strategy

- **Sonnet**: routine ticks (fast, cheap)
- **Opus**: first tick, every 20th, 3+ errors, motion wake (deep thinking)
- **Haiku**: memory compression sidecar (async, non-blocking)

## Accessing the Rover

```bash
ssh bucket@192.168.4.226                    # shell
ssh bucket@192.168.4.226 "tail -f ~/ugv.log" # live logs
# Web UI: http://192.168.4.226:5000
# Jupyter: http://192.168.4.226:8888
```

Kill the Waveshare app before running the bridge:
```bash
ssh bucket@192.168.4.226 "pkill -f app.py"
```

Restart the control app:
```bash
ssh bucket@192.168.4.226 "pkill -f app.py; sleep 2; cd ~/ugv_rpi && XDG_RUNTIME_DIR=/run/user/1000 nohup ugv-env/bin/python app.py >> ~/ugv.log 2>&1 &"
```

## Running Tests

```bash
python -m pytest test_kombucha.py -v
```

Tests run without hardware — no serial, camera, or API calls needed.

## Known Issues

- **USB camera**: Can drop off the USB bus after force-killing app.py. May need physical reseat. Check with `v4l2-ctl --list-devices`.
- **Pan-tilt servos**: `module_type` was changed to 2 (Gimbal) in config.yaml but physical movement unconfirmed — may be a wiring/power issue
- **ESP32 feedback**: IMU (T:126) and battery voltage (T:130) queries return no data — needs investigation
- **mediapipe**: Not available for Python 3.13/aarch64, imports are guarded with `HAS_MEDIAPIPE` flag in the Waveshare code
- **pyttsx3**: espeak driver fails on this system — use gTTS instead
- **app.py cron**: `@reboot` cron respawns app.py on boot. Must kill before each bridge run. Consider disabling the cron for dedicated bridge operation.
- **OpenCV backend**: pip-installed cv2 on the Pi doesn't have V4L2 backend compiled in. Uses GStreamer/FFMPEG fallback. The V4L2 attempt in init_camera is harmless.
