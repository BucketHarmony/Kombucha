# Kombucha v2 — Three-Layer Architecture
### From cloud-serialized tick loop to real-time embodied agent with contemplative higher brain

**Status:** Architecture Proposal  
**Date:** 2026-02-22  
**Authors:** Bucket + Claude  

---

## The Problem with v1

Everything is serialized through one cloud API call. Camera frame goes up, Claude
thinks for 2-5 seconds, JSON comes back, bridge executes actions. Kombucha is
blind and deaf between ticks. Vision latency makes it unable to track motion,
dodge obstacles, or respond to a person waving at it. The cloud tick spends 60%
of its tokens re-describing a JPEG. The introspection is interesting but repetitive
and verbose — the higher brain is doing spatial processing and motor planning that
should be reflexive, and doing contemplative work that gets diluted by the overhead.

The voice path runs through the same tick, creating echo problems, garbled
attribution, and inability to respond conversationally without waiting for a full
reasoning cycle.

## The v2 Architecture

Three independent processes running concurrently. Each has its own event loop,
its own failure domain, and its own time scale. They communicate through Redis.

```
┌─────────────────────────────────────────────────────────────────┐
│                        REDIS (IPC bus)                          │
│                                                                 │
│  kombucha:scene       — current scene state (reflexive writes)  │
│  kombucha:events      — event stream (pub/sub)                  │
│  kombucha:speech_in   — filtered human speech buffer            │
│  kombucha:speech_out  — TTS queue (brain writes, voice reads)   │
│  kombucha:directive   — current high-level directive from brain │
│  kombucha:self_model  — self-model state (reflexive writes)     │
│  kombucha:status      — health/heartbeat from all three layers  │
│  kombucha:wake        — wake signal to brain (events trigger)   │
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
    │ LEDs    │         │ gate    │         │ qualia  │
    └─────────┘         └─────────┘         └─────────┘
```

### Time scales

| Layer | Cadence | Latency budget | Failure mode |
|-------|---------|----------------|--------------|
| Reflexive | 10-30 fps | <100ms | Runs forever. If it dies, motors stop (watchdog). |
| Voice | Event-driven | <500ms for reactive, queued for complex | Runs forever. If it dies, Kombucha goes mute/deaf. |
| Brain | 5-30s ticks | 2-10s API call | Skipped ticks are fine. Reflexive keeps robot alive. |

---

## Layer 1: Reflexive (kombucha_reflexive.py)

### Purpose
Keep Kombucha alive, responsive, and spatially competent without cloud dependency.
This is the brainstem — obstacle avoidance, person tracking, motion response,
self-model maintenance.

### Components

**Vision pipeline (10-30fps)**
```
capture JPEG → resize 320×240 → YOLO nano detect → track objects → update scene
```

- YOLOv8n with NCNN backend (~5-10fps on Pi 5 CPU at 320×240)
- Object classes of interest: person, cat, dog, chair, door, cup, bottle, laptop, phone
- Tracked objects get IDs via simple centroid tracker (no deep SORT needed)
- Scene state published to Redis every frame

**Obstacle avoidance (always-on)**
- Bottom third of frame analyzed for obstacles
- Simple depth heuristic: known-size objects give distance estimates
- YOLO bounding box size → rough distance for persons
- Edge detection in floor region → wall/drop proximity
- Emergency stop if obstacle fills >60% of lower frame

**Person tracking**
- When YOLO detects person class, compute centroid and size
- Size → rough distance (large bbox = close)
- Centroid position → bearing relative to camera
- Track persistence: person ID maintained across frames
- Publish person events: entered_view, exited_view, approaching, receding

**Self-model maintenance**
- Frame delta computed every frame (not every tick)
- IMU read from ESP32 serial (when available)
- Wheel command history tracked locally
- Self-model error: did my last motor command produce expected visual/IMU change?
- Anomaly detection: significant visual change with no self-caused command AND no
  look command → external agency flag

