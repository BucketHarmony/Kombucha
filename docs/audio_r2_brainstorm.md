# Audio Endpoint Brainstorm: R2-Style Tones

**Date**: 2026-03-28
**Status**: Brainstorm
**Constraint**: No voice synthesis. Bucket's daughter finds espeak creepy. All expression through tones, chirps, and tweets — like R2-D2.

---

## What We Have

- **Playback**: USB PnP Audio Device at `plughw:3,0` — confirmed working
- **Capture**: USB mic at `plughw:3,0` (same device) + camera mic at `plughw:2,0`
- **Libraries**: numpy (tone generation), aplay (playback), ffmpeg (mixing/conversion)
- **Missing**: No sounddevice, no simpleaudio, no sox. Pure numpy + aplay pipeline.
- **Proven**: `python3 → numpy sine wave → stdout → aplay` works. Sub-50ms latency for short tones.

## Core Idea

R2-D2's voice is just rapid sequences of sine waves at varying frequencies, durations, and envelopes. No words. Pure musical emotion. A happy chirp is a fast ascending sequence. A sad warble is a slow descending one with vibrato. A startled beep is a single sharp high note.

We can do this with nothing but numpy and aplay.

## Tone Primitives

| Primitive | Description | Parameters |
|-----------|-------------|------------|
| `beep` | Single tone | freq_hz, duration_ms, volume (0-1) |
| `chirp` | Frequency sweep | freq_start, freq_end, duration_ms |
| `warble` | Tone with vibrato | freq_hz, vibrato_hz, vibrato_depth, duration_ms |
| `noise_burst` | Filtered noise | center_freq, bandwidth, duration_ms |
| `silence` | Gap between tones | duration_ms |

## Emotion Sequences (the R2 vocabulary)

| Mood | Sequence | Feel |
|------|----------|------|
| **happy** | chirp(400→1200, 80ms) + chirp(800→1600, 60ms) + beep(1200, 100ms) | ascending excitement |
| **curious** | beep(600, 150ms) + chirp(600→900, 200ms) + beep(900, 100ms) | questioning rise |
| **startled** | beep(1800, 50ms) + silence(30ms) + beep(1400, 50ms) + chirp(1400→600, 150ms) | sharp then falling |
| **sad** | warble(400, 3hz, 50hz, 300ms) + chirp(400→250, 200ms) | slow descent with wobble |
| **greeting** | chirp(300→800, 100ms) + silence(50ms) + chirp(500→1200, 100ms) + beep(1200, 150ms) | the classic R2 "boo-BEE-boop" |
| **frustrated** | noise_burst(200, 400, 100ms) + beep(300, 80ms) + beep(250, 80ms) | raspberry + grumble |
| **alert** | beep(1000, 100ms) + silence(80ms) + beep(1000, 100ms) + silence(80ms) + beep(1400, 150ms) | attention pattern |
| **settled** | beep(500, 200ms) + chirp(500→450, 150ms) | content sigh |
| **anxious** | chirp(600→800, 60ms) + chirp(800→600, 60ms) + chirp(600→800, 60ms) | rapid oscillation |
| **playful** | chirp(400→1000, 50ms) + chirp(1000→400, 50ms) + chirp(400→1200, 80ms) | bouncy |

## Bridge API Design

### New Endpoint: POST /action type=sound

```json
{
  "type": "sound",
  "mood": "happy"
}
```

Plays the pre-composed sequence for that mood. Simple. Maps directly to the mood word the soul already returns every tick.

### Extended: POST /action type=sound (custom sequence)

```json
{
  "type": "sound",
  "sequence": [
    {"type": "chirp", "start": 400, "end": 1200, "ms": 80},
    {"type": "beep", "freq": 1200, "ms": 100}
  ]
}
```

For when the soul or body wants to compose on the fly.

### Listening Endpoint: GET /audio/level

Returns current RMS audio level from the mic. Useful for detecting when someone is talking, a door slams, the cat meows. No STT needed — just amplitude.

