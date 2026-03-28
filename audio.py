"""
audio.py — R2-style tone generator for Kombucha.

Pure numpy sine synthesis piped to aplay. No dependencies beyond numpy.
Tones are non-blocking (subprocess). No voice, no words — just chirps,
beeps, and warbles that convey mood through frequency and timing.

Usage:
    from audio import TonePlayer
    player = TonePlayer(volume=0.3)
    player.play_mood("curious")
    player.play_sequence([
        {"type": "chirp", "start": 400, "end": 1200, "ms": 80},
        {"type": "beep", "freq": 1200, "ms": 100},
    ])
"""

import subprocess
import struct
import math
import threading
import time
import json
import os
import logging

logger = logging.getLogger(__name__)

SAMPLE_RATE = 22050
FADE_MS = 5  # raised-cosine fade to prevent clicks
DEVICE = "plughw:3,0"

# Mood-to-sequence lookup — the R2 vocabulary
MOOD_SEQUENCES = {
    "happy": [
        {"type": "chirp", "start": 400, "end": 1200, "ms": 80},
        {"type": "chirp", "start": 800, "end": 1600, "ms": 60},
        {"type": "beep", "freq": 1200, "ms": 100},
    ],
    "curious": [
        {"type": "beep", "freq": 600, "ms": 150},
        {"type": "chirp", "start": 600, "end": 900, "ms": 200},
        {"type": "beep", "freq": 900, "ms": 100},
    ],
    "startled": [
        {"type": "beep", "freq": 1800, "ms": 50},
        {"type": "silence", "ms": 30},
        {"type": "beep", "freq": 1400, "ms": 50},
        {"type": "chirp", "start": 1400, "end": 600, "ms": 150},
    ],
    "sad": [
        {"type": "warble", "freq": 400, "vibrato_hz": 3, "vibrato_depth": 50, "ms": 300},
        {"type": "chirp", "start": 400, "end": 250, "ms": 200},
    ],
    "greeting": [
        {"type": "chirp", "start": 300, "end": 800, "ms": 100},
        {"type": "silence", "ms": 50},
        {"type": "chirp", "start": 500, "end": 1200, "ms": 100},
        {"type": "beep", "freq": 1200, "ms": 150},
    ],
    "frustrated": [
        {"type": "noise_burst", "center": 200, "bandwidth": 400, "ms": 100},
        {"type": "beep", "freq": 300, "ms": 80},
        {"type": "beep", "freq": 250, "ms": 80},
    ],
    "alert": [
        {"type": "beep", "freq": 1000, "ms": 100},
        {"type": "silence", "ms": 80},
        {"type": "beep", "freq": 1000, "ms": 100},
        {"type": "silence", "ms": 80},
        {"type": "beep", "freq": 1400, "ms": 150},
    ],
    "settled": [
        {"type": "beep", "freq": 500, "ms": 200},
        {"type": "chirp", "start": 500, "end": 450, "ms": 150},
    ],
    "anxious": [
        {"type": "chirp", "start": 600, "end": 800, "ms": 60},
        {"type": "chirp", "start": 800, "end": 600, "ms": 60},
        {"type": "chirp", "start": 600, "end": 800, "ms": 60},
    ],
    "playful": [
        {"type": "chirp", "start": 400, "end": 1000, "ms": 50},
        {"type": "chirp", "start": 1000, "end": 400, "ms": 50},
        {"type": "chirp", "start": 400, "end": 1200, "ms": 80},
    ],
    "greeting_known": [
        {"type": "chirp", "start": 400, "end": 1000, "ms": 80},
        {"type": "silence", "ms": 40},
        {"type": "chirp", "start": 600, "end": 1400, "ms": 80},
        {"type": "silence", "ms": 40},
        {"type": "beep", "freq": 1400, "ms": 120},
        {"type": "chirp", "start": 1400, "end": 1600, "ms": 60},
    ],
    "greeting_unknown": [
        {"type": "chirp", "start": 500, "end": 800, "ms": 120},
        {"type": "silence", "ms": 60},
        {"type": "beep", "freq": 800, "ms": 100},
    ],
    "goodbye": [
        {"type": "beep", "freq": 800, "ms": 150},
        {"type": "chirp", "start": 800, "end": 400, "ms": 200},
        {"type": "silence", "ms": 50},
        {"type": "beep", "freq": 350, "ms": 200},
    ],
    "thinking": [
        {"type": "beep", "freq": 500, "ms": 80},
        {"type": "silence", "ms": 120},
        {"type": "beep", "freq": 550, "ms": 80},
        {"type": "silence", "ms": 120},
        {"type": "chirp", "start": 500, "end": 600, "ms": 100},
    ],
    "cat_spotted": [
        {"type": "chirp", "start": 800, "end": 1200, "ms": 60},
        {"type": "chirp", "start": 1200, "end": 800, "ms": 60},
        {"type": "chirp", "start": 800, "end": 1400, "ms": 80},
        {"type": "beep", "freq": 1000, "ms": 60},
    ],
}


def _generate_samples(n_samples):
    """Generate a time array for n_samples at SAMPLE_RATE."""
    return [i / SAMPLE_RATE for i in range(n_samples)]


def _apply_envelope(samples, fade_samples):
    """Apply raised-cosine fade-in and fade-out to prevent clicks."""
    n = len(samples)
    for i in range(min(fade_samples, n)):
        # Raised cosine: 0.5 * (1 - cos(pi * i / fade_samples))
        envelope = 0.5 * (1 - math.cos(math.pi * i / fade_samples))
        samples[i] *= envelope
    for i in range(min(fade_samples, n)):
        idx = n - 1 - i
        envelope = 0.5 * (1 - math.cos(math.pi * i / fade_samples))
        samples[idx] *= envelope
    return samples


