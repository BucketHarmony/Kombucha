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

from hardware import (
    validate_tcode, send_tcode, _clamp,
    CAPTURE_W, CAPTURE_H,
    CV_QUEUE_MAX_DEPTH, CV_QUEUE_STALE_S, CV_DEAD_ZONE_PX,
    CV_KP_PAN, CV_KP_TILT, CV_MAX_STEP_DEG, CV_SMOOTHING,
    CV_HYSTERESIS_S, CV_MANUAL_TIMEOUT_S,
    HEARTBEAT_INTERVAL_S, JPEG_QUALITY,
)

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
        self._tone_player = None  # late-bound from bridge.py
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
        self._track_cooldown_s = 0.4

        self._last_light_change = 0.0
        self._light_off_at = 0.0

        self._last_disengage_time = 0.0
        self._last_sound_time = 0.0  # cooldown for instinct sounds

    def _send(self, cmd: dict) -> bool:
        return send_tcode(self._ser, cmd, self._serial_lock)

    def _play_instinct_sound(self, mood: str):
        """Play an emotional tone through the instinct layer. 5s cooldown."""
        if self._tone_player is None:
            return
        now = time.time()
        if now - self._last_sound_time < 5.0:
            return
        self._last_sound_time = now
        try:
            self._tone_player.play_mood(mood)
            log.info(f"Instinct sound: {mood}")
        except Exception as e:
            log.warning(f"Instinct sound failed: {e}")

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
                    if self._wake_recorder:
                        dets = self._cv_pipeline.get_detections() if self._cv_pipeline else []
                        self._wake_recorder.engage("face", dets)
                    # Emotional sound on face engage
                    now = time.time()
                    if now - self._last_disengage_time > 30.0:
                        self._play_instinct_sound("greeting_known")
                    elif now - self._last_disengage_time > 10.0:
                        self._play_instinct_sound("greeting_unknown")
                    else:
                        self._play_instinct_sound("curious")
                    if now - self._last_light_change > 3.0:
                        light_cmd = validate_tcode(132, {"IO4": 0, "IO5": 255})
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
                        light_cmd = validate_tcode(132, {"IO4": 0, "IO5": 180})
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
                    self._play_instinct_sound("alert")
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
                    self._play_instinct_sound("goodbye")
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

            return None

    def _track_target(self, target: dict) -> Optional[dict]:
        """Proportional gimbal steering toward smoothed target center."""
        now = time.time()
        if now - self._last_track_cmd_time < self._track_cooldown_s:
            return None

        raw_cx = target["cx"]
        raw_cy = target["cy"]

        alpha = CV_SMOOTHING
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

        pan_adj = _clamp(error_x * CV_KP_PAN, -CV_MAX_STEP_DEG, CV_MAX_STEP_DEG)
        tilt_adj = _clamp(-error_y * CV_KP_TILT, -CV_MAX_STEP_DEG, CV_MAX_STEP_DEG)

        new_pan = int(_clamp(current_pan + pan_adj, -180, 180))
        new_tilt = int(_clamp(current_tilt + tilt_adj, -30, 90))

        cmd = validate_tcode(133, {
            "X": new_pan, "Y": new_tilt, "SPD": 70, "ACC": 12
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
