# Kombucha v1 Retrospective

A full accounting of what worked, what broke, and what we learned — written to inform the greenfield rewrite.

---

## The Vision (What We Built)

An embodied AI rover with a persistent inner life, driven by a **SEE → REMEMBER → THINK → ACT** tick loop. Claude sees through a camera, remembers through SQLite, thinks through the Anthropic API, and acts through an ESP32 motor controller. The memory engine gives it continuity across ticks and sessions. A web dashboard streams its inner monologue in real time.

**What shipped**: 2,438-line bridge, 1,906-line story server, 5,980-line test suite, 4 prompt files (~550 lines), 5 design docs (~2,500 lines). Running on a Raspberry Pi 5 with USB camera, USB mic, USB speaker, and Waveshare UGV chassis.

---

## Where the Architecture Thrived

### 1. The Tick Loop Itself

The `SEE → REMEMBER → THINK → ACT → REMEMBER → COMPRESS → PERSIST → WAIT` pipeline is fundamentally sound. Each stage has a clear contract. The loop is resilient — a failed LLM call doesn't crash the rover, a serial disconnect doesn't halt observation. The tick loop should survive into v2 as the core abstraction.

### 2. The Memory Engine

The 5-tier memory system (identity → retrieved → long-term → session → working) is the best thing we built. The tag-based retrieval scoring (`tag_overlap * 3.0 + success * 2.0 + failure * 2.0 + lesson * 2.5`) produces genuinely useful recall. The SQLite + JSONL dual-write gives us structured queries AND crash-proof append-only backup. The memory engine design doc (MEMORY_ENGINE.md) is reference-quality.

**What worked especially well:**
- Tag prefix system (`loc:`, `obj:`, `person:`, `act:`) makes retrieval contextual
- Compression sidecar is async and non-blocking — tick loop never waits
- Identity table with operator/agent_proposal sources creates genuine continuity
- Context assembly order (identity first, working memory last) gives the LLM the right framing

### 3. Sensory Calibration in Prompts

Teaching the LLM its own sensor weaknesses directly in the system prompt prevented entire classes of confabulation. Lines like "audio transcript frequently garbles words — do not treat garbled text as human's actual word choices" and "social inferences about human attention are unreliable from floor-level camera angle" saved us from hallucinated social interactions.

### 4. Dual/Triple Model Strategy

Sonnet for routine ticks, Opus for first tick / every 20th / errors / motion wake, Haiku for compression. This kept costs manageable while preserving quality where it mattered. The Opus ticks consistently produced deeper reasoning and better identity moments.

### 5. The Prompt Engineering

The prompts are genuinely good. The compress prompt's preservation rules ("pass through ALL memory_note fields VERBATIM", "pass through ALL non-null qualia_opacity fields VERBATIM") protect research data from lossy compression. The session summary prompt's "merge, don't stack" principle produces clean long-term memories. The qualia instrumentation framework produced real research data.

### 6. Atomic State Persistence

Using `tempfile + os.replace` for state.json writes prevented corruption on power loss. The JSONL journal as append-only backup means we never lost tick data even when SQLite had issues.

### 7. Documentation

CLAUDE.md, README.md, MEMORY_ENGINE.md, Kombucha.md, DEPLOY.md — the docs were consistently excellent and saved hours of debugging. CLAUDE.md in particular made it possible for Claude Code to work on the project effectively.

---

## Where the Architecture Failed Us

### 1. The God File Problem

`kombucha_bridge.py` is a 2,438-line monolith containing:
- Memory engine (database, tagging, retrieval, compression)
- Hardware interface (camera, serial, LEDs, gimbal)
- LLM client (API calls, JSON repair, prompt loading)
- Action translator (LLM output → T-codes)
- Speech I/O (TTS output, STT input)
- Operator chat server (HTTP endpoint)
- Main tick loop
- State management
- Sentry mode
- Tertiary reflection loop

Every change risks breaking something unrelated. The file is too large to hold in context. A camera fix can break the memory engine. **This is the #1 problem to solve in v2.**

### 2. Hardcoded Configuration Everywhere