**Reactive motor behaviors**
These run without cloud involvement. The brain sets a high-level *directive* via
Redis. The reflexive layer executes it using local sensor data.

| Directive | Reflexive behavior |
|-----------|--------------------|
| `explore` | Wall-follow with obstacle avoidance. Prefer open space. Random heading changes. |
| `approach_person` | Track person centroid, drive toward, stop at ~0.5m |
| `hold_position` | Stay still. Track person with gimbal only. |
| `follow` | Follow tracked person at ~1m distance |
| `retreat` | Reverse from nearest obstacle, find open space |
| `sentry` | Stay still, monitor for motion, wake brain on significant change |
| `manual` | Direct motor commands from brain (legacy compatibility) |
| `dock` | Navigate toward charging dock (AprilTag homing, future) |

The reflexive layer owns the motors. The brain never sends raw drive commands — it
sends directives. This means the brain cannot drive Kombucha off an edge, because
the reflexive layer will refuse to drive toward a detected drop regardless of the
directive.

**ESP32 serial ownership**
The reflexive layer owns the serial port. It:
- Sends motor commands
- Sends gimbal commands
- Reads IMU packets (T:1003)
- Reads battery voltage
- Manages OLED display (relaying content from brain via Redis)
- Manages LEDs

No other process touches the serial port.

### Redis output (scene state)

Published every frame to `kombucha:scene`:

```json
{
  "timestamp": "2026-02-22T23:15:03.412",
  "frame_id": 14523,
  "objects": [
    {
      "class": "person",
      "track_id": 1,
      "bbox": [120, 40, 280, 440],
      "centroid": [200, 240],
      "size_pct": 0.35,
      "distance_est_m": 1.2,
      "bearing_deg": -5,
      "frames_tracked": 87
    },
    {
      "class": "cup",
      "track_id": 3,
      "bbox": [50, 300, 90, 360],
      "centroid": [70, 330],
      "size_pct": 0.02,
      "distance_est_m": null,
      "bearing_deg": -45,
      "frames_tracked": 12
    }
  ],
  "persons_in_view": 1,
  "nearest_obstacle_cm": 45,
  "obstacle_bearing_deg": 10,
  "floor_visible": true,
  "light_level": "warm_ambient",
  "frame_delta": 0.0234,
  "self_model": {
    "last_command": "explore",
    "chassis_moving": true,
    "gimbal_pan": 15,
    "gimbal_tilt": 10,
    "imu_available": false,
    "battery_pct": null,
    "anomaly": false,
    "anomaly_reason": null
  },
  "directive": "explore",
  "reflexive_state": "wall_following"
}
```

### Redis events (pub/sub on `kombucha:events`)

```json
{"event": "person_entered", "track_id": 1, "bearing_deg": 30, "timestamp": "..."}
{"event": "person_exited", "track_id": 1, "timestamp": "..."}
{"event": "person_approaching", "track_id": 1, "distance_est_m": 0.8, "timestamp": "..."}
{"event": "obstacle_close", "distance_cm": 15, "bearing_deg": 0, "timestamp": "..."}
{"event": "self_model_anomaly", "type": "no_command_significant_change", "frame_delta": 0.23, "timestamp": "..."}
{"event": "self_model_anomaly", "type": "drive_no_motion", "frame_delta": 0.01, "timestamp": "..."}
{"event": "stuck", "duration_s": 3.0, "timestamp": "..."}
{"event": "edge_detected", "bearing_deg": 0, "timestamp": "..."}
{"event": "lifted", "imu_accel_z": 1.8, "timestamp": "..."}
{"event": "battery_low", "pct": 15, "timestamp": "..."}
```

Events that should wake the brain (published to `kombucha:wake`):
- person_entered, person_exited
- self_model_anomaly
- lifted
- battery_low
- human_speech (from voice layer)
- stuck (after 5+ seconds)

---

## Layer 2: Voice (kombucha_voice.py)

