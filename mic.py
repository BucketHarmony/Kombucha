"""
mic.py — Background microphone listener for Kombucha.

Reads audio from USB mic via arecord, computes RMS level, detects
audio events (impulse, sustained). Runs in a daemon thread so it
never blocks the bridge.

Usage:
    from mic import AudioListener
    listener = AudioListener(device="plughw:2,0")
    listener.start()
    print(listener.snapshot())  # {"rms": 0.03, "peak": 0.12, "silence": True, ...}
    listener.stop()
"""

import logging
import math
import struct
import subprocess
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHUNK_MS = 100  # read 100ms chunks
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000  # 1600 samples
CHUNK_BYTES = CHUNK_SAMPLES * 2  # 16-bit = 2 bytes per sample

# Thresholds (normalized 0-1 scale, where 1.0 = max int16)
SILENCE_THRESHOLD = 0.01  # below this RMS = silence
IMPULSE_THRESHOLD = 0.15  # sudden spike above this = impulse event
SUSTAINED_THRESHOLD = 0.03  # above this for >1s = sustained sound
SUSTAINED_DURATION_S = 1.0

# How many chunks to keep for rolling average
HISTORY_SIZE = 30  # 3 seconds at 100ms chunks


class AudioEvent:
    """A detected audio event."""
    __slots__ = ("event_type", "timestamp", "peak", "rms", "duration_s")

    def __init__(self, event_type, peak, rms, duration_s=0.0):
        self.event_type = event_type  # "impulse" or "sustained"
        self.timestamp = time.time()
        self.peak = peak
        self.rms = rms
        self.duration_s = duration_s

    def to_dict(self):
        return {
            "type": self.event_type,
            "t": round(self.timestamp, 2),
            "peak": round(self.peak, 4),
            "rms": round(self.rms, 4),
            "duration_s": round(self.duration_s, 2),
        }


class AudioListener(threading.Thread):
    """Background thread that continuously reads mic input and computes levels."""

    def __init__(self, device="plughw:2,0"):
        super().__init__(daemon=True, name="AudioListener")
        self.device = device
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Current state
        self._rms = 0.0
        self._peak = 0.0
        self._silence = True
        self._rms_history = deque(maxlen=HISTORY_SIZE)

        # Event detection
        self._above_sustained_since = None  # timestamp when RMS went above sustained threshold
        self._events = deque(maxlen=20)  # last 20 events
        self._last_impulse_t = 0.0  # debounce impulses

        # Suppress self-sound: when tone_player is active, ignore audio
        self._suppress_until = 0.0

    def suppress(self, duration_s=1.0):
        """Suppress audio detection for duration_s (e.g., while playing tones)."""
        self._suppress_until = time.time() + duration_s

    def run(self):
        logger.info(f"AudioListener starting on {self.device}")
        while not self._stop_event.is_set():
            try:
                self._listen_loop()
            except Exception as e:
                logger.error(f"AudioListener error: {e}")
                if not self._stop_event.is_set():
                    time.sleep(2)  # back off before retry

    def _listen_loop(self):
        """Open arecord and read chunks until stopped or error."""
        proc = subprocess.Popen(
            [
                "arecord", "-D", self.device,
                "-f", "S16_LE",
                "-r", str(SAMPLE_RATE),
                "-c", "1",
                "-t", "raw",
                "-q",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        try:
            while not self._stop_event.is_set():
                data = proc.stdout.read(CHUNK_BYTES)
                if not data or len(data) < CHUNK_BYTES:
                    break
                self._process_chunk(data)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _process_chunk(self, data):
        """Compute RMS and peak from raw PCM, detect events."""
        # Decode 16-bit signed LE samples
        n_samples = len(data) // 2
        samples = struct.unpack(f'<{n_samples}h', data)

        # Normalize to 0-1
        peak = max(abs(s) for s in samples) / 32768.0
        sum_sq = sum(s * s for s in samples) / (32768.0 * 32768.0)
        rms = math.sqrt(sum_sq / n_samples) if n_samples > 0 else 0.0

        silence = rms < SILENCE_THRESHOLD
        now = time.time()
        suppressed = now < self._suppress_until

        with self._lock:
            self._rms = rms
            self._peak = peak
            self._silence = silence
            self._rms_history.append(rms)

        if suppressed:
            return

        # Impulse detection: sudden spike
        if peak > IMPULSE_THRESHOLD and (now - self._last_impulse_t) > 0.5:
            self._last_impulse_t = now
            event = AudioEvent("impulse", peak, rms)
            with self._lock:
                self._events.append(event)
            logger.info(f"Audio impulse: peak={peak:.3f} rms={rms:.3f}")

        # Sustained sound detection
        if rms > SUSTAINED_THRESHOLD:
            if self._above_sustained_since is None:
                self._above_sustained_since = now
            elif (now - self._above_sustained_since) >= SUSTAINED_DURATION_S:
                duration = now - self._above_sustained_since
                # Only log sustained events every 2 seconds
                event = AudioEvent("sustained", peak, rms, duration)
                with self._lock:
                    # Replace last sustained event if recent
                    if (self._events and
                            self._events[-1].event_type == "sustained" and
                            now - self._events[-1].timestamp < 2.0):
                        self._events[-1] = event
                    else:
                        self._events.append(event)
        else:
            self._above_sustained_since = None

    def snapshot(self):
        """Return current audio state as a dict."""
        with self._lock:
            avg_rms = (sum(self._rms_history) / len(self._rms_history)
                       if self._rms_history else 0.0)
            recent_events = [e.to_dict() for e in self._events
                             if time.time() - e.timestamp < 30]
            return {
                "rms": round(self._rms, 4),
                "peak": round(self._peak, 4),
                "avg_rms": round(avg_rms, 4),
                "silence": self._silence,
                "events": recent_events[-5:],  # last 5 events within 30s
            }

    def stop(self):
        self._stop_event.set()