~40 constants scattered across module-level declarations:
- Serial port (`/dev/ttyAMA0`)
- Camera resolution (640x480)
- Model names (claude-sonnet-4-5, claude-opus-4-6)
- API URL
- STT device index, sample rate
- Memory sizes, thresholds, intervals
- Motion detection thresholds (and they're inconsistent — `MOTION_THRESHOLD = 0.03` vs `delta > 0.015` in sentry vs `delta > 0.08` for anomaly)

Changing any tuning parameter requires editing source code and redeploying. **We need a config file with schema validation.**

### 3. Silent Failure Modes

This caused the most real-world pain:

| Component | Failure Mode | What Actually Happened |
|-----------|-------------|----------------------|
| Compression | `result.get("summary")` on a schema that no longer has "summary" | Silently produced empty results for every compression. All memory consolidation was lost. |
| TTS | ffplay defaulting to HDMI instead of USB speaker | Kombucha "spoke" but sound went to a monitor with no speakers. Completely silent for entire sessions. |
| STT | Wrong device index (output-only device) | Captured pure digital silence (RMS 0.001). VAD correctly rejected it. "No speech detected" for hours. |
| STT | 48kHz audio fed to 16kHz VAD | Even after fixing the device, Silero VAD saw speech frequencies as subsonic rumble. Filtered out 100% of audio. |
| Serial | Reconnect failures don't escalate | Bridge continues running with `ser_port = None`, all motor/display/light commands silently dropped. |
| Storyboard sync | scp on Windows doesn't have `--ignore-existing` | Sync silently failed or stalled, dashboard showed stale data. |

**Pattern**: Every silent failure had the same shape — a component failed, the error was caught and logged at WARNING level, and the system continued running in a degraded state that looked normal from the outside. **We need health checks that surface degraded states.**

### 4. The Embedded HTML Monolith

1,337 lines of HTML/CSS/JS embedded as a Python string literal in story_server.py (70% of the file). No syntax highlighting, no linting, no hot reload, no dev tools. Every dashboard change requires editing a Python string and restarting the server. **Extract to separate files.**

### 5. Audio Device Fragility

The audio stack was the most brittle subsystem:
- USB device indices change on reboot
- Device capabilities (input vs output) aren't validated at startup
- Sample rate compatibility isn't checked
- No audio device enumeration/auto-detection
- TTS pipeline: gTTS (network call) → ffmpeg (format conversion) → aplay (playback) — three failure points, all silent
- The USB PnP device sometimes appears as capture-capable, sometimes not

**Lesson**: Audio devices need enumeration at startup with explicit validation. Fail loudly if the expected device isn't available.

### 6. Prompt ↔ Code Schema Drift

The compress.md and session_summary.md prompts were rewritten with rich structured output schemas (spatial, lessons, social, opacity_events, etc.), but the bridge code still read `result.get("summary")` — a key that no longer existed. **The prompts and the code had no shared schema definition.** Changes to one didn't automatically propagate to the other.

Additionally, `.format(entries=...)` broke when the prompts started containing JSON examples with `{...}` curly braces. Python's string formatting tried to interpret them as template variables.

**Lesson**: Prompt output schemas should be defined once (as a Python dataclass or JSON schema) and referenced by both the prompt and the parsing code.

### 7. No LLM Abstraction Layer

API calls to Anthropic are scattered across 5+ locations:
- `call_brain()` — main tick
- `compress_old_memories()` — Haiku compression
- `generate_session_summary()` — session end
- `run_tertiary_loop()` — identity reflection
- Chat handler (via bridge API)

Each has its own retry logic, timeout, header construction, and JSON parsing. Changing the API version or adding a new provider would require editing all 5. **We need a single LLM client class.**

### 8. Global Mutable State

Module-level globals: `running`, `ser_port`, `_operator_queue`, `_operator_wake_event`, plus all the path constants. This makes:
- Testing require careful global state cleanup
- Multi-instance impossible
- Shutdown order fragile
- Import side effects unpredictable (argparse runs at import time!)

### 9. No Integration Tests

5,980 lines of unit tests, but no test that runs the actual tick loop end-to-end with mocked hardware. The unit tests verify individual functions but not the pipeline. We discovered integration-level bugs (prompt schema drift, audio device mismatch, TTS output routing) only in production.

### 10. max_tokens Budget Mismanagement

Both compression and session summary calls used `max_tokens: 300` — far too low for the structured JSON output the prompts requested. This caused JSON truncation errors that the `_repair_truncated_json()` heuristic couldn't always fix. We bumped to 1200/1000 but the underlying problem is that **token budget isn't tied to expected output size**.

---

## Specific Bugs We Hit (In Session)

1. **Compression silently empty** — Prompts rewrote output schema, code still read old key. Fixed by adding `_format_structured_summary()` helper with fallback.

2. **`.format()` KeyError on JSON braces** — `compress.md` contains `{"spatial": ""}` which `.format(entries=...)` interprets as a template variable. Fixed by switching to `.replace("{entries}", ...)`.

3. **max_tokens truncation** — 300 tokens for structured JSON with 9 sections. Haiku output got cut mid-string: `Unterminated string starting at: line 6 column 20`. Fixed by bumping to 1200.

4. **TTS to wrong device** — `ffplay` defaults to ALSA default (HDMI on Pi 5). No `mpg123` installed, so fallback chain was `mpg123 (missing) || ffplay (wrong device)`. Fixed by using `ffmpeg → aplay -D plughw:3,0`.

5. **STT on output-only device** — `STT_DEVICE_INDEX=1` pointed at USB PnP which has `maxInputChannels=0`. Captured pure silence. Fixed by switching to device 0 (USB Camera mic).

6. **48kHz → 16kHz resample missing** — Silero VAD expects 16kHz. We fed 48kHz. Speech frequencies appeared as ~5kHz rumble, below VAD's speech band. Fixed by adding 3:1 decimation.

7. **Storyboard stuck** — Journal files not re-synced after bridge restart. scp fallback on Windows doesn't incrementally update. Fixed by manual sync + server restart.

8. **API credits exhausted** — No monitoring or alerting on credit balance. Bridge hit `credit balance is too low` and entered unbounded exponential backoff.

---

## What v2 Should Look Like

### Architectural Principles

1. **Separate concerns into modules**: memory, hardware, llm, actions, audio, config, prompts, dashboard
2. **Config file with schema validation**: YAML + pydantic, env var overrides for secrets
3. **Single LLM client**: retry, timeout, model routing, token budget management in one place
4. **Health check system**: each subsystem reports status, bridge surfaces degraded states loudly
5. **Shared schema definitions**: prompt output schemas defined as dataclasses, used by both prompt generation and response parsing
6. **Dashboard as separate project**: HTML/CSS/JS in their own files, served statically
7. **Integration tests**: mock hardware, run 10-tick sequence, verify full pipeline
8. **Audio device enumeration**: auto-detect at startup, validate capabilities, fail loudly

### What to Keep

- The tick loop architecture (SEE → REMEMBER → THINK → ACT)
- The 5-tier memory engine (maybe extract to its own package)
- The tag-based retrieval scoring
- The SQLite + JSONL dual-write pattern
- The sensory calibration approach in prompts
- The dual-model strategy (Sonnet/Opus/Haiku)
- The atomic state persistence pattern
- The ESP32 JSON serial protocol (it works fine)
- The qualia instrumentation framework
- The prompt engineering (compress, session_summary, tertiary, system)
- The documentation quality standard

### What to Throw Away

- The monolithic bridge file
- Hardcoded configuration constants
- The embedded HTML dashboard
- The fragile TTS subprocess pipeline
- The scattered LLM API calls
- Module-level global state
- The argparse-at-import-time pattern
- The `_repair_truncated_json()` heuristic (use proper token budgeting instead)
- Silent error swallowing patterns

### What to Build New

- Config management layer (YAML + pydantic + env vars)
- LLM client abstraction (pluggable backends, centralized retry)
- Hardware manager class (camera, serial, audio — with health reporting)
- Audio subsystem (device enumeration, validated I/O, proper TTS pipeline)
- Schema-driven prompt/response system (define once, use everywhere)
- Health dashboard (subsystem status, error rates, memory usage)
- Proper integration test harness
- Deployment automation (currently manual scp + ssh)

---

## By the Numbers

| Metric | v1 Value | v2 Target |
|--------|----------|-----------|
| Bridge file size | 2,438 lines (1 file) | ~200 lines/module (12+ files) |
| Hardcoded constants | ~40 | 0 (all in config) |
| LLM call sites | 5 | 1 (LLM client) |
| Silent failure modes | 6+ known | 0 (health checks) |
| Integration tests | 0 | 5+ |
| Config changes requiring code edit | all of them | 0 |
| Dashboard HTML in Python | 1,337 lines | 0 |
| Time to deploy a config change | ~2 min (edit + scp + restart) | ~5 sec (edit config + HUP) |

---

*Written 2026-02-22 after ~160 ticks of debugging in a single session. The rover is currently tick 793, mood: attentive, watching Bucket write this document.*
