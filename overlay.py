"""
overlay.py - Full HUD overlay renderer for Kombucha video.

Draws ALL available real-time data on recorded video frames.
"""

import json
import logging
import math
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
C_DIM = (80, 80, 80)
C_DARK = (50, 50, 50)
C_RED = (60, 60, 200)
C_GREEN = (120, 200, 100)
C_CYAN = (220, 200, 100)
C_MAGENTA = (255, 100, 255)
C_BLUE = (220, 140, 40)
C_BG = (10, 10, 10)
C_BG_ALPHA = 0.55

DET_COLORS = {
    "person": (0, 180, 255), "cat": (255, 100, 255), "dog": (255, 180, 0),
    "chair": (100, 100, 255), "tv": (200, 200, 100), "bottle": (100, 200, 200),
    "couch": (150, 100, 200), "laptop": (200, 150, 100), "book": (100, 200, 150),
}

DRIVE_COLORS = {
    "wanderlust": (180, 120, 40), "curiosity": (40, 180, 120),
    "social": (40, 120, 220), "cringe": (60, 60, 180),
}

FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_S = cv2.FONT_HERSHEY_PLAIN


class OverlayRenderer:
    """Full HUD overlay with all available telemetry."""

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
        self._event_flash_text = ""
        self._event_flash_color = C_GOLD
        self._event_flash_until = 0.0
        self._last_person_time = 0.0
        self._last_cat_time = 0.0

        # Cached state
        self._drives = {}
        self._mood = ""
        self._goal = ""
        self._last_state_read = 0.0
        self._tick_num = 0

        # Audio event cache
        self._last_audio_label = ""
        self._last_audio_time = 0.0

        # Presence history for mini chart
        self._presence_history = []

    def _read_state(self):
        now = time.time()
        if now - self._last_state_read < 3.0:
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
                for line in self._goals_file.read_text().strip().split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self._goal = line[:55]
                        break
            except Exception:
                pass

        # Read last audio event
        try:
            manifest = Path("/opt/kombucha/media/audio/manifest.jsonl")
            if manifest.exists():
                last_line = ""
                with open(manifest, "rb") as f:
                    f.seek(0, 2)
                    pos = f.tell()
                    while pos > 0:
                        pos -= 1
                        f.seek(pos)
                        if f.read(1) == b"\n" and last_line:
                            break
                        f.seek(pos)
                        last_line = f.read(1).decode() + last_line
                if last_line.strip():
                    entry = json.loads(last_line.strip())
                    self._last_audio_label = entry.get("label", "")
                    ts = entry.get("timestamp", "")
                    if ts:
                        from datetime import datetime as dt
                        t = dt.fromisoformat(ts)
                        self._last_audio_time = t.timestamp()
        except Exception:
            pass

    def _box(self, frame, x, y, w, h, alpha=C_BG_ALPHA):
        """Draw a semi-transparent background box."""
        overlay = frame.copy()
        cv2.rectangle(overlay, (x, y), (x + w, y + h), C_BG, -1)
        cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)

    def render(self, frame: np.ndarray) -> np.ndarray:
        frame = frame.copy()
        fh, fw = frame.shape[:2]
        self._read_state()
        now = time.time()

        # Get telemetry snapshot once
        tsnap = self._telemetry.snapshot() if self._telemetry else {}
        cv_snap = self._cv_state.snapshot() if self._cv_state else {}

        # Compute sense values
        from hardware import BATTERY_MIN_V, BATTERY_MAX_V, _clamp
        bv = tsnap.get("battery_v", 0)
        battery_pct = _clamp((bv - BATTERY_MIN_V) / (BATTERY_MAX_V - BATTERY_MIN_V) * 100, 0, 100) if bv > 0 else 0
        wsl = tsnap.get("wheel_speed_l", 0)
        wsr = tsnap.get("wheel_speed_r", 0)
        speed = (abs(wsl) + abs(wsr)) / 2
        moving = speed > 0.01
        ax, ay, az = tsnap.get("ax", 0), tsnap.get("ay", 0), tsnap.get("az", 0)
        denom = math.sqrt(ay * ay + az * az)
        tilt_deg = round(math.degrees(math.atan2(-ax, denom)), 1) if denom > 0.001 else 0.0
        roll_deg = round(math.degrees(math.atan2(ay, az)), 1) if abs(az) > 0.001 else 0.0
        pan_pos = tsnap.get("gimbal_pan", 0)
        tilt_pos = tsnap.get("gimbal_tilt", 0)
        cpu_temp = None
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                cpu_temp = round(int(f.read().strip()) / 1000, 1)
        except Exception:
            pass

        odl = tsnap.get("odl", 0)
        odr = tsnap.get("odr", 0)
        session_start_l = tsnap.get("odom_session_start_l", 0)
        session_start_r = tsnap.get("odom_session_start_r", 0)
        distance_m = abs(((odl - session_start_l) + (odr - session_start_r)) / 2) / 1000

        stuck = False
        if tsnap.get("drive_commanded"):
            elapsed = now - tsnap.get("drive_command_time", 0)
            if elapsed > 0.5:
                od_l = abs(odl - tsnap.get("odom_at_command_l", 0))
                od_r = abs(odr - tsnap.get("odom_at_command_r", 0))
                if od_l < 2 and od_r < 2:
                    stuck = True

        faces = cv_snap.get("face_count", 0)
        motion = cv_snap.get("motion_detected", False)
        fps = cv_snap.get("fps", 0)
        target = cv_snap.get("current_target")
        presence = cv_snap.get("presence_30s", {})
        motion_regions = cv_snap.get("motion_region_count", 0)
        gimbal_mode = self._gimbal_arbiter.mode.value if self._gimbal_arbiter else "idle"
        queue_depth = self._gimbal_arbiter.snapshot().get("queue_depth", 0) if self._gimbal_arbiter else 0
        last_target_age = cv_snap.get("last_target_age_s") if "last_target_age_s" in str(cv_snap) else None
        wake_active = False

        # --- DETECTION BOXES ---
        dets = self._cv_pipe.get_detections() if self._cv_pipe else []
        for det in dets:
            x, y, dw, dh = det["x"], det["y"], det["w"], det["h"]
            name = det.get("class_name", "?")
            conf = det.get("confidence", 0)
            color = DET_COLORS.get(name, (100, 200, 100))
            cv2.rectangle(frame, (x, y), (x + dw, y + dh), color, 2)
            label = f"{name} {conf:.0%}"
            cv2.putText(frame, label, (x, y - 6), FONT, 0.4, color, 1)

            if name == "person" and now - self._last_person_time > 5.0:
                self._event_flash_text = "PERSON DETECTED"
                self._event_flash_color = C_GOLD
                self._event_flash_until = now + 2.0
                self._last_person_time = now
            elif name == "cat" and now - self._last_cat_time > 5.0:
                self._event_flash_text = "CAT DETECTED"
                self._event_flash_color = C_MAGENTA
                self._event_flash_until = now + 2.0
                self._last_cat_time = now

        # --- STUCK FLASH ---
        if stuck:
            self._event_flash_text = "STUCK"
            self._event_flash_color = C_RED
            self._event_flash_until = now + 1.0

        # --- EVENT FLASH ---
        if now < self._event_flash_until:
            alpha = min(1.0, (self._event_flash_until - now) / 1.0)
            self._draw_event_flash(frame, self._event_flash_text, self._event_flash_color, alpha)

        # --- TRACKING CROSSHAIR ---
        if target:
            cx_px = int(target["cx"] * fw)
            cy_px = int(target["cy"] * fh)
            cv2.drawMarker(frame, (cx_px, cy_px), C_RED, cv2.MARKER_CROSS, 25, 2)

        # --- TOP BAR ---
        self._box(frame, 0, 0, fw, 24)
        ts = datetime.now().strftime("%H:%M:%S")
        mode_color = C_GOLD if gimbal_mode == "instinct" else C_GREEN if gimbal_mode == "manual" else C_AMBER
        rec_dot = "REC" if True else ""  # always recording during ticks
        top_parts = [
            (ts, C_DIM),
            (f"T{self._tick_num}", C_AMBER) if self._tick_num else None,
            (gimbal_mode.upper(), mode_color),
            (f"FACES:{faces}", C_GOLD) if faces > 0 else None,
            (f"MOT:{motion_regions}", C_GREEN) if motion else None,
            (f"Q:{queue_depth}", C_CYAN) if queue_depth > 0 else None,
            (f"{fps:.0f}fps", C_DIM),
            (self._mood, C_AMBER) if self._mood else None,
            (rec_dot, C_RED),
        ]
        x = 6
        for part in top_parts:
            if part is None:
                continue
            txt, col = part
            cv2.putText(frame, txt, (x, 16), FONT, 0.35, col, 1)
            (tw, _), _ = cv2.getTextSize(txt, FONT, 0.35, 1)
            x += tw + 10

        # --- BOTTOM BAR ---
        self._box(frame, 0, fh - 24, fw, 24)
        bat_color = C_RED if battery_pct < 20 else C_AMBER
        bot_parts = [
            (f"BAT:{battery_pct:.0f}% ({bv:.1f}V)", bat_color),
            (f"CPU:{cpu_temp}C", C_RED if cpu_temp and cpu_temp > 70 else C_DIM) if cpu_temp else None,
            (f"SPD:{speed:.2f}", C_GREEN) if moving else None,
            ("DRIVING", C_GOLD) if tsnap.get("drive_commanded") else None,
            ("STUCK!", C_RED) if stuck else None,
            (f"ODO:{distance_m:.1f}m", C_CYAN),
            (f"DRIFT:{'L' if odr > odl * 1.1 and odl != 0 else 'R' if odl > odr * 1.1 and odr != 0 else '-'}", C_DIM),
        ]
        x = 6
        for part in bot_parts:
            if part is None:
                continue
            txt, col = part
            cv2.putText(frame, txt, (x, fh - 6), FONT, 0.33, col, 1)
            (tw, _), _ = cv2.getTextSize(txt, FONT, 0.33, 1)
            x += tw + 10

        # --- RIGHT PANEL: Drives + Gimbal + Presence ---
        panel_x = fw - 75
        self._box(frame, panel_x - 5, 26, 80, fh - 52)

        # Drive bars
        bar_w = 8
        bar_max_h = 50
        y_start = 32
        gap = 16
        for i, (name, level) in enumerate(self._drives.items()):
            if name == "attachment":
                continue
            bx = panel_x + i * gap
            color = DRIVE_COLORS.get(name, C_DIM)
            bh = int(level * bar_max_h)
            cv2.rectangle(frame, (bx, y_start), (bx + bar_w, y_start + bar_max_h), C_DARK, 1)
            if bh > 0:
                cv2.rectangle(frame, (bx, y_start + bar_max_h - bh),
                              (bx + bar_w, y_start + bar_max_h), color, -1)
            cv2.putText(frame, name[0].upper(), (bx, y_start + bar_max_h + 10), FONT, 0.28, color, 1)
            threshold = {"wanderlust": 0.8, "curiosity": 0.7, "social": 0.6, "cringe": 0.7}.get(name, 0.7)
            if level >= threshold:
                cv2.putText(frame, "!", (bx + 1, y_start - 2), FONT, 0.3, C_GOLD, 1)

        # Gimbal position indicator
        gy = y_start + bar_max_h + 22
        cv2.putText(frame, "GIMBAL", (panel_x - 2, gy), FONT, 0.25, C_DIM, 1)
        gy += 12
        cv2.putText(frame, f"P:{int(pan_pos):>4}", (panel_x - 2, gy), FONT, 0.3, C_AMBER, 1)
        gy += 12
        cv2.putText(frame, f"T:{int(tilt_pos):>4}", (panel_x - 2, gy), FONT, 0.3, C_AMBER, 1)

        # Artificial horizon (tiny)
        gy += 16
        hz_cx = panel_x + 30
        hz_cy = gy + 8
        hz_r = 14
        cv2.circle(frame, (hz_cx, hz_cy), hz_r, C_DARK, 1)
        # Horizon line rotated by roll
        roll_rad = math.radians(roll_deg)
        dx = int(hz_r * math.cos(roll_rad))
        dy = int(hz_r * math.sin(roll_rad))
        cv2.line(frame, (hz_cx - dx, hz_cy + dy), (hz_cx + dx, hz_cy - dy), C_CYAN, 1)
        # Tilt marker
        tilt_y = hz_cy - int((tilt_deg / 90) * hz_r)
        cv2.line(frame, (hz_cx - 3, tilt_y), (hz_cx + 3, tilt_y), C_AMBER, 1)

        # Presence (30s)
        gy += 28
        cv2.putText(frame, "PRESENCE", (panel_x - 2, gy), FONT, 0.25, C_DIM, 1)
        gy += 2
        for name, pct in list(presence.items())[:4]:
            gy += 11
            bar_len = int(pct / 100 * 55)
            cv2.rectangle(frame, (panel_x, gy - 6), (panel_x + bar_len, gy), C_GREEN, -1)
            cv2.putText(frame, f"{name[:6]}", (panel_x, gy + 8), FONT, 0.22, C_DIM, 1)

        # --- LEFT PANEL: Audio + Wake ---
        self._box(frame, 0, 26, 90, 80)
        ly = 38
        # Audio last event
        if self._last_audio_label and now - self._last_audio_time < 30:
            cv2.putText(frame, f"♪ {self._last_audio_label}", (4, ly), FONT, 0.3, C_MAGENTA, 1)
        else:
            cv2.putText(frame, "♪ --", (4, ly), FONT, 0.3, C_DARK, 1)

        # Wake status
        ly += 14
        if faces > 0:
            cv2.putText(frame, "WAKE ACTIVE", (4, ly), FONT, 0.28, C_GOLD, 1)
        elif motion:
            cv2.putText(frame, "MOTION", (4, ly), FONT, 0.28, C_GREEN, 1)
        else:
            cv2.putText(frame, "IDLE", (4, ly), FONT, 0.28, C_DARK, 1)

        # Target age
        ly += 14
        arb_snap = self._gimbal_arbiter.snapshot() if self._gimbal_arbiter else {}
        age = arb_snap.get("last_target_age_s")
        if age is not None:
            cv2.putText(frame, f"TGT:{age:.0f}s ago", (4, ly), FONT, 0.25, C_DIM, 1)

        # Heading
        ly += 14
        mx, my = tsnap.get("mx", 0), tsnap.get("my", 0)
        if mx != 0 or my != 0:
            heading = round((math.degrees(math.atan2(my, mx)) + 360) % 360, 0)
            cv2.putText(frame, f"HDG:{heading:.0f}", (4, ly), FONT, 0.28, C_CYAN, 1)

        # --- GOAL TEXT (above bottom bar) ---
        if self._goal:
            self._box(frame, 0, fh - 38, fw, 14)
            cv2.putText(frame, self._goal[:65], (6, fh - 28), FONT, 0.28, C_DIM, 1)

        return frame

    def _draw_event_flash(self, frame, text, color, alpha):
        h, w = frame.shape[:2]
        scale = 1.0
        thickness = 2
        (tw, th), _ = cv2.getTextSize(text, FONT, scale, thickness)
        x = (w - tw) // 2
        y = (h + th) // 2 - 40
        overlay = frame.copy()
        cv2.rectangle(overlay, (x - 12, y - th - 12), (x + tw + 12, y + 12), C_BG, -1)
        cv2.addWeighted(overlay, alpha * C_BG_ALPHA, frame, 1 - alpha * C_BG_ALPHA, 0, frame)
        c = tuple(int(v * alpha) for v in color)
        cv2.putText(frame, text, (x, y), FONT, scale, c, thickness)
