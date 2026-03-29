"""
gimbal.py - Gimbal arbitration and heartbeat idle gestures.

GimbalArbiter manages instinct vs cognitive vs manual gimbal control.
Heartbeat generates periodic idle fidgets.
"""

import enum
import dataclasses
import logging
import random
import threading
import time
from collections import deque
from typing import Optional

import cv2
import numpy as np
from datetime import datetime
from pathlib import Path

from hardware import (
    validate_tcode, send_tcode, _clamp,
    CAPTURE_W, CAPTURE_H,
    CV_QUEUE_MAX_DEPTH, CV_QUEUE_STALE_S, CV_DEAD_ZONE_PX,
    CV_KP_PAN, CV_KP_TILT, CV_MAX_STEP_DEG, CV_SMOOTHING,
    CV_HYSTERESIS_S, CV_MANUAL_TIMEOUT_S,
    HEARTBEAT_INTERVAL_S, JPEG_QUALITY,
)

# Audio — import lazily to avoid circular deps / missing module
_tone_player = None

def _get_tone_player():
    global _tone_player
    if _tone_player is None:
        try:
            from audio_harmony import HarmonicPlayer
            _tone_player = HarmonicPlayer(volume=1.0)
            log.info("Audio: HarmonicPlayer loaded (polyphonic)")
        except Exception as e:
            try:
                from audio import TonePlayer
                _tone_player = TonePlayer(volume=1.0)
                log.info("Audio: TonePlayer loaded (fallback mono)")
            except Exception as e2:
                log.warning(f"Audio: no player available ({e}, {e2})")
                _tone_player = False
    return _tone_player if _tone_player else None

log = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Gimbal Mode & Queued Looks
# -----------------------------------------------------------------------------

class GimbalMode(enum.Enum):
    IDLE = "idle"
    INSTINCT = "instinct"
    COGNITIVE = "cognitive"
    MANUAL = "manual"


@dataclasses.dataclass
class QueuedLook:
    pan: int
    tilt: int
    speed: int = 100
    accel: int = 10
    created: float = dataclasses.field(default_factory=time.time)

    @property
    def stale(self) -> bool:
        return time.time() - self.created > CV_QUEUE_STALE_S


# -----------------------------------------------------------------------------
# Gimbal Arbiter
# -----------------------------------------------------------------------------

