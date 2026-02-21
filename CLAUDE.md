# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Kombucha is a Claude Opus-powered agentic AI embodiment experiment running on a Waveshare UGV Rover (4-wheel 4WD, Raspberry Pi 5). The rover has a persistent inner life — goals, observations, narrative — driven by a SEE → REMEMBER → THINK → ACT tick loop with a memory engine that gives it continuity across ticks and sessions. Successor to the Bitters prototype (Petoi Bittle X + Claude Sonnet on a Pi Zero 2 W).

Design docs: README.md (overview), Kombucha.md (hardware/protocol), MEMORY_ENGINE.md (memory system).

## The Rover (Kombucha)

- **Host**: Raspberry Pi 5, 4GB RAM, Debian 13 Trixie, Python 3.13.5
- **Hostname**: kombucha, mDNS `kombucha.local` (DHCP IP changes on reboot), user `bucket`, SSH key auth
- **Motor controller**: ESP32 via GPIO UART `/dev/ttyAMA0` @ 115200 baud, JSON protocol
  - **WARNING**: `/dev/ttyACM0` is a USB-serial chip (WCH CH340) that does NOT control motors/OLED/gimbal. Only `/dev/ttyAMA0` reaches the ESP32.
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

The rover's DHCP IP changes frequently. **Always use mDNS** (`kombucha.local`), never a hardcoded IP.
If mDNS fails, do a subnet ping sweep to find it (check all 192.168.x.0/24 subnets on your machine).

```bash
ssh bucket@kombucha.local                              # shell
ssh bucket@kombucha.local 'tail -f ~/ugv.log'          # live bridge logs
# Web UI: http://kombucha.local:5000
# Jupyter: http://kombucha.local:8888
# Story dashboard: http://kombucha.local:8080 (or run story_server.py locally)
```

Kill the Waveshare app before running the bridge:
```bash
ssh bucket@kombucha.local "pkill -f app.py"
```

Restart the control app:
```bash
ssh bucket@kombucha.local "pkill -f app.py; sleep 2; cd ~/ugv_rpi && XDG_RUNTIME_DIR=/run/user/1000 nohup ugv-env/bin/python app.py >> ~/ugv.log 2>&1 &"
```

## Running Tests

```bash
python -m pytest test_kombucha.py -v
```

Tests run without hardware — no serial, camera, or API calls needed.

## Confirmed Working

- **Drive motors**: Differential drive via `{"T":1,"L":speed,"R":speed}` on `/dev/ttyAMA0`. Speeds -1.3 to 1.3 m/s. Auto-stop with `duration_ms`.
- **OLED display**: 4-line display via `{"T":3,"lineNum":0-3,"Text":"..."}`. Max 20 chars per line.
- **Gimbal pan/tilt**: `{"T":133,"X":pan,"Y":tilt,"SPD":speed,"ACC":accel}`. Pan -180..180, tilt -30..90.
- **LED lights**: Base and head LEDs via `{"T":132,"IO4":0-255,"IO5":0-255}`.
- **Speech**: gTTS + ffplay on USB PnP Audio Device (card 3). Installed via `pip3 install --break-system-packages gTTS`.
- **Camera**: 640x480 MJPEG capture via OpenCV. GStreamer/FFMPEG backend (V4L2 not compiled in pip cv2).
- **ESP32 feedback**: Continuous T:1001 JSON stream on `/dev/ttyAMA0` with speed, IMU, odometry (`odl`/`odr`), and battery voltage (`v` in centivolts, e.g., 1177 = 11.77V).
- **Memory engine**: SQLite WAL mode, JSONL journal backup, 5-tier context assembly.
- **Story server**: Local web dashboard on port 8080 with SSE live updates, rsync sync from rover.

## Known Issues

- **Serial port confusion**: `/dev/ttyACM0` is a WCH CH340 USB-serial chip — NOT the ESP32. The ESP32 motor controller is on `/dev/ttyAMA0` (GPIO UART). The Waveshare `app.py` incorrectly uses `/dev/ttyACM0` for Pi5 in its auto-detect, but `base_ctrl.py` correctly uses `/dev/ttyAMA0`. The bridge uses `/dev/ttyAMA0`.
- **ESP32 init required**: On serial open, the bridge sends 5 init commands matching the Waveshare `cmd_on_boot()` sequence: feedback interval (T:142), feedback flow (T:131), echo off (T:143), module select (T:4), version set (T:900). Without these, the ESP32 may not respond correctly.
- **USB camera**: Can drop off the USB bus after force-killing app.py. May need physical reseat. Check with `v4l2-ctl --list-devices`.
- **mediapipe**: Not available for Python 3.13/aarch64, imports are guarded with `HAS_MEDIAPIPE` flag in the Waveshare code.
- **pyttsx3**: espeak driver fails on this system — use gTTS instead (already installed).
- **app.py cron**: The `@reboot` cron entry for app.py has been **removed**. If app.py needs to run, start it manually. See DEPLOY.md for instructions.
- **OpenCV backend**: pip-installed cv2 on the Pi doesn't have V4L2 backend compiled in. Uses GStreamer/FFMPEG fallback. The V4L2 attempt in init_camera is harmless.
