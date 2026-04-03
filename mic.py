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
import wave
from collections import deque
from pathlib import Path

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

# Adaptive noise floor
NOISE_FLOOR_ALPHA = 0.01  # EMA smoothing — slow adaptation to ambient level
NOISE_SHIFT_RATIO = 3.0  # current RMS must be Nx noise floor to trigger event
NOISE_SHIFT_CHUNKS = 5  # must exceed ratio for this many consecutive chunks (500ms)

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

        # Adaptive noise floor
        self._noise_floor = 0.002  # initial estimate (typical quiet room)
        self._above_noise_since = 0  # consecutive chunks above noise_shift ratio
        self._last_noise_shift_t = 0.0  # debounce noise_shift events

        # Raw audio buffer for clip saving (last 10 seconds)
        self._raw_buffer = deque(maxlen=int(10.0 / (CHUNK_MS / 1000)))
        self._clip_dir = Path("/opt/kombucha/media/audio/ticks")
        self._clip_dir.mkdir(parents=True, exist_ok=True)

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
                with self._lock:
                    self._raw_buffer.append(data)
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

        # Update adaptive noise floor (always, even when suppressed)
        self._noise_floor = (NOISE_FLOOR_ALPHA * rms +
                             (1 - NOISE_FLOOR_ALPHA) * self._noise_floor)

        if suppressed:
            return

        # Noise shift detection: room got louder relative to floor
        threshold = self._noise_floor * NOISE_SHIFT_RATIO
        if rms > threshold and self._noise_floor > 0.0005:
            self._above_noise_since += 1
            if (self._above_noise_since >= NOISE_SHIFT_CHUNKS and
                    (now - self._last_noise_shift_t) > 5.0):
                self._last_noise_shift_t = now
                event = AudioEvent("noise_shift", peak, rms)
                with self._lock:
                    self._events.append(event)
                logger.info(f"Audio noise_shift: rms={rms:.4f} floor={self._noise_floor:.4f} ratio={rms/self._noise_floor:.1f}x")
        else:
            self._above_noise_since = 0

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

    def save_clip(self, filename, duration_s=5.0):
        """Save the last duration_s seconds of buffered audio as a WAV file.

        Returns the path to the saved file, or None if no audio buffered.
        """
        chunks_needed = int(duration_s / (CHUNK_MS / 1000))
        with self._lock:
            if not self._raw_buffer:
                return None
            chunks = list(self._raw_buffer)[-chunks_needed:]

        raw_data = b"".join(chunks)
        if len(raw_data) < CHUNK_BYTES:
            return None

        path = self._clip_dir / filename
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(raw_data)

        logger.info(f"Saved audio clip: {path} ({len(raw_data)} bytes, {len(chunks)} chunks)")
        return str(path)

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
                "noise_floor": round(self._noise_floor, 4),
                "silence": self._silence,
                "events": recent_events[-5:],  # last 5 events within 30s
            }

    def stop(self):
        self._stop_event.set()


def analyze_clip(path):
    """Analyze a saved WAV clip. Returns stats dict with peak, mean_rms, max_rms, duration_s.

    Computes RMS across 100ms windows to find loudest moment and overall level.
    """
    path = str(path)
    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sampwidth != 2 or n_channels != 1:
        return {"error": f"unsupported format: {n_channels}ch {sampwidth*8}bit"}

    n_samples = len(raw) // 2
    if n_samples == 0:
        return {"error": "empty clip"}

    samples = struct.unpack(f'<{n_samples}h', raw)
    duration_s = n_samples / framerate

    # Global peak
    peak = max(abs(s) for s in samples) / 32768.0

    # Global mean RMS
    sum_sq = sum(s * s for s in samples) / (32768.0 * 32768.0)
    mean_rms = math.sqrt(sum_sq / n_samples)

    # Windowed RMS (100ms windows)
    window_samples = framerate // 10  # 100ms
    max_rms = 0.0
    rms_windows = []
    for i in range(0, n_samples, window_samples):
        chunk = samples[i:i + window_samples]
        if len(chunk) < window_samples // 2:
            break
        w_sq = sum(s * s for s in chunk) / (32768.0 * 32768.0)
        w_rms = math.sqrt(w_sq / len(chunk))
        rms_windows.append(w_rms)
        if w_rms > max_rms:
            max_rms = w_rms

    return {
        "duration_s": round(duration_s, 2),
        "peak": round(peak, 4),
        "mean_rms": round(mean_rms, 4),
        "max_rms": round(max_rms, 4),
        "n_windows": len(rms_windows),
    }
