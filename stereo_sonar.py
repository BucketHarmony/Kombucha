"""
stereo_sonar.py — Stereo spatial audio awareness.

Compares RMS levels from two physically separated microphones
to determine sound direction. Like sonar but passive.

Mic layout:
  - Left mic: C270 webcam (mounted rear/left)
  - Right mic: Realtek camera (mounted front/gimbal)

Difference in loudness → direction estimate.
"""

import logging
import math
import struct
import subprocess
import threading
import time
from collections import deque

from audio_device import find_capture_device

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class StereoSonar(threading.Thread):
    """Compare two mics to estimate sound direction."""

    def __init__(self, left_device=None, right_device=None):
        super().__init__(daemon=True)
        # Left = C270 webcam mic, Right = Realtek camera mic
        self._left_dev = left_device
        self._right_dev = right_device
        self._running = False

        # State
        self._left_rms = 0.0
        self._right_rms = 0.0
        self._direction = 0.0  # -1.0 = hard left, 0 = center, +1.0 = hard right
        self._confidence = 0.0  # 0 = quiet (no signal), 1.0 = clear direction
        self._history = deque(maxlen=20)  # Recent direction readings

    def _detect_devices(self):
        """Find the two USB mics by card name."""
        try:
            result = subprocess.run(
                ["arecord", "-l"], capture_output=True, text=True, timeout=5)
            import re
            cards = {}
            for line in result.stdout.split("\n"):
                m = re.match(r"card (\d+):.*\[(.+?)\]", line)
                if m:
                    card_num, name = m.group(1), m.group(2)
                    if "C270" in name or "WEBCAM" in name:
                        cards["left"] = f"plughw:{card_num},0"
                    elif "USB Camera" in name:
                        cards["right"] = f"plughw:{card_num},0"
                    elif "USB PnP" in name and "right" not in cards:
                        cards["right"] = f"plughw:{card_num},0"

            self._left_dev = cards.get("left")
            self._right_dev = cards.get("right")
            if self._left_dev and self._right_dev:
                log.info(f"Stereo sonar: L={self._left_dev} R={self._right_dev}")
                return True
            else:
                log.warning(f"Stereo sonar: only found {cards}, need 2 mics")
                return False
        except Exception as e:
            log.warning(f"Stereo sonar device detection failed: {e}")
            return False

    def _capture_rms(self, device):
        """Capture 0.5s of audio and return RMS."""
        try:
            proc = subprocess.run(
                ["arecord", "-D", device, "-f", "S16_LE",
                 "-r", str(SAMPLE_RATE), "-d", "1", "-t", "raw", "-q"],
                capture_output=True, timeout=3,
            )
            if proc.returncode != 0 or not proc.stdout:
                return 0.0
            raw = proc.stdout
            n = len(raw) // 2
            if n == 0:
                return 0.0
            samples = struct.unpack(f"<{n}h", raw[:n * 2])
            return math.sqrt(sum(s * s for s in samples) / n) / 32768.0
        except Exception:
            return 0.0

    def run(self):
        self._running = True

        if not self._left_dev or not self._right_dev:
            if not self._detect_devices():
                log.warning("Stereo sonar: insufficient mics, stopping")
                return

        log.info("Stereo sonar started")

        while self._running:
            # Capture from both mics (sequential — not truly simultaneous
            # but close enough for direction at human-movement speeds)
            left = self._capture_rms(self._left_dev)
            right = self._capture_rms(self._right_dev)

            self._left_rms = left
            self._right_rms = right

            # Direction: compare loudness
            total = left + right
            if total > 0.005:  # Above noise floor
                # -1 = all left, +1 = all right, 0 = equal
                self._direction = (right - left) / total
                self._confidence = min(1.0, total / 0.05)  # Scale confidence by volume
            else:
                self._direction = 0.0
                self._confidence = 0.0

            self._history.append({
                "dir": self._direction,
                "conf": self._confidence,
                "left": left,
                "right": right,
                "time": time.time(),
            })

            time.sleep(1)  # Sample every second

    def get_status(self) -> dict:
        """Return current spatial audio state."""
        # Direction label
        d = self._direction
        if self._confidence < 0.2:
            label = "quiet"
        elif d < -0.3:
            label = "LEFT"
        elif d > 0.3:
            label = "RIGHT"
        elif d < -0.1:
            label = "slight left"
        elif d > 0.1:
            label = "slight right"
        else:
            label = "center"

        return {
            "direction": round(self._direction, 2),
            "direction_label": label,
            "confidence": round(self._confidence, 2),
            "left_rms": round(self._left_rms, 4),
            "right_rms": round(self._right_rms, 4),
            "history_len": len(self._history),
        }

    def stop(self):
        self._running = False