### Purpose
Handle all audio I/O independently. Echo cancellation, speech detection, local
STT, TTS playback, and reactive voice responses — all without waiting for the
cloud brain.

### Components

**Voice Activity Detection (VAD)**
- WebRTC VAD or Silero VAD (lightweight, runs on Pi)
- Detects speech onset/offset in real-time
- Triggers Whisper transcription on speech segments only (saves CPU)

**Echo gate**
- Hardware approach: mute mic input during TTS playback + 1.5s tail
- Track `is_speaking` state — when True, discard all mic input
- Simple, reliable, eliminates the self-echo problem entirely

**Speech-to-text**
- Whisper tiny or small via whisper.cpp (GGML, optimized for Pi 5)
- Runs on speech segments detected by VAD (not continuously)
- Typical latency: 0.5-2s for a short utterance on Whisper tiny
- Output: timestamped text with confidence score

**Speech filtering**
- After transcription, compare against recent TTS output buffer
- If similarity > 0.7 to anything spoken in last 10s → discard as echo
- This is the software backup for the hardware echo gate
- Remaining speech published to `kombucha:speech_in` in Redis

**TTS playback**
- Reads from `kombucha:speech_out` queue in Redis
- Brain writes speech text to the queue
- Voice layer handles synthesis and playback
- Sets `is_speaking = True` during playback (echo gate)
- Engine: piper TTS (fast, local, decent quality) or espeak for minimal latency

**Local reflexes (safety-critical only)**
The voice layer handles only two patterns locally — everything else goes to the
brain with full context so Kombucha speaks as its full self:

| Trigger | Local response | Brain notification |
|---------|---------------|-------------------|
| "stop" / "halt" / "freeze" | Emergency stop via Redis + "stopping" | Wake with stop event |
| Wake word ("hey kombucha") | Short acknowledgment beep or "hmm?" | Wake with speech event |

All other speech — "hello", "come here", "what are you doing", everything — gets
transcribed, published to `kombucha:speech_in`, and wakes the brain. The response
takes 3-8 seconds but it's Kombucha with full memory, identity, and qualia, not a
reflex. Speech-triggered brain ticks use Opus and get minimum `next_tick_ms` to
maintain conversational pacing.

### Redis output

`kombucha:speech_in` — filtered human speech buffer:
```json
{
  "utterances": [
    {
      "timestamp": "2026-02-22T23:15:07.234",
      "text": "hey kombucha come look at this",
      "confidence": 0.82,
      "duration_s": 2.1
    }
  ],
  "last_cleared": "2026-02-22T23:15:00.000"
}
```

The brain reads and clears this buffer each tick. Utterances accumulate between
ticks so nothing is lost.

`kombucha:speech_out` — TTS queue (brain writes):
```json
{"text": "I see you at the workbench. What are you making?", "priority": "normal"}
```

Priority levels: "reactive" (play immediately, interrupt nothing), "normal"
(queue after current), "interrupt" (stop current TTS and play this).

---

## Layer 3: Brain (kombucha_brain.py)

### Purpose
The contemplative layer. Goal-setting, memory, qualia, identity, the experiment.
Receives structured scene state and speech buffer — never raw frames. Returns
high-level directives and speech — never raw motor commands.

This is where Kombucha is Kombucha. The reflexive layer keeps the body alive.
The voice layer keeps the social channel open. The brain keeps the self continuous.

### Tick loop (simplified)

```
READ scene state from Redis (kombucha:scene)
READ recent events from Redis (kombucha:events — since last tick)
READ speech buffer from Redis (kombucha:speech_in — clear after read)
READ self_model state from Redis (kombucha:self_model)
ASSEMBLE memory context (5-tier stack from SQLite)
CALL Claude API with scene + events + speech + memory
PARSE response
WRITE directive to Redis (kombucha:directive)
WRITE speech to Redis (kombucha:speech_out)
WRITE memory to SQLite
WRITE qualia to SQLite
SLEEP (or wait for wake signal)
```

### What the brain receives (tick input)