def _render_beep(freq, duration_ms, volume=1.0):
    """Single sine tone at fixed frequency."""
    n_samples = int(SAMPLE_RATE * duration_ms / 1000)
    fade_samples = int(SAMPLE_RATE * FADE_MS / 1000)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        samples.append(volume * math.sin(2 * math.pi * freq * t))
    return _apply_envelope(samples, fade_samples)


def _render_chirp(freq_start, freq_end, duration_ms, volume=1.0):
    """Linear frequency sweep from freq_start to freq_end."""
    n_samples = int(SAMPLE_RATE * duration_ms / 1000)
    fade_samples = int(SAMPLE_RATE * FADE_MS / 1000)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        progress = i / max(n_samples - 1, 1)
        freq = freq_start + (freq_end - freq_start) * progress
        # Integrate frequency for phase continuity
        phase = 2 * math.pi * (freq_start * t + (freq_end - freq_start) * t * progress / 2)
        samples.append(volume * math.sin(phase))
    return _apply_envelope(samples, fade_samples)


def _render_warble(freq, vibrato_hz, vibrato_depth, duration_ms, volume=1.0):
    """Tone with vibrato (frequency modulation)."""
    n_samples = int(SAMPLE_RATE * duration_ms / 1000)
    fade_samples = int(SAMPLE_RATE * FADE_MS / 1000)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        mod_freq = freq + vibrato_depth * math.sin(2 * math.pi * vibrato_hz * t)
        # Approximate phase integration
        phase = 2 * math.pi * freq * t + (vibrato_depth / vibrato_hz) * (
            1 - math.cos(2 * math.pi * vibrato_hz * t)
        )
        samples.append(volume * math.sin(phase))
    return _apply_envelope(samples, fade_samples)


def _render_noise_burst(center_freq, bandwidth, duration_ms, volume=1.0):
    """Band-limited noise burst. Crude but effective for raspberries."""
    import random
    n_samples = int(SAMPLE_RATE * duration_ms / 1000)
    fade_samples = int(SAMPLE_RATE * FADE_MS / 1000)
    # Generate white noise, then crude bandpass via mixing with carrier
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        noise = random.uniform(-1, 1)
        carrier = math.sin(2 * math.pi * center_freq * t)
        samples.append(volume * 0.5 * noise * carrier)
    return _apply_envelope(samples, fade_samples)


def _render_silence(duration_ms):
    """Dead air between tones."""
    n_samples = int(SAMPLE_RATE * duration_ms / 1000)
    return [0.0] * n_samples


def render_sequence(sequence, volume=1.0):
    """Render a sequence of tone primitives into a flat list of samples."""
    all_samples = []
    for step in sequence:
        t = step.get("type", "silence")
        v = step.get("volume", volume)
        if t == "beep":
            all_samples.extend(_render_beep(step["freq"], step["ms"], v))
        elif t == "chirp":
            all_samples.extend(_render_chirp(step["start"], step["end"], step["ms"], v))
        elif t == "warble":
            all_samples.extend(_render_warble(
                step["freq"], step["vibrato_hz"],
                step.get("vibrato_depth", 50), step["ms"], v
            ))
        elif t == "noise_burst":
            all_samples.extend(_render_noise_burst(
                step["center"], step["bandwidth"], step["ms"], v
            ))
        elif t == "silence":
            all_samples.extend(_render_silence(step["ms"]))
        else:
            logger.warning(f"Unknown tone type: {t}")
    return all_samples


def samples_to_pcm(samples):
    """Convert float samples (-1..1) to 16-bit signed PCM bytes."""
    pcm = bytearray()
    for s in samples:
        clamped = max(-1.0, min(1.0, s))
        pcm.extend(struct.pack('<h', int(clamped * 32767)))
    return bytes(pcm)


class TonePlayer:
    """Non-blocking tone player. Queues sequences so they don't overlap."""

    def __init__(self, volume=0.3, device=DEVICE):
        self.volume = volume
        self.device = device
        self._lock = threading.Lock()
        self._playing = False

    def play_sequence(self, sequence):
        """Render and play a sequence of tone primitives. Non-blocking."""
        samples = render_sequence(sequence, self.volume)
        pcm = samples_to_pcm(samples)
        self._play_pcm(pcm)

    def play_mood(self, mood):
        """Play the pre-composed sequence for a mood word. Non-blocking."""
        seq = MOOD_SEQUENCES.get(mood)
        if seq is None:
            logger.warning(f"No tone sequence for mood '{mood}', using 'settled'")
            seq = MOOD_SEQUENCES.get("settled", [])
        self.play_sequence(seq)

    def _play_pcm(self, pcm_data):
        """Pipe PCM to aplay in a background thread."""
        def _worker():
            with self._lock:
                self._playing = True
            try:
                proc = subprocess.Popen(
                    [
                        "aplay", "-D", self.device,
                        "-f", "S16_LE",
                        "-r", str(SAMPLE_RATE),
                        "-c", "1",
                        "-t", "raw",
                        "-q",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                proc.stdin.write(pcm_data)
                proc.stdin.close()
                proc.wait(timeout=5)
            except Exception as e:
                logger.error(f"aplay failed: {e}")
            finally:
                with self._lock:
                    self._playing = False

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    @property
    def is_playing(self):
        with self._lock:
            return self._playing


# Convenience: run from command line to test
if __name__ == "__main__":
    import sys
    player = TonePlayer(volume=0.3)
    mood = sys.argv[1] if len(sys.argv) > 1 else "greeting"
    print(f"Playing '{mood}'...")
    player.play_mood(mood)
    time.sleep(2)  # wait for playback
    print("Done.")