class GimbalArbiter:
    """Arbitrates gimbal control between CV instinct and soul commands.

    All hardware dependencies (serial port, lock, wake recorder, cv pipeline)
    are passed via constructor — no global state.
    """

    def __init__(self, cv_state, telemetry, ser, serial_lock,
                 wake_recorder=None, cv_pipeline=None):
        self._cv = cv_state
        self._telemetry = telemetry
        self._ser = ser
        self._serial_lock = serial_lock
        self._wake_recorder = wake_recorder
        self._cv_pipeline = cv_pipeline
        self._lock = threading.Lock()

        self._mode = GimbalMode.IDLE
        self._queue: deque[QueuedLook] = deque(maxlen=CV_QUEUE_MAX_DEPTH)
        self._last_target_time = 0.0
        self._no_target_since = 0.0
        self._manual_start = 0.0

        self._cmd_pan = 0.0
        self._cmd_tilt = 0.0

        self._smooth_cx = 0.5
        self._smooth_cy = 0.5

        self._last_track_cmd_time = 0.0
        self._track_cooldown_s = 0.15  # Was 0.4 — faster updates = smoother

        self._last_light_change = 0.0
        self._light_off_at = 0.0

        # Self-talk: background status babble during sustained face tracking
        self._self_talk_thread = None
        self._self_talk_active = False
        self._last_status_play = 0.0

        self._last_disengage_time = 0.0

        # Object detection audio tracking
        self._known_objects: set = set()  # Objects we've already announced
        self._last_object_sound = 0.0

    def _send(self, cmd: dict) -> bool:
        return send_tcode(self._ser, cmd, self._serial_lock)

    def _start_self_talk(self):
        """Start background self-talk babble during sustained face interaction."""
        if self._self_talk_active:
            return
        self._self_talk_active = True

        def _talk_loop():
            tp = _get_tone_player()
            if not tp or not hasattr(tp, 'play_status'):
                self._self_talk_active = False
                return
            while self._self_talk_active and self._mode == GimbalMode.INSTINCT:
                state = {
                    'battery_pct': 50, 'wanderlust': 0.5, 'social': 0.8,
                    'curiosity': 0.3, 'distance_m': 0,
                    'has_face': True, 'seconds_since_cat': None,
                }
                try:
                    import json as _json
                    with open('/opt/kombucha/state/body_state.json') as f:
                        bs = _json.load(f)
                    drives = bs.get('drives', {})
                    state['wanderlust'] = drives.get('wanderlust', 0.5)
                    state['social'] = drives.get('social', 0.5)
                    state['curiosity'] = drives.get('curiosity', 0.3)
                except Exception:
                    pass
                try:
                    tsnap = self._telemetry.snapshot()
                    from hardware import BATTERY_MIN_V, BATTERY_MAX_V, _clamp
                    bv = tsnap.get('battery_v', 0)
                    state['battery_pct'] = _clamp(
                        (bv - BATTERY_MIN_V) / (BATTERY_MAX_V - BATTERY_MIN_V) * 100, 0, 100)
                except Exception:
                    pass
                try:
                    tp.play_status(state)
                except Exception:
                    pass
                time.sleep(4)
            self._self_talk_active = False

        self._self_talk_thread = threading.Thread(target=_talk_loop, daemon=True)
        self._self_talk_thread.start()

    def _stop_self_talk(self):
        self._self_talk_active = False

    def _save_face_crops(self, dets):
        """Crop and save detected faces for recognition training."""
        if not self._cv_pipeline or not hasattr(self._cv_pipeline, '_queue'):
            return
        try:
            # Get the latest frame from the frame distributor
            # (wake_recorder has a ref to frame_dist)
            if not self._wake_recorder or not self._wake_recorder._frame_dist:
                return
            ret, frame, _ = self._wake_recorder._frame_dist.get_latest_frame()
            if not ret or frame is None:
                return

            face_dir = Path("/opt/kombucha/media/faces/unknown")
            face_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")

            for i, det in enumerate(dets):
                if det.get("class_name") != "person" and det.get("class_id") != 0:
                    continue
                x, y, w, h = det["x"], det["y"], det["w"], det["h"]
                # Expand crop region by 30% for context
                pad_x = int(w * 0.3)
                pad_y = int(h * 0.3)
                x1 = max(0, x - pad_x)
                y1 = max(0, y - pad_y)
                x2 = min(frame.shape[1], x + w + pad_x)
                y2 = min(frame.shape[0], y + h + pad_y)
                crop = frame[y1:y2, x1:x2]
                if crop.size == 0:
                    continue
                fname = f"face_{ts}_{i:02d}.jpg"
                cv2.imwrite(str(face_dir / fname), crop,
                            [cv2.IMWRITE_JPEG_QUALITY, 90])
                log.info(f"Face crop saved: {fname} ({w}x{h})")
        except Exception as e:
            log.warning(f"Face crop failed: {e}")

    @property
    def mode(self) -> GimbalMode:
        with self._lock:
            return self._mode

    def request_look(self, pan: int, tilt: int,
                     speed: int = 100, accel: int = 10) -> dict:
        """Called by action handler for soul look commands."""
        with self._lock:
            if self._mode == GimbalMode.MANUAL:
                cmd = validate_tcode(133, {
                    "X": pan, "Y": tilt, "SPD": speed, "ACC": accel
                })
                if cmd:
                    self._send(cmd)
                    self._cmd_pan = float(pan)
                    self._cmd_tilt = float(tilt)
                return {"result": "ok", "mode": "manual"}

            if self._mode == GimbalMode.INSTINCT:
                look = QueuedLook(pan, tilt, speed, accel)
                self._queue.append(look)
                return {
                    "result": "queued",
                    "queue_depth": len(self._queue),
                    "reason": "instinct_active",
                }

            self._mode = GimbalMode.COGNITIVE
            cmd = validate_tcode(133, {
                "X": pan, "Y": tilt, "SPD": speed, "ACC": accel
            })
            if cmd:
                self._send(cmd)
                self._cmd_pan = float(pan)
                self._cmd_tilt = float(tilt)
            return {"result": "ok", "mode": self._mode.value}

    def set_mode(self, mode_str: str) -> dict:
        """Set mode from API."""
        with self._lock:
            if mode_str == "manual":
                self._mode = GimbalMode.MANUAL
                self._manual_start = time.time()
                return {"result": "ok", "mode": "manual"}
            elif mode_str == "off":
                self._mode = GimbalMode.IDLE
                self._queue.clear()
                return {"result": "ok", "mode": "idle"}
            elif mode_str == "tracking":
                self._mode = GimbalMode.IDLE
                return {"result": "ok", "mode": "tracking"}
            return {"result": "error", "reason": f"unknown mode: {mode_str}"}

    def tick(self) -> Optional[dict]:
        """Called by CV pipeline after each processed frame."""
        with self._lock:
            if self._mode == GimbalMode.MANUAL:
                if time.time() - self._manual_start > CV_MANUAL_TIMEOUT_S:
                    self._mode = GimbalMode.IDLE
                    log.info("Manual mode timed out, returning to idle")
                return None

            # Turn off flash light if scheduled
            if self._light_off_at > 0 and time.time() > self._light_off_at:
                light_cmd = validate_tcode(132, {"IO4": 0, "IO5": 0})
                if light_cmd:
                    self._send(light_cmd)
                self._light_off_at = 0.0

            cv_snap = self._cv.snapshot()
            has_face = cv_snap["face_count"] > 0
            has_motion = cv_snap["motion_detected"]
            target = cv_snap.get("current_target")

            if has_face and target:
                self._last_target_time = time.time()
                self._no_target_since = 0.0

                if self._mode in (GimbalMode.IDLE, GimbalMode.COGNITIVE):
                    self._mode = GimbalMode.INSTINCT
                    log.info("Instinct engaged: face detected")
                    # INSTANT detect trill + name flirtation
                    tp = _get_tone_player()
                    if tp:
                        try:
                            # Get face size for flirtation intensity
                            face_pct = 0.1
                            if target:
                                face_pct = (target.get("w", 50) * target.get("h", 50)) / (CAPTURE_W * CAPTURE_H)
                            from audio_harmony import render_face_detect
                            import struct as _struct
                            samples = render_face_detect(face_pct)
                            if samples:
                                import tempfile, wave as _wave
                                with tempfile.NamedTemporaryFile(suffix='.wav', delete=False, dir='/tmp') as f:
                                    tmp = f.name
                                clamped = [max(-1.0, min(1.0, s)) for s in samples]
                                int_s = [int(s * 32767) for s in clamped]
                                data = _struct.pack('<%dh' % len(int_s), *int_s)
                                with _wave.open(tmp, 'w') as w:
                                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
                                    w.writeframes(data)
                                import subprocess
                                subprocess.Popen(['aplay', '-D', 'plughw:3,0', '-q', tmp],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception as e:
                            log.warning(f"Face detect sound failed: {e}")
                            # Fallback to simple greeting
                            try:
                                tp.play_mood("greeting")
                            except Exception:
                                pass
                    # Start self-talk babble (status phrases every 4s)
                    self._start_self_talk()
                    # Crop and save face for recognition training
                    if self._cv_pipeline:
                        dets = self._cv_pipeline.get_detections()
                        threading.Thread(
                            target=self._save_face_crops, args=(dets,),
                            daemon=True).start()
                    if self._wake_recorder:
                        dets = self._cv_pipeline.get_detections() if self._cv_pipeline else []
                        self._wake_recorder.engage("face", dets)
                    now = time.time()
                    if now - self._last_light_change > 3.0:
                        light_cmd = validate_tcode(132, {"IO4": 0, "IO5": 25})  # 10% brightness
                        if light_cmd:
                            self._send(light_cmd)
                        self._last_light_change = now
                        self._light_off_at = now + 0.5
                    if now - self._last_disengage_time > 10.0:
                        cx = target["cx"]
                        cy = target["cy"]
                        side = "left" if cx < 0.4 else "right" if cx > 0.6 else "center"
                        height = "above" if cy < 0.4 else "below" if cy > 0.6 else "level"
                        for i, line in enumerate([
                            "FACE DETECTED",
                            f"PAN:{int(self._cmd_pan):>4} TILT:{int(self._cmd_tilt):>3}",
                            f"POS: {side} {height}",
                            "TRACKING...",
                        ]):
                            cmd = validate_tcode(3, {"lineNum": i, "Text": line[:20]})
                            if cmd:
                                self._send(cmd)

                if self._mode == GimbalMode.INSTINCT:
                    if self._wake_recorder:
                        dets = self._cv_pipeline.get_detections() if self._cv_pipeline else []
                        self._wake_recorder.update_detections(dets)
                    # Pulse light while tracking face (1s on, 2s off cycle)
                    now_t = time.time()
                    if has_face and now_t - self._last_light_change > 3.0:
                        light_cmd = validate_tcode(132, {"IO4": 0, "IO5": 25})  # 10% brightness
                        if light_cmd:
                            self._send(light_cmd)
                        self._last_light_change = now_t
                        self._light_off_at = now_t + 1.0
                    return self._track_target(target)

            elif has_motion and not has_face:
                self._last_target_time = time.time()
                self._no_target_since = 0.0

                if self._mode in (GimbalMode.IDLE, GimbalMode.COGNITIVE):
                    self._mode = GimbalMode.INSTINCT
                    # Motion warble — scales with motion size
                    tp = _get_tone_player()
                    if tp:
                        try:
                            # Get motion area as fraction of frame
                            regions = cv_snap.get("motion_regions", [])
                            total_area = sum(r[2] * r[3] for r in regions) if regions else 500
                            motion_pct = total_area / (CAPTURE_W * CAPTURE_H)
                            from audio_harmony import render_motion_detect
                            import struct as _struct
                            samples = render_motion_detect(motion_pct)
                            if samples:
                                import tempfile, wave as _wave
                                with tempfile.NamedTemporaryFile(suffix='.wav', delete=False, dir='/tmp') as f:
                                    tmp = f.name
                                clamped = [max(-1.0, min(1.0, s)) for s in samples]
                                int_s = [int(s * 32767) for s in clamped]
                                data = _struct.pack('<%dh' % len(int_s), *int_s)
                                with _wave.open(tmp, 'w') as w:
                                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
                                    w.writeframes(data)
                                import subprocess
                                subprocess.Popen(['aplay', '-D', 'plughw:3,0', '-q', tmp],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            try:
                                tp.play_mood("curious")
                            except Exception:
                                pass
                    if self._wake_recorder:
                        dets = self._cv_pipeline.get_detections() if self._cv_pipeline else []
                        self._wake_recorder.engage("motion", dets)

                if self._mode == GimbalMode.INSTINCT:
                    pass  # Hold position for motion

            else:
                if self._no_target_since == 0.0 and self._last_target_time > 0:
                    self._no_target_since = time.time()

                if (self._mode == GimbalMode.INSTINCT
                        and self._no_target_since > 0
                        and time.time() - self._no_target_since > CV_HYSTERESIS_S):
                    self._mode = GimbalMode.IDLE
                    self._last_disengage_time = time.time()
                    log.info("Instinct released: no targets")
                    # Stop self-talk
                    self._stop_self_talk()
                    # Goodbye chord
                    tp = _get_tone_player()
                    if tp:
                        try:
                            tp.play_mood("goodbye")
                        except Exception:
                            pass
                    if self._wake_recorder:
                        self._wake_recorder.disengage()
                    center_cmd = validate_tcode(133, {"X": 0, "Y": 0, "SPD": 80, "ACC": 10})
                    if center_cmd:
                        self._send(center_cmd)
                    self._cmd_pan = 0.0
                    self._cmd_tilt = 0.0
                    self._smooth_cx = 0.5
                    self._smooth_cy = 0.5
                    self._drain_one()

            # Object detection audio — announce new objects by spelling their name
            now_obj = time.time()
            if self._cv_pipeline and now_obj - self._last_object_sound > 5.0:
                try:
                    dets = self._cv_pipeline.get_detections()
                    for det in dets:
                        name = det.get("class_name", "")
                        conf = det.get("confidence", 0)
                        if not name or name == "person" or conf < 0.4:
                            continue
                        if name not in self._known_objects:
                            self._known_objects.add(name)
                            self._last_object_sound = now_obj
                            log.info(f"New object detected: {name} ({conf:.0%})")
                            # Play in background thread
                            def _play_obj(n=name, c=conf):
                                try:
                                    from audio_harmony import render_object_detect
                                    import struct as _s, tempfile, wave as _w, subprocess as _sp
                                    samples = render_object_detect(n, c)
                                    if samples:
                                        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False, dir='/tmp') as f:
                                            tmp = f.name
                                        clamped = [max(-1.0, min(1.0, s)) for s in samples]
                                        int_s = [int(s * 32767) for s in clamped]
                                        data = _s.pack('<%dh' % len(int_s), *int_s)
                                        with _w.open(tmp, 'w') as w:
                                            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
                                            w.writeframes(data)
                                        _sp.Popen(['aplay', '-D', 'plughw:3,0', '-q', tmp],
                                            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                                except Exception:
                                    pass
                            threading.Thread(target=_play_obj, daemon=True).start()
                            break  # One announcement per tick cycle
                except Exception:
                    pass
                # Reset known objects every 5 minutes so re-appearances get announced
                if len(self._known_objects) > 20:
                    self._known_objects.clear()

            return None

    def _track_target(self, target: dict) -> Optional[dict]:
        """Proportional gimbal steering toward smoothed target center."""
        now = time.time()
        if now - self._last_track_cmd_time < self._track_cooldown_s:
            return None

        raw_cx = target["cx"]
        raw_cy = target["cy"]

        alpha = 0.3  # Was CV_SMOOTHING (0.5) — lower = smoother, less jitter
        self._smooth_cx = alpha * raw_cx + (1 - alpha) * self._smooth_cx
        self._smooth_cy = alpha * raw_cy + (1 - alpha) * self._smooth_cy

        error_x = self._smooth_cx - 0.5
        error_y = self._smooth_cy - 0.5

        error_px_x = abs(error_x) * CAPTURE_W
        error_px_y = abs(error_y) * CAPTURE_H
        if error_px_x < CV_DEAD_ZONE_PX and error_px_y < CV_DEAD_ZONE_PX:
            return None

        current_pan = self._cmd_pan
        current_tilt = self._cmd_tilt

        # Bigger steps + higher gain = fewer commands to reach target
        max_step = 15.0  # Was CV_MAX_STEP_DEG (6.0) — larger steps = fewer jerks
        pan_adj = _clamp(error_x * 120.0, -max_step, max_step)   # Was CV_KP_PAN (80)
        tilt_adj = _clamp(-error_y * 60.0, -max_step, max_step)  # Was CV_KP_TILT (40)

        new_pan = int(_clamp(current_pan + pan_adj, -180, 180))
        new_tilt = int(_clamp(current_tilt + tilt_adj, -30, 90))

        cmd = validate_tcode(133, {
            "X": new_pan, "Y": new_tilt, "SPD": 150, "ACC": 30  # Was 70/12 — faster servo
        })
        if cmd:
            self._send(cmd)
            self._cmd_pan = float(new_pan)
            self._cmd_tilt = float(new_tilt)
            self._last_track_cmd_time = time.time()
            log.info(
                f"Tracking → pan={new_pan} tilt={new_tilt} "
                f"(face@{raw_cx:.2f},{raw_cy:.2f} err={error_x:.2f},{error_y:.2f})"
            )
            return cmd
        return None

    def _drain_one(self):
        """Execute one non-stale queued look command."""
        while self._queue:
            look = self._queue.popleft()
            if not look.stale:
                cmd = validate_tcode(133, {
                    "X": look.pan, "Y": look.tilt,
                    "SPD": look.speed, "ACC": look.accel,
                })
                if cmd:
                    self._send(cmd)
                self._mode = GimbalMode.COGNITIVE
                return

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "mode": self._mode.value,
                "queue_depth": len(self._queue),
                "last_target_age_s": (
                    round(time.time() - self._last_target_time, 2)
                    if self._last_target_time > 0 else None
                ),
            }


# -----------------------------------------------------------------------------
# Heartbeat — periodic idle gestures
# -----------------------------------------------------------------------------

class Heartbeat(threading.Thread):
    """Periodic idle fidgets — gimbal + light animations."""

    IDLE_GESTURES = [
        ("curious_tilt", [
            (15, 0, 60, 0, 0.3), (-15, 0, 60, 0, 0.3), (0, 0, 60, 0, 0.2),
        ]),
        ("look_up", [
            (0, 30, 40, 0, 0.5), (0, 0, 40, 0, 0.3),
        ]),
        ("soft_pulse", [
            (0, 0, 0, 80, 0.2), (0, 0, 0, 0, 0.3),
        ]),
        ("glance_left", [
            (-25, 5, 50, 0, 0.4), (0, 0, 50, 0, 0.3),
        ]),
        ("glance_right", [
            (25, -5, 50, 0, 0.4), (0, 0, 50, 0, 0.3),
        ]),
    ]

    ANNOYED_GESTURES = [
        ("double_dip", [
            (0, -15, 100, 0, 0.15), (0, 0, 100, 0, 0.15),
            (0, -15, 100, 0, 0.15), (0, 0, 100, 0, 0.2),
        ]),
        ("head_shake", [
            (-20, 0, 120, 0, 0.12), (20, 0, 120, 0, 0.12),
            (-20, 0, 120, 0, 0.12), (0, 0, 80, 0, 0.2),
        ]),
        ("double_flash", [
            (0, 0, 0, 200, 0.1), (0, 0, 0, 0, 0.1),
            (0, 0, 0, 200, 0.1), (0, 0, 0, 0, 0.2),
        ]),
    ]

    EXASPERATED_GESTURES = [
        ("triple_dip", [
            (0, -20, 150, 0, 0.1), (0, 0, 150, 0, 0.1),
            (0, -20, 150, 0, 0.1), (0, 0, 150, 0, 0.1),
            (0, -20, 150, 0, 0.1), (0, 0, 150, 0, 0.15),
        ]),
        ("eye_roll", [
            (-40, 0, 80, 0, 0.2), (-40, 20, 80, 0, 0.2),
            (40, 20, 80, 0, 0.2), (40, 0, 80, 0, 0.2),
            (0, 0, 80, 0, 0.3),
        ]),
        ("the_sigh", [
            (0, 40, 30, 0, 0.8), (0, 40, 0, 100, 0.3),
            (0, 0, 40, 0, 0.4),
        ]),
        ("triple_flash", [
            (0, 0, 0, 255, 0.08), (0, 0, 0, 0, 0.08),
            (0, 0, 0, 255, 0.08), (0, 0, 0, 0, 0.08),
            (0, 0, 0, 255, 0.08), (0, 0, 0, 0, 0.15),
        ]),
    ]

    def __init__(self, gimbal_arbiter: GimbalArbiter, ser, serial_lock):
        super().__init__(daemon=True)
        self._gimbal_arbiter = gimbal_arbiter
        self._ser = ser
        self._serial_lock = serial_lock
        self._running = False
        self._frustration = 0
        self._last_gesture = 0.0
        self._lock = threading.Lock()

    def _send(self, cmd: dict) -> bool:
        return send_tcode(self._ser, cmd, self._serial_lock)

    @property
    def frustration(self) -> int:
        with self._lock:
            return self._frustration

    @frustration.setter
    def frustration(self, val: int):
        with self._lock:
            self._frustration = max(0, min(5, val))

    def run(self):
        self._running = True
        log.info("Heartbeat started")
        while self._running:
            time.sleep(1.0)
            now = time.time()
            if now - self._last_gesture < HEARTBEAT_INTERVAL_S:
                continue

            if self._gimbal_arbiter and self._gimbal_arbiter.mode != GimbalMode.IDLE:
                continue

            frust = self.frustration
            if frust <= 1:
                gestures = self.IDLE_GESTURES
            elif frust <= 3:
                gestures = self.ANNOYED_GESTURES
            else:
                gestures = self.EXASPERATED_GESTURES

            name, sequence = random.choice(gestures)
            log.info(f"Heartbeat: {name} (frustration={frust})")

            for pan, tilt, spd, light, delay in sequence:
                if not self._running:
                    break
                if self._gimbal_arbiter and self._gimbal_arbiter.mode == GimbalMode.INSTINCT:
                    break
                if spd > 0:
                    cmd = validate_tcode(133, {"X": pan, "Y": tilt, "SPD": spd, "ACC": 20})
                    if cmd:
                        self._send(cmd)
                if light > 0 or (light == 0 and spd == 0):
                    lcmd = validate_tcode(132, {"IO4": 0, "IO5": light})
                    if lcmd:
                        self._send(lcmd)
                time.sleep(delay)

            # Return to center
            cmd = validate_tcode(133, {"X": 0, "Y": 0, "SPD": 60, "ACC": 10})
            if cmd:
                self._send(cmd)
            lcmd = validate_tcode(132, {"IO4": 0, "IO5": 0})
            if lcmd:
                self._send(lcmd)

            self._last_gesture = now

    def stop(self):
        self._running = False