```json
{"rms": 0.034, "peak": 0.12, "silence": true}
```

### Listening Endpoint: GET /audio/listen?duration=3

Records N seconds, returns frequency analysis. Not speech recognition — just "was that a high sound or a low sound, loud or quiet, rhythmic or sudden."

```json
{"duration_s": 3, "avg_rms": 0.05, "events": [{"t": 1.2, "type": "impulse", "peak": 0.8}]}
```

## Implementation Plan

### Phase 1: Tone Generator Module (`audio.py`)
- Pure numpy tone synthesis (beep, chirp, warble, noise_burst)
- Envelope shaping (attack/decay so tones don't click)
- Sequence player: takes a list of primitives, renders to PCM, pipes to aplay
- Non-blocking: runs aplay in subprocess so it doesn't block the bridge
- Mood-to-sequence lookup table (like mood_gestures.json but for sound)

### Phase 2: Bridge Integration
- Add `sound` action type to `/action` endpoint in bridge.py
- Sound plays alongside gesture — mood_gestures.json gets optional `sound` field
- Every tick gesture now has an audio component

### Phase 3: Microphone Input
- Background thread reading from `plughw:3,0` (or `plughw:2,0`)
- RMS level computation, exposed via `/audio/level`
- Event detection (impulse = sudden loud noise, sustained = talking/music)
- Feed audio events into `/sense` so the soul knows about sounds

### Phase 4: Reactive Audio
- Hear a loud noise → startled beep + instinct wake
- Hear sustained talking → social drive charges
- Silence after noise → curious chirp
- Audio events become wake triggers alongside face/motion

## Technical Notes

- **Latency**: numpy generates a 200ms tone in <1ms. aplay startup is ~20ms. Total: well under 50ms to first sound.
- **Non-blocking**: `subprocess.Popen(['aplay', ...], stdin=PIPE)` — fire and forget.
- **Sample rate**: 22050 Hz is fine for tones up to 10kHz. No need for 44100.
- **Click prevention**: Apply 5ms raised-cosine fade-in/fade-out to every tone segment.
- **Volume**: Scale globally. Might want a volume setting in perception.toml.
- **Simultaneous sounds**: Don't. Queue them. Overlapping aplay processes sound terrible.

## Mood-Sound-Gesture Unification

Right now mood_gestures.json maps mood → gimbal/light sequence. The natural extension:

```json
{
  "curious": {
    "description": "Investigating something new",
    "sequence": [
      {"type": "look", "pan": 0, "tilt": -10, "speed": 80},
      {"type": "sound", "mood": "curious"},
      {"type": "wait", "ms": 200},
      {"type": "look", "pan": 10, "tilt": 0, "speed": 60},
      {"type": "lights", "head": 180}
    ]
  }
}
```

Sound becomes just another action primitive in the gesture system. The soul says "curious," the body plays the whole multimodal expression — head tilt + chirp + light pulse — as one atomic gesture.

## Open Questions

1. **Volume control** — How loud? Need to test in the house. Too loud wakes people up, too quiet is pointless.
2. **Sound library growth** — Should the soul be able to compose novel sequences, or stick to the preset mood vocabulary? (Start with presets, grow later.)
3. **Mic input priority** — Which mic is better? Camera mic might pick up motor noise. USB PnP might be cleaner. Need to test both.
4. **Daughter-safe testing** — Test tones at low volume first. Some frequencies are more startling than others. Avoid sharp high-pitched beeps above 2kHz until we know what's comfortable.
5. **Can the soul hear itself?** — If the mic picks up our own tones, we need to suppress that (same pattern as the self-flinch fix for vision).

## Dependencies

- numpy (already installed)
- aplay (already installed)
- arecord (already installed, for Phase 3)
- No pip installs needed for Phase 1-2

## Estimated Complexity

- Phase 1 (audio.py): ~150 lines of Python
- Phase 2 (bridge integration): ~30 lines added to bridge.py
- Phase 3 (mic input): ~100 lines, new background thread
- Phase 4 (reactive): ~50 lines wiring audio events to wake system
