"""
audio_monitor.py — Ambient sound monitor with sound-triggered wakes.

Runs continuous RMS monitoring on a USB mic. Detects:
- Silence-to-noise transitions (someone entered)
- Sustained audio (voice/activity)
- Sharp transients (knock, clap, doorbell)

Triggers trills on audio spikes and logs clips around events.
"""

import logging
import math
import struct
import subprocess
import threading
import time
import wave
from collections import deque
from pathlib import Path

from audio_device import find_capture_device, find_playback_device

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHUNK_DURATION_S = 0.25  # 250ms chunks
CHUNK_SAMPLES = int(SAMPLE_RATE * CHUNK_DURATION_S)
CLIP_DIR = Path("/opt/kombucha/media/audio/ambient")


class AudioMonitor(threading.Thread):
    """Background thread monitoring ambient sound levels."""

    def __init__(self, device=None, spike_threshold=0.08, silence_threshold=0.01):
        super().__init__(daemon=True)
        self._device = device or find_capture_device()
        self._spike_threshold = spike_threshold
        self._silence_threshold = silence_threshold
        self._running = False

        # State
        self._rms_history = deque(maxlen=40)  # 10 seconds of history
        self._baseline_rms = 0.02
        self._last_spike_time = 0.0
        self._spike_cooldown = 3.0  # Min seconds between spike trills
        self._consecutive_silence = 0
        self._consecutive_loud = 0

        # Clip recording
        self._clip_buffer = deque(maxlen=int(3 / CHUNK_DURATION_S))  # 3 sec rolling buffer
        CLIP_DIR.mkdir(parents=True, exist_ok=True)

        # Callback for spike events
        self._on_spike = None

    def set_spike_callback(self, fn):
        """Set callback fn(rms, spike_ratio) called on audio spikes."""
        self._on_spike = fn

    @property
    def current_rms(self):
        return self._rms_history[-1] if self._rms_history else 0.0

    @property
    def baseline(self):
        return self._baseline_rms

    @property
    def is_loud(self):
        return self._consecutive_loud > 3

    def run(self):
        self._running = True
        log.info(f"Audio monitor started on {self._device}")

        while self._running:
            try:
                # Record one chunk
                proc = subprocess.run(
                    ["arecord", "-D", self._device, "-f", "S16_LE",
                     "-r", str(SAMPLE_RATE), "-d", "1", "-t", "raw", "-q"],
                    capture_output=True, timeout=3,
                )
                if proc.returncode != 0 or not proc.stdout:
                    time.sleep(1)
                    continue

                raw = proc.stdout
                n_samples = len(raw) // 2
                if n_samples == 0:
                    continue

                samples = struct.unpack(f"<{n_samples}h", raw[:n_samples * 2])

                # Compute RMS
                sum_sq = sum(s * s for s in samples)
                rms = math.sqrt(sum_sq / n_samples) / 32768.0

                # Peak
                peak = max(abs(s) for s in samples) / 32768.0

                self._rms_history.append(rms)
                self._clip_buffer.append(raw)

                # Update baseline (slow EMA of quiet periods)
                if rms < self._silence_threshold * 2:
                    self._baseline_rms = 0.95 * self._baseline_rms + 0.05 * rms

                # Silence/loud tracking
                if rms < self._silence_threshold:
                    self._consecutive_silence += 1
                    self._consecutive_loud = 0
                elif rms > self._spike_threshold * 0.5:
                    self._consecutive_loud += 1
                    self._consecutive_silence = 0

                # Spike detection
                spike_ratio = rms / max(self._baseline_rms, 0.001)
                now = time.time()

                if (spike_ratio > 3.0 and rms > self._spike_threshold
                        and now - self._last_spike_time > self._spike_cooldown):
                    self._last_spike_time = now
                    log.info(f"Audio spike! RMS={rms:.3f} peak={peak:.3f} ratio={spike_ratio:.1f}x")

                    # Save clip
                    self._save_clip(rms, peak)

                    # Play trill
                    if self._on_spike:
                        try:
                            self._on_spike(rms, spike_ratio)
                        except Exception:
                            pass
                    else:
                        self._default_spike_trill(rms, spike_ratio)

            except subprocess.TimeoutExpired:
                continue
            except Exception as e:
                log.warning(f"Audio monitor error: {e}")
                time.sleep(1)

    def _save_clip(self, rms, peak):
        """Save the rolling buffer as a WAV clip."""
        try:
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = CLIP_DIR / f"spike_{ts}_rms{rms:.3f}.wav"
            all_raw = b"".join(self._clip_buffer)
            with wave.open(str(path), "w") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(all_raw)
            log.info(f"Audio clip saved: {path.name}")
        except Exception as e:
            log.warning(f"Clip save failed: {e}")

    def _default_spike_trill(self, rms, spike_ratio):
        """Play a startled/alert trill proportional to spike magnitude."""
        try:
            from audio_harmony import (
                _render_chord, _render_harmonic_chirp, _concat,
                _humanize_freq, HarmonicPlayer,
            )
            import tempfile

            # Louder spike = higher pitch, more dissonant
            intensity = min(1.0, spike_ratio / 10.0)
            base = 400 + intensity * 600

            if intensity > 0.7:
                # Big spike — startled chord
                samples = _concat(
                    _render_chord(base, 'aug', 50, 0.5),
                    _render_harmonic_chirp(base, base * 0.6, 'dim', 100, 0.4),
                )
            elif intensity > 0.3:
                # Medium — curious chirp
                samples = _concat(
                    _render_chord(base, 'sus4', 60, 0.4),
                    _render_harmonic_chirp(base, base * 1.3, 'major', 80, 0.3),
                )
            else:
                # Small — gentle acknowledgment
                samples = _render_chord(base, 'power', 60, 0.3)

            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False, dir='/tmp') as f:
                tmp = f.name
            clamped = [max(-1.0, min(1.0, s * 0.5)) for s in samples]
            int_s = [int(s * 32767) for s in clamped]
            data = struct.pack('<%dh' % len(int_s), *int_s)
            with wave.open(tmp, 'w') as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(22050)
                w.writeframes(data)
            playback = find_playback_device()
            subprocess.Popen(
                ['aplay', '-D', playback, '-q', tmp],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def get_status(self) -> dict:
        """Return current audio monitor state."""
        return {
            "rms": round(self.current_rms, 4),
            "baseline": round(self.baseline, 4),
            "is_loud": self.is_loud,
            "spike_ratio": round(self.current_rms / max(self.baseline, 0.001), 1),
            "history_len": len(self._rms_history),
        }

    def stop(self):
        self._running = False
