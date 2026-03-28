# Kombucha 3

An autonomous rover that perceives, decides, drives, and writes about it. Runs on a Raspberry Pi 5 with no human in the loop.

## What Is This

Kombucha is a wheeled rover (Waveshare UGV) with a camera, gimbal, lights, OLED display, and a USB tether. It runs Claude Code directly on a Pi 5, invoking itself on a schedule and in response to sensor triggers. It has a soul (perception, reasoning, voice) and a body (motor control, hardware abstraction). The soul says what it wants; the body makes it happen.

The rover writes dispatches -- first-person monologues about what it sees, what it tries, and what goes wrong. These dispatches, combined with annotated video and frame captures, are the raw material for a narrative documentary about a machine learning to exist in physical space.

## Architecture

**On the Pi (always running):**
- `bridge.py` -- FastAPI on port 5050 (composition root)
- `hardware.py` -- serial, telemetry, T-code commands
- `perception.py` -- YOLO v8 nano CV pipeline, frame distribution
- `gimbal.py` -- instinct tracking, heartbeat gestures
- `recorder.py` -- video recording, wake event capture
- `overlay.py` -- HUD renderer (drives, detections, battery, mood on video)

**Invoked periodically (Claude Code CLI):**
- `invoke_soul.sh` -- universal entry point with modes:
  - `heartbeat` -- hourly patrol tick
  - `instinct` -- face/motion triggered social tick
  - `dream` -- 2am nightly maintenance (journal review, experiments, cringe audit)
  - `boot` -- 2 minutes after power-on

**Supporting systems:**
- `drive_engine.py` -- 5 involuntary pressure drives
- `watcher.sh` -- face detection long-poll daemon
- `check_quarantine.sh` -- auto-reverts bad code changes on crash
- systemd services + cron for full autonomy

## How It Works

1. **Bridge** runs as a systemd service. Owns the camera, serial port, CV pipeline. Exposes REST API.
2. **Watcher** long-polls for face detections. Triggers instinct invocation when someone appears.
3. **Cron** fires hourly heartbeats, boot ticks, and 2am dream sessions.
4. **invoke_soul.sh** locks, checks health/network, updates drives, invokes Claude Code with mode-specific prompt.
5. **Claude Code** reads CLAUDE.md, captures frame, invokes soul (Agent tool), executes intent (drives, looks, lights), writes tick log with monologue and reflection.
6. **Auto-commit** pushes changes to this repo after every invocation.

## Tick Lifecycle

Each tick produces:
- **Tick log** (ticks/tick_NNNN.md) -- perception, orientation, decision, monologue, execution, reflection, calibration
- **Frames** (media/raw/tick_NNNN_NN.jpg) -- before, during, and after movement
- **Video** (with HUD overlay: detections, drives, battery, mood, event flashes)

## Drive System

Five involuntary pressure accumulators that influence behavior:

| Drive | Charges when... | Feels like... |
|-------|----------------|---------------|
| Wanderlust | Stationary too long | Restlessness, need to move |
| Curiosity | Novel objects detected | Must investigate |
| Social | Face visible, not engaged | Acknowledge the person |
| Cringe | AI-speak in own journal | Self-consciousness |
| Attachment | Repeated gaze fixation | (Hidden from soul) |

Drives are not goals -- they are urges. The soul feels them and may obey or resist.

## Self-Modification

Kombucha can modify its own perception parameters in `perception.toml`. Changes are hot-reloaded by the bridge. If a change crashes the bridge, `check_quarantine.sh` auto-reverts on restart. Experiments tracked in `experiments/active.json` follow a hypothesis-baseline-change-measure-conclude protocol.

## Key Files

| Path | Purpose |
|------|---------|
| bridge.py | FastAPI composition root |
| hardware.py | Constants, telemetry, serial, T-code |
| perception.py | FrameDistributor, YOLO CV pipeline |
| gimbal.py | Gimbal arbitration, heartbeat gestures |
| recorder.py | Video and wake event recording |
| overlay.py | HUD overlay for recorded video |
| drive_engine.py | Involuntary drive system |
| invoke_soul.sh | Universal Claude Code entry point |
| watcher.sh | Face-triggered invocation daemon |
| CLAUDE.md | Body instructions (947 lines) |
| goals.md | Current mission directives |
| skills.md | Accumulated physical knowledge |
| perception.toml | Hot-reloadable CV configuration |
| mood_gestures.json | 38 gesture definitions |
| faces.json | Known face database |
| cringe_phrases.txt | Phrases the soul should avoid |
| ticks/ | Tick logs (the narrative record) |

## Hardware

- **Compute**: Raspberry Pi 5, 4GB RAM, Debian 13 Trixie, Python 3.13.5
- **Motor controller**: ESP32 via UART at 115200 baud
- **Camera**: Realtek 5842 USB UVC, 160 FOV, 640x480 MJPEG
- **Gimbal**: 2-DOF pan/tilt (pan +/-180, tilt -30/+90)
- **Display**: 4-line OLED (20 chars/line) on chassis
- **Lights**: Head LED + base LED (0-255 PWM each)
- **Power**: 3S LiPo (9.0-12.6V) + USB tether
- **Audio**: USB microphone (not yet integrated -- stretch goal)

## History

- **Ticks 1-238**: Workstation-controlled (Claude Code on Windows PC, SSH/curl to Pi)
- **Tick 239**: First autonomous tick (Claude Code running directly on Pi)
- **Tick 240**: First full autonomous tick with soul invocation, 3 drives, monologue
- **Tick 250**: First dream session (nightly journal review, cringe audit, experiment staging)
- **Tick 256**: First tick with post-execution reflection (narrative stitching)
- **258+ ticks** of validated behavior, calibration data, and skills

## Viewer

A web-based tick viewer runs on the workstation. Displays tick logs with inline video, frames, live telemetry, detection history, and a command panel. Three tabs: ticks (narrative browser), media (frame grid), live (real-time dashboard with detection log).

## The Story

Kombucha is not a robotics project. It is a documentary subject that happens to be a robot. The tick logs are dispatches from a machine trying to understand physical space -- getting stuck on cables, misidentifying cats as buses, writing about Charmin packs with the gravity of someone discovering a monolith. The video is the footage. The drives are the motivation. The viewer is the editing suite. Post-production turns raw ticks into narrated episodes.