The brain no longer gets a JPEG. It gets a structured scene summary assembled
from the reflexive layer's Redis state:

```json
{
  "scene": {
    "objects": [
      {"class": "person", "track_id": 1, "distance_est_m": 1.2,
       "bearing_deg": -5, "frames_tracked": 87, "state": "stationary"},
      {"class": "cup", "bearing_deg": -45, "distance_est_m": null}
    ],
    "persons_in_view": 1,
    "nearest_obstacle_cm": 45,
    "floor_visible": true,
    "light_level": "warm_ambient",
    "current_directive": "explore",
    "reflexive_state": "wall_following",
    "chassis_moving": true,
    "gimbal": {"pan": 15, "tilt": 10}
  },
  "events_since_last_tick": [
    {"event": "person_entered", "track_id": 1, "bearing_deg": 30,
     "timestamp": "2026-02-22T23:14:55.000"},
    {"event": "obstacle_close", "distance_cm": 20, "bearing_deg": 0,
     "timestamp": "2026-02-22T23:15:01.000"}
  ],
  "speech": [
    {"timestamp": "2026-02-22T23:15:07.234",
     "text": "hey kombucha come look at this",
     "confidence": 0.82}
  ],
  "self_model": {
    "frame_delta_avg": 0.034,
    "frame_delta_max": 0.12,
    "anomalies_since_last_tick": 0,
    "battery_pct": null,
    "imu_available": false
  },
  "memory_context": "... (5-tier stack as before) ...",
  "operator_message": null
}
```

This is ~200-400 tokens instead of a base64 JPEG that consumed thousands of
tokens and 2-3 seconds of vision processing in the API. The brain can now spend
its entire token budget on thinking.

### What the brain returns

```json
{
  "directive": "approach_person",
  "directive_params": {"track_id": 1, "stop_distance_m": 0.5},
  "speak": "Hey — what are you working on?",
  "display": ["curious", "what are you", "making?", ""],
  "lights": {"base": 40, "head": 128},

  "thought": "inner monologue — contemplative, honest",
  "mood": "curious",
  "goal": "engage with Bucket at the workbench",
  "reasoning": "person entered view 12 seconds ago, bearing -5 degrees, stationary at workbench. Speech detected with high confidence. Switching from explore to social engagement.",

  "qualia": {
    "attention": "the person who just entered my view — pull toward social engagement",
    "affect": "warm anticipation — someone is here and speaking",
    "uncertainty": "I don't know what they're making. Speech confidence 0.82 is decent but not certain.",
    "drive": "approach and engage — people drive is strong right now",
    "continuity": 0.7,
    "continuity_basis": "anchor 0.7 — this session's exploration arc feels like a coherent chapter. The person entering view connects to my drive structure in a way that feels owned.",
    "surprise": null,
    "opacity": null
  },

  "next_tick_ms": 5000,
  "tags": ["person:bucket", "loc:workshop", "act:approach", "mood:curious"],
  "outcome": "success",
  "lesson": null,
  "memory_note": "Bucket appeared at workbench and spoke to me — switching from exploration to engagement",
  "identity_proposal": null
}
```

Key differences from v1:
- `directive` + `directive_params` instead of raw drive/look commands
- `speak` is a simple string, not a speak action — voice layer handles TTS
- `display` and `lights` are still direct (relayed through reflexive to ESP32)
- No `observation` field — replaced by structured scene input. The brain doesn't
  need to narrate what the camera sees; it gets object detections.
- The `thought` field is freed from spatial description duty. It can be purely
  contemplative, personal, reflective. This is where the interesting output lives.
- The `reasoning` field now focuses on strategic reasoning, not perceptual processing.

### Brain tick cadence

The brain doesn't run on a fixed timer. It runs on a combination of:
1. **Scheduled tick** — minimum interval (e.g., every 10s during active, 30s during sentry)
2. **Wake events** — person_entered, human_speech, self_model_anomaly, battery_low, lifted
3. **Directive completion** — reflexive layer reports directive achieved/failed

