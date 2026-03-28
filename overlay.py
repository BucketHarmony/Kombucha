"""
overlay.py - Video HUD overlay renderer.

Draws real-time telemetry, detections, drives, mood, and events on frames
for the recorded video. All data pulled from live bridge state.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Colors (BGR)
C_AMBER = (42, 146, 212)
C_GOLD = (48, 168, 232)
C_WHITE = (200, 200, 200)
C_DIM = (100, 100, 100)
C_RED = (60, 60, 200)
C_GREEN = (120, 200, 100)
C_CYAN = (220, 200, 100)
C_MAGENTA = (255, 100, 255)
C_BG = (10, 10, 10)
C_BG_ALPHA = 0.6

# Detection box colors
DET_COLORS = {
    "person": (0, 180, 255),
    "cat": (255, 100, 255),
    "dog": (255, 180, 0),
    "chair": (100, 100, 255),
    "tv": (200, 200, 100),
    "bottle": (100, 200, 200),
}

# Drive bar colors
DRIVE_COLORS = {
    "wanderlust": (180, 120, 40),
    "curiosity": (40, 180, 120),
    "social": (40, 120, 220),
    "cringe": (60, 60, 180),
    "attachment": (160, 80, 160),
}


class OverlayRenderer:
    """Draws HUD overlay on video frames with live telemetry and state."""

    def __init__(self, cv_pipeline=None, cv_state=None, telemetry=None,
                 gimbal_arbiter=None, state_file: Optional[Path] = None,
                 goals_file: Optional[Path] = None):
        self._cv_pipe = cv_pipeline
        self._cv_state = cv_state
        self._telemetry = telemetry
        self._gimbal_arbiter = gimbal_arbiter
        self._state_file = state_file
        self._goals_file = goals_file

        # Event flash state
        self._last_person_time = 0.0
        self._last_cat_time = 0.0
        self._person_flash_until = 0.0
        self._cat_flash_until = 0.0

        # Cached state (reload periodically)
        self._drives = {}
        self._mood = ""
        self._goal = ""
        self._last_state_read = 0.0
        self._tick_num = 0

    def _read_state(self):
        """Read drives and mood from body_state.json (cached, every 5s)."""
        now = time.time()
        if now - self._last_state_read < 5.0:
            return
        self._last_state_read = now

        if self._state_file and self._state_file.exists():
            try:
                with open(self._state_file) as f:
                    s = json.load(f)
                self._drives = s.get("drives", {})
                self._mood = s.get("last_mood", "")
                self._tick_num = s.get("last_tick", 0)
            except Exception:
                pass

        if self._goals_file and self._goals_file.exists():
            try:
                text = self._goals_file.read_text().strip()
                # First non-empty, non-comment line
                for line in text.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self._goal = line[:60]
                        break
            except Exception:
                pass

    def render(self, frame: np.ndarray) -> np.ndarray:
        """Draw full HUD overlay on a frame. Returns new frame."""
        frame = frame.copy()
        h, w = frame.shape[:2]
        self._read_state()
        now = time.time()

        # --- Detection boxes ---
        dets = []
        if self._cv_pipe:
            dets = self._cv_pipe.get_detections()
            for det in dets:
                x, y, dw, dh = det["x"], det["y"], det["w"], det["h"]
                name = det.get("class_name", "?")
                conf = det.get("confidence", 0)
                color = DET_COLORS.get(name, (100, 200, 100))
                cv2.rectangle(frame, (x, y), (x + dw, y + dh), color, 2)
                label = f"{name} {conf:.0%}"
                cv2.putText(frame, label, (x, y - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)

                # Event flash triggers
                if name == "person":
                    if now - self._last_person_time > 5.0:
                        self._person_flash_until = now + 2.0
                    self._last_person_time = now
                elif name == "cat":
                    if now - self._last_cat_time > 5.0:
                        self._cat_flash_until = now + 2.0
                    self._last_cat_time = now

        # --- Event flashes (large text) ---
        if now < self._person_flash_until:
            alpha = min(1.0, (self._person_flash_until - now) / 1.0)
            self._draw_event_flash(frame, "PERSON DETECTED", C_GOLD, alpha)
        elif now < self._cat_flash_until:
            alpha = min(1.0, (self._cat_flash_until - now) / 1.0)
            self._draw_event_flash(frame, "CAT DETECTED", C_MAGENTA, alpha)

        # --- Tracking crosshair ---
        if self._cv_state:
            snap = self._cv_state.snapshot()
            target = snap.get("current_target")
            if target:
                cx_px = int(target["cx"] * w)
                cy_px = int(target["cy"] * h)
                cv2.drawMarker(frame, (cx_px, cy_px), C_RED,
                               cv2.MARKER_CROSS, 25, 2)

        # --- Top bar: timestamp, tick, gimbal mode ---
        self._draw_top_bar(frame, w, now)

        # --- Bottom bar: battery, speed, goal ---
        self._draw_bottom_bar(frame, w, h)

        # --- Right side: drive bars ---
        self._draw_drive_bars(frame, w, h)

        return frame

    def _draw_event_flash(self, frame, text, color, alpha):
        """Draw large centered event text with fade."""
        h, w = frame.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 1.2
        thickness = 3
        (tw, th), _ = cv2.getTextSize(text, font, scale, thickness)
        x = (w - tw) // 2
        y = (h + th) // 2 - 40

        # Background box
        overlay = frame.copy()
        cv2.rectangle(overlay, (x - 15, y - th - 15), (x + tw + 15, y + 15), C_BG, -1)
        cv2.addWeighted(overlay, alpha * C_BG_ALPHA, frame, 1 - alpha * C_BG_ALPHA, 0, frame)

        # Text with alpha approximation via color scaling
        c = tuple(int(v * alpha) for v in color)
        cv2.putText(frame, text, (x, y), font, scale, c, thickness)

    def _draw_top_bar(self, frame, w, now):
        """Draw top status bar."""
        bar_h = 22
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (w, bar_h), C_BG, -1)
        cv2.addWeighted(overlay, C_BG_ALPHA, frame, 1 - C_BG_ALPHA, 0, frame)

        ts = datetime.now().strftime("%H:%M:%S")
        parts = [ts]

        if self._tick_num:
            parts.append(f"T{self._tick_num}")

        if self._gimbal_arbiter:
            mode = self._gimbal_arbiter.mode.value.upper()
            parts.append(mode)

        if self._cv_state:
            snap = self._cv_state.snapshot()
            faces = snap.get("face_count", 0)
            if faces > 0:
                parts.append(f"FACES:{faces}")
            fps = snap.get("fps", 0)
            parts.append(f"{fps:.0f}fps")

        if self._mood:
            parts.append(self._mood)

        text = " | ".join(parts)
        cv2.putText(frame, text, (6, 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, C_AMBER, 1)

    def _draw_bottom_bar(self, frame, w, h):
        """Draw bottom status bar with battery, speed, goal."""
        bar_h = 22
        y0 = h - bar_h
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, y0), (w, h), C_BG, -1)
        cv2.addWeighted(overlay, C_BG_ALPHA, frame, 1 - C_BG_ALPHA, 0, frame)

        parts = []

        if self._telemetry:
            snap = self._telemetry.snapshot()
            bv = snap.get("battery_v", 0)
            if bv > 0:
                from hardware import BATTERY_MIN_V, BATTERY_MAX_V, _clamp
                pct = _clamp((bv - BATTERY_MIN_V) / (BATTERY_MAX_V - BATTERY_MIN_V) * 100, 0, 100)
                color = C_RED if pct < 20 else C_AMBER
                parts.append((f"BAT:{pct:.0f}%", color))

            wsl = snap.get("wheel_speed_l", 0)
            wsr = snap.get("wheel_speed_r", 0)
            spd = (abs(wsl) + abs(wsr)) / 2
            if spd > 0.01:
                parts.append((f"SPD:{spd:.2f}", C_GREEN))

            if snap.get("drive_commanded"):
                parts.append(("DRIVING", C_GOLD))

        if self._goal:
            parts.append((self._goal[:50], C_DIM))

        x = 6
        for text, color in parts:
            cv2.putText(frame, text, (x, h - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)
            (tw, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.35, 1)
            x += tw + 12

    def _draw_drive_bars(self, frame, w, h):
        """Draw drive level bars on right side."""
        if not self._drives:
            return

        bar_w = 6
        bar_max_h = 60
        x_start = w - 50
        y_start = 30
        gap = 14

        for i, (name, level) in enumerate(self._drives.items()):
            if name == "attachment":  # hidden drive
                continue
            x = x_start + i * gap
            color = DRIVE_COLORS.get(name, C_DIM)
            bar_h = int(level * bar_max_h)

            # Background
            cv2.rectangle(frame, (x, y_start), (x + bar_w, y_start + bar_max_h), C_DIM, 1)
            # Fill
            if bar_h > 0:
                cv2.rectangle(frame, (x, y_start + bar_max_h - bar_h),
                              (x + bar_w, y_start + bar_max_h), color, -1)
            # Label
            label = name[0].upper()
            cv2.putText(frame, label, (x, y_start + bar_max_h + 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.3, color, 1)

            # HIGH indicator
            threshold = {"wanderlust": 0.8, "curiosity": 0.7, "social": 0.6, "cringe": 0.7}.get(name, 0.7)
            if level >= threshold:
                cv2.putText(frame, "!", (x + 1, y_start - 3),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, C_GOLD, 1)