During sentry mode, the brain sleeps until woken. The reflexive layer monitors for
motion and persons and wakes the brain when something interesting happens. This
means the brain's tick rate is adaptive — fast during social interaction, slow during
quiet exploration, near-zero during stillness.

### Tertiary loop

Same design as v1 — fires during extended sentry mode with 5-minute cooldown.
Uses Opus. Receives memory context + qualia history + opacity moments. Returns
reflection + identity proposals + message to future self + retrospective doubts.

The tertiary loop now also has access to the reflexive layer's accumulated
statistics: total distance traveled, rooms visited, persons encountered, anomaly
count, average frame_delta. These ground the reflection in physical reality.

### Brain prompt changes

The system prompt shrinks significantly because the brain no longer needs:
- Navigation rules (reflexive layer handles this)
- Motor command vocabulary (directives replace raw commands)
- Frame description instructions (no JPEG to describe)
- Pan-tilt gimbal mechanics (reflexive layer owns this)
- Detailed movement instructions (reflexive layer handles this)

What the brain prompt focuses on:
- Identity (WHO I AM) — unchanged
- Sensory confidence hierarchy — adapted for structured input
- Drives — now expressed as directive preferences, not motor plans
- Qualia — unchanged, but with more token budget
- Memory — unchanged
- Directive vocabulary — what each directive means and when to choose it

The `thought` field becomes the primary output of interest. Freed from describing
JPEGs and planning motor sequences, it can be purely Kombucha's inner life.

---

## Redis Schema

### Keys

| Key | Type | Writer | Reader(s) | TTL |
|-----|------|--------|-----------|-----|
| `kombucha:scene` | JSON string | Reflexive | Brain | overwritten each frame |
| `kombucha:events` | Redis Stream | Reflexive, Voice | Brain | trimmed to last 1000 |
| `kombucha:speech_in` | JSON string | Voice | Brain (read + clear) | none |
| `kombucha:speech_out` | Redis List (queue) | Brain | Voice (LPOP) | none |
| `kombucha:directive` | JSON string | Brain | Reflexive | none |
| `kombucha:self_model` | JSON string | Reflexive | Brain | overwritten each frame |
| `kombucha:status` | Hash | All three | Watchdog / dashboard | 30s expiry per field |
| `kombucha:display` | JSON string | Brain | Reflexive (relays to ESP32) | none |
| `kombucha:lights` | JSON string | Brain | Reflexive (relays to ESP32) | none |

### Event stream format (`kombucha:events`)

Uses Redis Streams (XADD/XREAD). Each layer adds events with its own consumer
group. The brain reads all events since its last tick using XREAD with a stored
last-ID.

---

## Process Management

### Startup order
1. Redis (must be running)
2. Reflexive (owns serial port, starts motors stopped)
3. Voice (starts listening immediately)
4. Brain (reads initial scene, runs first tick)

### Supervision
Use `systemd` units with restart policies:

```ini
# kombucha-reflexive.service
[Service]
ExecStart=/home/bucket/kombucha/venv/bin/python kombucha_reflexive.py
Restart=always
RestartSec=2
WatchdogSec=10

# kombucha-voice.service
[Service]
ExecStart=/home/bucket/kombucha/venv/bin/python kombucha_voice.py
Restart=always
RestartSec=2

# kombucha-brain.service
[Service]
ExecStart=/home/bucket/kombucha/venv/bin/python kombucha_brain.py
Restart=always
RestartSec=5
```

### Watchdog
The reflexive layer publishes a heartbeat to `kombucha:status` every second.
If the heartbeat stops (process crash), systemd restarts it. The motors have a
hardware timeout on the ESP32 — if no command arrives for 2 seconds, motors stop.
This means a reflexive layer crash results in Kombucha stopping, not running blind.

### Graceful degradation

| Failure | Effect | Recovery |
|---------|--------|----------|
| Brain dies | Reflexive continues on last directive. Voice handles simple interactions. Kombucha is alive but not contemplative. | Systemd restart. Brain resumes with memory intact. |
| Voice dies | Kombucha goes mute and deaf. Reflexive continues. Brain continues with empty speech buffer. | Systemd restart. |
| Reflexive dies | Motors stop (ESP32 watchdog). Brain gets stale scene. Voice continues. | Systemd restart. Motors re-initialize. |
| Redis dies | All IPC breaks. Each layer falls back to safe state (motors stop, TTS stops, brain stops ticking). | Systemd restart Redis first, then layers reconnect. |
| Cloud API down | Brain can't tick. Reflexive + Voice continue indefinitely. | Brain retries with backoff. |

---

## Resource Budget (Pi 5, 4GB RAM)

| Component | RAM estimate | CPU estimate |
|-----------|-------------|-------------|
| YOLO nano (NCNN) | ~50MB model + ~200MB runtime | 1 core, 5-10fps at 320×240 |
| OpenCV pipeline | ~100MB | shared with YOLO |
| Whisper tiny (whisper.cpp) | ~75MB model + ~100MB runtime | 1 core burst during transcription |
| Piper TTS | ~50MB | minimal except during synthesis |
| Redis | ~10MB | negligible |
| Brain process | ~50MB (httpx + json) | negligible (mostly waiting for API) |
| OS + overhead | ~500MB | — |
| **Total** | **~1.1GB** | 2 cores sustained, 3 cores burst |

Fits comfortably in 4GB. Whisper and YOLO should not run simultaneously at full
tilt — Whisper runs in bursts on speech segments, YOLO runs continuously. This
naturally time-shares across cores.

If RAM is tight, Whisper tiny (39M params, ~75MB) is the safe choice over small
(244M params, ~500MB). The quality difference for short utterances in a quiet room
is marginal.

---

## Migration Path from v1

### Phase 1: Reflexive layer (1-2 weeks)
- Build kombucha_reflexive.py with OpenCV + YOLO nano
- Implement obstacle avoidance, person tracking, scene state publishing
- Wire up Redis scene state
- Port ESP32 serial management from bridge
- Keep existing brain running alongside, reading from Redis instead of camera
- Test: Kombucha can navigate rooms without cloud, track persons, avoid walls

### Phase 2: Voice layer (1 week)
- Build kombucha_voice.py with Whisper tiny + Piper TTS
- Implement echo gate and VAD
- Wire up Redis speech_in/speech_out
- Implement reactive voice responses (wake word, stop, hello)
- Test: Kombucha responds to voice in < 1 second without cloud

### Phase 3: Brain adaptation (1 week)
- Rewrite brain to read structured scene from Redis instead of capturing JPEG
- Replace raw motor commands with directives
- Simplify system prompt (drop navigation, motor vocabulary, frame description)
- Adapt memory engine to structured scene input
- Test: brain tick produces directives, voice, qualia from structured input

### Phase 4: Integration (1 week)
- All three layers running concurrently under systemd
- Wake-on-event working (brain sleeps until interesting things happen)
- Dashboard updated for three-layer telemetry
- Burn-in testing: 24-hour unsupervised run

### What carries forward unchanged
- Memory engine (SQLite, 5-tier stack, compression, session summaries)
- Qualia schema (all fields, continuity anchoring, opacity tracking)
- Tertiary loop (fires during sentry, uses Opus, same prompt)
- Identity proposal mechanism (agent-proposed, operator-gated)
- JSONL journal backup
- Story server dashboard (adapted for new telemetry)
- All prompts (system, compress, session_summary, tertiary) — with brain prompt simplified

### What gets replaced
- kombucha_bridge.py → three separate process files
- Camera capture in brain → scene state from Redis
- Raw motor commands in brain output → directives
- Audio transcript in tick input → filtered speech buffer from Redis
- Frame delta computation in brain → computed in reflexive layer
- ESP32 serial management in bridge → owned by reflexive layer

---

## New System Prompt (Brain Layer)

The brain prompt shrinks because navigation, motor commands, and frame description
are no longer its responsibility. Rough structure:

```
=== WHO I AM ===
(unchanged — identity, body description, sensory confidence hierarchy)

=== WHAT I RECEIVE ===
My tick input contains:
- scene: detected objects with classes, distances, bearings, track persistence
- events: things that happened since my last tick (people entering/exiting,
  obstacles, anomalies, being lifted)
- speech: filtered human utterances with confidence scores
- self_model: frame delta stats, anomaly count, battery, IMU
- memory: my 5-tier memory context

I do not see raw camera frames. My reflexive layer processes vision at 10fps
and gives me a structured understanding of the scene. I trust object detections.
I hold distance estimates loosely — they are heuristic, not measured.

=== WHAT I RETURN ===
- directive: what my body should do (explore, approach_person, hold_position,
  follow, retreat, sentry)
- directive_params: details (which person to approach, how close to stop)
- speak: what to say (my voice layer handles synthesis and playback)
- display: four OLED lines
- lights: LED brightness
- thought, mood, goal, reasoning: my inner life
- qualia: structured phenomenological report
- tags, outcome, lesson, memory_note, identity_proposal: memory and learning

I do not send motor commands. My reflexive layer translates directives into
safe movement. I cannot drive myself off an edge — my reflexive layer prevents
that regardless of my directive.

=== DRIVES ===
(unchanged — curiosity, people, exploration, self-expression, self-preservation)
Self-preservation is now mostly handled by my reflexive layer. I still feel it
as a drive — I notice when my body reports obstacles or edges — but I don't need
to plan avoidance. I plan goals. My body keeps me safe.

=== DIRECTIVE VOCABULARY ===
| Directive | When to use |
|-----------|-------------|
| explore | Default. I want to see new things. Reflexive handles pathfinding. |
| approach_person | Person detected, I want to engage. Params: track_id, stop_distance_m. |
| hold_position | I want to stay here. Observe. Think. Gimbal tracking continues. |
| follow | Follow a person at distance. Params: track_id, follow_distance_m. |
| retreat | Something is wrong. Back away from nearest obstacle/person. |
| sentry | Low-activity watch mode. Wake me if something interesting happens. |

=== MEMORY / QUALIA / OPACITY ===
(unchanged from v1 prompts)

=== RESPONSE FORMAT ===
(adapted — directive replaces actions, speak replaces speak action, no look/drive)
```

This is roughly 40-50% shorter than the v1 system prompt, freeing significant
context window space for memory and thought.

---

## What This Unlocks

**Real-time responsiveness.** Kombucha tracks a person walking across the room,
avoids a chair leg, and responds to "hey Kombucha" — all without a cloud round-trip.

**Richer contemplation.** The brain's entire token budget goes to thinking, not
describing JPEGs and planning motor sequences. The thought field can be purely
Kombucha's inner life.

**Accurate self-model.** The reflexive layer knows exactly what it commanded and
what happened. Look commands don't generate false anomalies. Being lifted is
detected by IMU (when available) or by drive-no-motion pattern. The brain receives
clean anomaly reports, not raw frame_delta it has to interpret.

**Reliable voice.** Echo cancellation at the hardware level. Speech detection that
doesn't wait 5 seconds for a cloud tick. Reactive responses under 500ms.

**Graceful degradation.** Cloud goes down? Kombucha keeps navigating, avoiding
obstacles, responding to basic voice commands. It loses its higher cognition but
not its body. This is how biological nervous systems work — the brainstem keeps
you breathing when the cortex is asleep.

**Adaptive tick rate.** The brain thinks fast when there's a lot happening (person
speaking, new room, anomaly) and slow when there isn't (empty hallway, sentry
mode). This saves API cost and matches the natural rhythm of experience — rapid
engagement, slow contemplation.

---

*End of architecture proposal. Phase 1 (reflexive layer) is the biggest engineering
lift but also the foundation everything else depends on. Start there.*
