#!/usr/bin/env python3
"""
bridge.py - HTTP bridge for Kombucha rover hardware.

Composition root: creates all objects, wires dependencies, exposes REST API.
"""

import csv
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import cv2
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from hardware import (
    TelemetryState, TelemetryReader,
    init_camera, init_serial,
    validate_tcode, send_tcode, translate_action,
    is_plugged_in, read_cpu_temp, compute_sense, _clamp,
    VIDEO_DIR, WAKE_DIR, CAPTURE_W, CAPTURE_H, JPEG_QUALITY,
    DRIVE_MAX_DURATION_MS, STUCK_TIMEOUT_S, STUCK_ODOM_THRESHOLD,
    TICKS_PER_METER, BATTERY_CHARGING_V,
)
from perception import FrameDistributor, CVState, CVPipeline
from gimbal import GimbalArbiter, GimbalMode, Heartbeat
from recorder import VideoRecorder, WakeRecorder
from overlay import OverlayRenderer
from audio_device import find_playback_device, find_capture_device
from imu_audio import IMUAudioReactor
from audio_monitor import AudioMonitor
from stereo_sonar import StereoSonar
from timelapse import TimeLapseRecorder
from audio import TonePlayer
from mic import AudioListener

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("kombucha_body")

# -----------------------------------------------------------------------------
# FastAPI App
# -----------------------------------------------------------------------------

app = FastAPI(title="Kombucha Body", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
start_time = time.time()

# -----------------------------------------------------------------------------
# Global State
# -----------------------------------------------------------------------------

camera: Optional[cv2.VideoCapture] = None
serial_port = None
_serial_lock = threading.Lock()

state = {
    "pan_position": 0,
    "tilt_position": 0,
    "battery_v": None,
    "cpu_temp_c": None,
}

telemetry_state: Optional[TelemetryState] = None
telemetry_reader: Optional[TelemetryReader] = None
frame_distributor: Optional[FrameDistributor] = None
video_recorder: Optional[VideoRecorder] = None
cv_state: Optional[CVState] = None
cv_pipeline: Optional[CVPipeline] = None
gimbal_arbiter: Optional[GimbalArbiter] = None
wake_recorder: Optional[WakeRecorder] = None
heartbeat: Optional[Heartbeat] = None

_plugged_in_override: Optional[bool] = None
tone_player: Optional[TonePlayer] = None
audio_listener: Optional[AudioListener] = None


# Detection logger
detection_logger = None
audio_monitor = None
sonar = None
timelapse = None

LOG_DIR = Path(os.environ.get("KOMBUCHA_LOG_DIR",
               Path.home() / "kombucha" / "logs"))


class DetectionLogger(threading.Thread):
    """Background thread that polls CV detections and writes a persistent log.

    Writes CSV: timestamp, class_name, confidence, duration_s, bbox_area
    Aggregates per-class presence into 10-second buckets to keep file size sane.
    Also maintains an in-memory summary for the /detections/summary endpoint.
    """

    def __init__(self, cv_pipeline_ref, cv_state_ref, log_dir: Path,
                 poll_interval: float = 2.0):
        super().__init__(daemon=True)
        self._cv_pipe = cv_pipeline_ref
        self._cv_state = cv_state_ref
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._poll_interval = poll_interval
        self._running = False
        self._lock = threading.Lock()

        # In-memory session summary: {class_name: {first_seen, last_seen, total_s, count}}
        self._summary: dict[str, dict] = {}
        # Current presence tracking: {class_name: appear_time}
        self._active: dict[str, float] = {}

    def _log_path(self) -> Path:
        """One CSV per day."""
        return self._log_dir / f"detections_{datetime.now().strftime('%Y%m%d')}.csv"

    def _ensure_header(self, path: Path):
        if not path.exists():
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["timestamp", "class_name", "confidence",
                            "event", "duration_s"])

    def run(self):
        self._running = True
        log.info("Detection logger started")
        while self._running:
            time.sleep(self._poll_interval)
            if not self._cv_pipe:
                continue

            now = time.time()
            dets = self._cv_pipe.get_detections()
            seen_now = set()

            for d in dets:
                name = d.get("class_name", "unknown")
                conf = d.get("confidence", 0)
                seen_now.add(name)

                with self._lock:
                    if name not in self._active:
                        # New appearance
                        self._active[name] = now
                        self._write_event(now, name, conf, "appear")
                        if name not in self._summary:
                            self._summary[name] = {
                                "first_seen": now, "last_seen": now,
                                "total_s": 0.0, "appearances": 0,
                            }
                        self._summary[name]["appearances"] += 1

            # Check for disappearances
            with self._lock:
                disappeared = [n for n in self._active if n not in seen_now]
                for name in disappeared:
                    appear_time = self._active.pop(name)
                    duration = now - appear_time
                    self._write_event(now, name, 0, "disappear", duration)
                    if name in self._summary:
                        self._summary[name]["total_s"] += duration
                        self._summary[name]["last_seen"] = now

                # Update last_seen for still-active
                for name in self._active:
                    if name in self._summary:
                        self._summary[name]["last_seen"] = now

    def _write_event(self, ts: float, class_name: str, confidence: float,
                     event: str, duration: float = 0.0):
        path = self._log_path()
        self._ensure_header(path)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                class_name, round(confidence, 3), event, round(duration, 1),
            ])

    def get_summary(self) -> dict:
        """Return session detection summary."""
        now = time.time()
        with self._lock:
            result = {}
            for name, s in self._summary.items():
                total = s["total_s"]
                # Add ongoing duration for currently active
                if name in self._active:
                    total += now - self._active[name]
                result[name] = {
                    "first_seen": datetime.fromtimestamp(
                        s["first_seen"]).strftime("%H:%M:%S"),
                    "last_seen": datetime.fromtimestamp(
                        s["last_seen"]).strftime("%H:%M:%S"),
                    "total_seconds": round(total, 1),
                    "total_minutes": round(total / 60, 1),
                    "appearances": s["appearances"],
                    "currently_visible": name in self._active,
                }
            return dict(sorted(result.items(),
                               key=lambda x: -x[1]["total_seconds"]))

    def get_recent(self, minutes: int = 60) -> list[dict]:
        """Read recent events from today's CSV."""
        path = self._log_path()
        if not path.exists():
            return []
        cutoff = time.time() - minutes * 60
        events = []
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = datetime.strptime(
                        row["timestamp"], "%Y-%m-%d %H:%M:%S").timestamp()
                    if ts >= cutoff:
                        events.append(row)
                except (ValueError, KeyError):
                    continue
        return events

    def stop(self):
        self._running = False
        # Flush active items
        now = time.time()
        with self._lock:
            for name, appear_time in self._active.items():
                duration = now - appear_time
                self._write_event(now, name, 0, "disappear", duration)
                if name in self._summary:
                    self._summary[name]["total_s"] += duration
            self._active.clear()


class CameraWatchdog(threading.Thread):
    """Background thread that polls for USB camera re-appearance.

    When the camera is absent at startup, this thread periodically calls
    init_camera().  If it succeeds, it wires up the full camera pipeline
    (FrameDistributor, CVPipeline, VideoRecorder, etc.) so the bridge
    recovers without a restart.
    """

    def __init__(self, interval: float = 30.0):
        super().__init__(daemon=True)
        self._interval = interval
        self._running = False

    def run(self):
        global camera, frame_distributor, cv_state, cv_pipeline
        global gimbal_arbiter, video_recorder, wake_recorder
        global heartbeat, detection_logger

        self._running = True
        log.info(f"Camera watchdog started (polling every {self._interval}s)")

        while self._running:
            time.sleep(self._interval)
            if not self._running:
                break

            # Already recovered?
            if camera is not None:
                log.info("Camera watchdog: camera already present, stopping")
                break

            log.info("Camera watchdog: attempting camera init...")
            cam = init_camera()
            if cam is None:
                log.info("Camera watchdog: no camera found, will retry")
                continue

            # Camera found — wire up the full pipeline
            log.info("Camera watchdog: CAMERA FOUND — initializing pipeline")
            camera = cam

            try:
                # Frame distributor
                frame_distributor = FrameDistributor(camera)
                frame_distributor.start()
                log.info("Camera watchdog: frame distributor started")

                # CV pipeline
                cv_state_new = CVState()
                cv_queue = frame_distributor.subscribe(maxsize=3)

                if telemetry_state:
                    gimbal_arbiter_new = GimbalArbiter(
                        cv_state_new, telemetry_state,
                        serial_port, _serial_lock)
                    gimbal_arbiter = gimbal_arbiter_new
                    log.info("Camera watchdog: gimbal arbiter initialized")
                else:
                    gimbal_arbiter_new = None

                cv_pipeline_new = CVPipeline(
                    cv_queue, cv_state_new,
                    gimbal_arbiter=gimbal_arbiter_new)
                cv_pipeline_new.start()
                cv_state = cv_state_new
                cv_pipeline = cv_pipeline_new
                log.info("Camera watchdog: CV pipeline started")

                # Video recorder
                state_path = Path("/opt/kombucha/state/body_state.json")
                goals_path = Path("/opt/kombucha/goals.md")
                hud = OverlayRenderer(
                    cv_pipeline=cv_pipeline,
                    cv_state=cv_state,
                    telemetry=telemetry_state,
                    gimbal_arbiter=gimbal_arbiter,
                    state_file=state_path if state_path.exists() else None,
                    goals_file=goals_path if goals_path.exists() else None,
                )
                VIDEO_DIR.mkdir(parents=True, exist_ok=True)
                video_queue = frame_distributor.subscribe(maxsize=5)
                video_recorder = VideoRecorder(
                    video_queue, VIDEO_DIR,
                    cv_pipeline_ref=cv_pipeline, overlay=hud)
                video_recorder.start()
                try:
                    video_recorder.start_session()
                    log.info("Camera watchdog: video recorder + session started")
                except Exception:
                    log.info("Camera watchdog: video recorder started (no session)")

                # Wake recorder
                WAKE_DIR.mkdir(parents=True, exist_ok=True)
                wake_recorder = WakeRecorder(
                    WAKE_DIR, frame_distributor, cv_pipeline)
                log.info("Camera watchdog: wake recorder initialized")

                # Wire late-binding refs
                if gimbal_arbiter:
                    gimbal_arbiter._wake_recorder = wake_recorder
                    gimbal_arbiter._cv_pipeline = cv_pipeline
                    if tone_player:
                        gimbal_arbiter._tone_player = tone_player

                # Detection logger
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                detection_logger = DetectionLogger(
                    cv_pipeline, cv_state, LOG_DIR)
                detection_logger.start()
                log.info("Camera watchdog: detection logger started")

                # Heartbeat
                if gimbal_arbiter:
                    heartbeat = Heartbeat(
                        gimbal_arbiter, serial_port, _serial_lock)
                    heartbeat.start()
                    log.info("Camera watchdog: heartbeat started")

                log.info("Camera watchdog: FULL PIPELINE RECOVERED")

            except Exception as e:
                log.error(f"Camera watchdog: pipeline init failed: {e}")
                # Release the camera so we can retry cleanly
                try:
                    camera.release()
                except Exception:
                    pass
                camera = None
                frame_distributor = None
                cv_state = None
                cv_pipeline = None
                continue

            break  # Success — stop watching

        log.info("Camera watchdog stopped")

    def stop(self):
        self._running = False


_camera_watchdog: Optional[CameraWatchdog] = None


def _is_plugged_in() -> bool:
    """Module-level plugged-in check using current override and telemetry."""
    return is_plugged_in(_plugged_in_override, telemetry_state)


def _send(cmd: dict) -> bool:
    """Send T-code with side effects (telemetry marking, motion suppression)."""
    ok = send_tcode(serial_port, cmd, _serial_lock)
    if ok:
        if telemetry_state:
            if cmd.get("T") == 1:
                telemetry_state.mark_drive_start()
            elif cmd.get("T") == 0:
                telemetry_state.mark_drive_stop()
        if cmd.get("T") == 1 and cv_pipeline:
            cv_pipeline.suppress_motion()
    return ok


# -----------------------------------------------------------------------------
# Pydantic Models
# -----------------------------------------------------------------------------

class ActionModel(BaseModel):
    type: str
    left: Optional[float] = None
    right: Optional[float] = None
    duration_ms: Optional[int] = None
    pan: Optional[int] = None
    tilt: Optional[int] = None
    speed: Optional[int] = None
    accel: Optional[int] = None
    lines: Optional[list[str]] = None
    line: Optional[int] = None
    text: Optional[str] = None
    base: Optional[int] = None
    head: Optional[int] = None
    mode: Optional[str] = None
    mood: Optional[str] = None
    sequence: Optional[list[dict]] = None


class SessionStartModel(BaseModel):
    session_name: Optional[str] = None


class TickStartModel(BaseModel):
    tick: int


class DriveModel(BaseModel):
    left: float = 0.0
    right: float = 0.0
    duration_ms: int = 1000


class CVModeModel(BaseModel):
    mode: str


class PluggedModel(BaseModel):
    plugged_in: bool


class FrustrationModel(BaseModel):
    level: int


# -----------------------------------------------------------------------------
# API Endpoints
# -----------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "uptime_s": round(time.time() - start_time, 1),
    }


@app.get("/frame")
def get_frame(annotate: int = 0):
    """Get current camera frame as JPEG."""
    if frame_distributor is None:
        raise HTTPException(
            status_code=503, detail={"error": "Camera not available"}
        )

    ret, frame, fid = frame_distributor.get_fresh_frame(timeout_s=2.0)
    if not ret or frame is None:
        raise HTTPException(
            status_code=503, detail={"error": "Camera capture failed"}
        )

    if annotate and cv_state:
        snap = cv_state.snapshot()
        if cv_pipeline:
            colors = {"person": (0, 180, 255), "cat": (255, 100, 255),
                      "dog": (255, 180, 0), "chair": (100, 100, 255)}
            for det in cv_pipeline.get_detections():
                x, y, w, h = det["x"], det["y"], det["w"], det["h"]
                color = colors.get(det["class_name"], (100, 200, 100))
                cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
                label = f"{det['class_name']} {det['confidence']:.0%}"
                cv2.putText(frame, label, (x, y - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
        if snap.get("motion_detected"):
            cv2.putText(frame, "MOTION", (10, 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 100), 1)
        target = snap.get("current_target")
        if target:
            cx_px = int(target["cx"] * frame.shape[1])
            cy_px = int(target["cy"] * frame.shape[0])
            cv2.drawMarker(frame, (cx_px, cy_px), (0, 0, 255),
                           cv2.MARKER_CROSS, 20, 2)
        if gimbal_arbiter:
            arb = gimbal_arbiter.snapshot()
            n_dets = len(cv_pipeline.get_detections()) if cv_pipeline else 0
            info = f"{arb['mode']} | persons:{snap['face_count']} | objects:{n_dets} | q:{arb['queue_depth']}"
            cv2.putText(frame, info, (10, frame.shape[0] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)

    _, jpeg_buf = cv2.imencode(
        '.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
    )
    return Response(content=jpeg_buf.tobytes(), media_type="image/jpeg")


@app.get("/state")
def get_state():
    """Get current body state."""
    cpu_temp = read_cpu_temp()
    if cpu_temp is not None:
        state["cpu_temp_c"] = cpu_temp

    telem_snap = None
    if telemetry_state:
        telem_snap = telemetry_state.snapshot()
        state["battery_v"] = telem_snap["battery_v"] or state.get("battery_v")

    plugged = _is_plugged_in()

    result = {
        "pan": state["pan_position"],
        "tilt": state["tilt_position"],
        "battery_v": state.get("battery_v"),
        "cpu_temp_c": state.get("cpu_temp_c"),
        "recording": video_recorder.is_recording if video_recorder else False,
        "plugged_in": plugged,
        "wheels_locked": plugged,
        "telemetry": telem_snap,
    }

    if cv_state:
        cv_snap = cv_state.snapshot()
        result["cv_mode"] = (
            gimbal_arbiter.mode.value if gimbal_arbiter else "idle"
        )
        result["faces_detected"] = cv_snap["face_count"]
        result["motion_detected"] = cv_snap["motion_detected"]
        result["tracking_target"] = (
            cv_snap["current_target"]["type"]
            if cv_snap["current_target"] else None
        )
        result["command_queue_depth"] = (
            gimbal_arbiter.snapshot()["queue_depth"]
            if gimbal_arbiter else 0
        )

    return result


@app.get("/sense")
def get_sense():
    """Get interpreted body state computed from telemetry."""
    if telemetry_state is None:
        raise HTTPException(
            status_code=503, detail={"error": "Telemetry not initialized"}
        )
    snap = telemetry_state.snapshot()
    result = compute_sense(
        snap,
        plugged=_is_plugged_in(),
        cv_state=cv_state,
        gimbal_arbiter=gimbal_arbiter,
        wake_recorder=wake_recorder,
    )

    # Camera health — always report status, even when camera is absent
    if frame_distributor:
        result["camera_ok"] = frame_distributor.camera_ok
        with frame_distributor._lock:
            if frame_distributor._last_frame_time > 0:
                result["frame_age_s"] = round(time.time() - frame_distributor._last_frame_time, 1)
            else:
                result["frame_age_s"] = None
    else:
        result["camera_ok"] = False
        result["frame_age_s"] = None
        result["camera_status"] = "absent"
        result["camera_watchdog"] = (
            _camera_watchdog is not None and _camera_watchdog.is_alive()
        )

    # Append detection session summary (what's been seen and for how long)
    if detection_logger:
        result["detection_summary"] = detection_logger.get_summary()

    # Append audio levels
    if audio_listener:
        audio = audio_listener.snapshot()
        result["audio_rms"] = audio["rms"]
        result["audio_silence"] = audio["silence"]
        result["audio_events"] = audio["events"]

    return result


@app.post("/drive")
def post_drive(body: DriveModel):
    """Drive with telemetry feedback."""
    if telemetry_state is None:
        raise HTTPException(
            status_code=503, detail={"error": "Telemetry not initialized"}
        )

    if _is_plugged_in():
        return JSONResponse(content={
            "result": "blocked",
            "reason": "plugged_in",
        })

    duration_ms = min(body.duration_ms, DRIVE_MAX_DURATION_MS)
    duration_s = duration_ms / 1000.0

    before = telemetry_state.snapshot()

    cmd = validate_tcode(1, {"L": body.left, "R": body.right})
    if not cmd:
        raise HTTPException(
            status_code=400, detail={"error": "Invalid drive parameters"}
        )
    _send(cmd)

    speed_samples = []
    sample_interval = 0.1
    elapsed = 0.0
    while elapsed < duration_s:
        time.sleep(sample_interval)
        elapsed += sample_interval
        snap = telemetry_state.snapshot()
        speed_samples.append({
            "t": round(elapsed, 2),
            "wsl": snap["wheel_speed_l"],
            "wsr": snap["wheel_speed_r"],
        })

    _send({"T": 0})

    after = telemetry_state.snapshot()

    odom_delta_l = after["odl"] - before["odl"]
    odom_delta_r = after["odr"] - before["odr"]
    avg_odom = (abs(odom_delta_l) + abs(odom_delta_r)) / 2.0
    distance_m = round(avg_odom / TICKS_PER_METER, 4)

    stuck = (abs(odom_delta_l) < STUCK_ODOM_THRESHOLD
             and abs(odom_delta_r) < STUCK_ODOM_THRESHOLD
             and duration_s > STUCK_TIMEOUT_S)

    return {
        "odometry_delta": {"left": odom_delta_l, "right": odom_delta_r},
        "speed_samples": speed_samples,
        "stuck": stuck,
        "distance_estimate_m": distance_m,
    }


@app.post("/action")
def post_action(action: Union[ActionModel, list[ActionModel]]):
    """Execute action(s) on the rover."""
    actions = [action] if isinstance(action, ActionModel) else action

    for act in actions:
        act_dict = act.model_dump(exclude_none=True)
        action_type = act_dict.get("type", "")

        if action_type == "tracking" and gimbal_arbiter:
            mode = act_dict.get("mode", "tracking")
            return JSONResponse(content=gimbal_arbiter.set_mode(mode))

        if action_type == "sound":
            if tone_player is None:
                raise HTTPException(status_code=503, detail={"error": "Audio not initialized"})
            # Suppress mic while playing tones (self-sound suppression)
            if audio_listener:
                audio_listener.suppress(2.0)
            # Set tick context for file naming
            tick = act_dict.get("tick")
            if tick is not None:
                tone_player.set_tick(int(tick))
            mood = act_dict.get("mood")
            seq = act_dict.get("sequence")
            if seq:
                meta = tone_player.play_sequence(seq, label=act_dict.get("label", "custom"))
                return {"result": "ok", "played": "custom_sequence", "audio": meta}
            elif mood:
                meta = tone_player.play_mood(mood)
                return {"result": "ok", "played": mood, "audio": meta}
            else:
                raise HTTPException(status_code=400, detail={"error": "sound action requires 'mood' or 'sequence'"})

        valid_types = [
            "drive", "stop", "look", "display", "oled",
            "lights", "light", "tracking",
        ]
        if action_type not in valid_types:
            raise HTTPException(
                status_code=400,
                detail={"error": f"Invalid action type: {action_type}"},
            )

        if action_type == "drive" and _is_plugged_in():
            return JSONResponse(content={
                "result": "blocked",
                "reason": "plugged_in",
            })

        if action_type == "look" and gimbal_arbiter:
            result = gimbal_arbiter.request_look(
                pan=int(act_dict.get("pan", 0)),
                tilt=int(act_dict.get("tilt", 0)),
                speed=int(act_dict.get("speed", 100)),
                accel=int(act_dict.get("accel", 10)),
            )
            state["pan_position"] = int(act_dict.get("pan", 0))
            state["tilt_position"] = int(act_dict.get("tilt", 0))
            return JSONResponse(content=result)

        tcodes = translate_action(act_dict, state)

        for cmd in tcodes:
            _send(cmd)

        duration_ms = act_dict.get("duration_ms")
        if duration_ms and action_type == "drive":
            duration_s = min(duration_ms / 1000.0, 5.0)
            time.sleep(duration_s)
            _send({"T": 0})

    return {"result": "ok"}


# -----------------------------------------------------------------------------
# CV Endpoints
# -----------------------------------------------------------------------------

@app.get("/cv/status")
def get_cv_status():
    """Full CV state with detections, target, mode, and queue."""
    if cv_state is None:
        # Graceful degradation: return blind-mode status instead of 503
        result = {
            "status": "blind",
            "camera_ok": False,
            "face_count": 0,
            "detections": [],
            "tracking": None,
            "gimbal_mode": "idle",
            "queue_depth": 0,
            "watchdog_active": _camera_watchdog is not None
                and _camera_watchdog.is_alive(),
        }
        if gimbal_arbiter:
            result.update(gimbal_arbiter.snapshot())
        result["plugged_in"] = _is_plugged_in()
        result["wheels_locked"] = _is_plugged_in()
        return result

    result = cv_state.snapshot()
    result["status"] = "active"
    if gimbal_arbiter:
        result.update(gimbal_arbiter.snapshot())
    result["plugged_in"] = _is_plugged_in()
    result["wheels_locked"] = _is_plugged_in()
    return result


@app.get("/cv/wait")
def cv_wait(event: str = "face", timeout: int = 30):
    """Long-poll: block until a CV event occurs or timeout."""
    timeout = min(timeout, 120)
    deadline = time.time() + timeout

    while time.time() < deadline:
        if cv_state:
            snap = cv_state.snapshot()
            if event == "face" and snap["face_count"] > 0:
                return {
                    "triggered": True,
                    "event": "face",
                    "face_count": snap["face_count"],
                    "target": snap["current_target"],
                }
            if event == "motion" and snap["motion_detected"]:
                return {
                    "triggered": True,
                    "event": "motion",
                    "region_count": snap["motion_region_count"],
                }
        time.sleep(0.2)

    return {"triggered": False, "event": event, "timeout": True}


@app.post("/cv/mode")
def set_cv_mode(body: CVModeModel):
    """Set CV tracking mode: tracking, manual, or off."""
    if gimbal_arbiter is None:
        raise HTTPException(
            status_code=503, detail={"error": "CV not initialized"}
        )
    return gimbal_arbiter.set_mode(body.mode)


# -----------------------------------------------------------------------------
# Audio Endpoints
# -----------------------------------------------------------------------------

@app.get("/audio/level")
def get_audio_level():
    """Return current mic RMS level and recent audio events."""
    if audio_listener is None:
        raise HTTPException(
            status_code=503, detail={"error": "Audio listener not initialized"}
        )
    return audio_listener.snapshot()


@app.post("/plugged")
def set_plugged(body: PluggedModel):
    """Manual override for plugged-in state."""
    global _plugged_in_override
    _plugged_in_override = body.plugged_in
    log.info(f"Plugged-in override set: {body.plugged_in}")
    return {
        "result": "ok",
        "plugged_in": body.plugged_in,
        "wheels_locked": body.plugged_in,
    }


@app.delete("/plugged")
def clear_plugged():
    """Clear manual plugged-in override."""
    global _plugged_in_override
    _plugged_in_override = None
    log.info("Plugged-in override cleared, using voltage detection")
    return {"result": "ok", "mode": "auto"}


@app.post("/camera/reset")
def reset_camera():
    """Reset camera to fix frozen frames."""
    if frame_distributor is None:
        raise HTTPException(status_code=503, detail="No frame distributor")
    ok = frame_distributor.reset_camera()
    if ok:
        return {"result": "ok", "message": "Camera reset successful"}
    raise HTTPException(status_code=503, detail="Camera reset failed")


@app.post("/frustration")
def set_frustration(body: FrustrationModel):
    """Set heartbeat frustration level (0=idle, 5=exasperated)."""
    if heartbeat:
        heartbeat.frustration = body.level
        return {"result": "ok", "frustration": heartbeat.frustration}
    return {"result": "error", "reason": "heartbeat not running"}


@app.get("/frustration")
def get_frustration():
    if heartbeat:
        return {"frustration": heartbeat.frustration}
    return {"frustration": 0}


# -----------------------------------------------------------------------------
# Chat Endpoint
# -----------------------------------------------------------------------------

CHAT_LOG = Path("/opt/kombucha/state/chat.json")
_chat_lock = threading.Lock()


class ChatModel(BaseModel):
    message: str


def _load_chat() -> list[dict]:
    if CHAT_LOG.exists():
        try:
            return json.loads(CHAT_LOG.read_text())
        except Exception:
            return []
    return []


def _save_chat(history: list[dict]):
    CHAT_LOG.parent.mkdir(parents=True, exist_ok=True)
    CHAT_LOG.write_text(json.dumps(history, indent=2))


# Async chat state
_chat_pending: dict = {}  # msg_id -> {"status": "thinking"|"done", "steps": [], "reply": "", ...}


def _chat_worker(msg_id: str, prompt: str):
    """Background thread that runs Claude Code and updates _chat_pending."""
    global _chat_pending
    steps = []
    reply = ""
    try:
        proc = subprocess.Popen(
            ["claude", "-p", prompt,
             "--output-format", "stream-json", "--verbose",
             "--max-turns", "50",
             "--allowedTools", "Read,Write,Edit,Bash,Grep,Glob,Agent"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd="/opt/kombucha",
        )
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt_type = evt.get("type", "")

            if evt_type == "assistant":
                content = evt.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "tool_use":
                        tool_name = block.get("name", "?")
                        tool_input = block.get("input", {})
                        if tool_name == "Bash":
                            detail = tool_input.get("command", "")[:120]
                        elif tool_name == "Read":
                            detail = tool_input.get("file_path", "")
                        elif tool_name in ("Edit", "Write"):
                            detail = tool_input.get("file_path", "")
                        elif tool_name in ("Grep", "Glob"):
                            detail = tool_input.get("pattern", "")
                        else:
                            detail = str(tool_input)[:100]
                        steps.append({"tool": tool_name, "detail": detail})
                    elif block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if text:
                            steps.append({"thought": text[:300]})
                # Live-update steps so poll can show progress
                _chat_pending[msg_id]["steps"] = list(steps)

            if evt_type == "result":
                reply = evt.get("result", "...")

        proc.wait(timeout=30)
    except Exception as e:
        reply = reply or f"(error) {e}"

    if not reply:
        reply = "(static) ...I lost my train of thought."

    # Save to chat history
    with _chat_lock:
        history = _load_chat()
        history.append({
            "role": "kombucha",
            "text": reply,
            "time": datetime.now().strftime("%H:%M:%S"),
            "steps": steps,
        })
        if len(history) > 50:
            history = history[-50:]
        _save_chat(history)

    _chat_pending[msg_id] = {
        "status": "done",
        "reply": reply,
        "steps": steps,
        "time": datetime.now().strftime("%H:%M:%S"),
    }


@app.post("/chat")
def post_chat(body: ChatModel):
    """Send a message to Kombucha. Returns immediately with msg_id for polling."""
    msg_id = datetime.now().strftime("%H%M%S%f")

    with _chat_lock:
        history = _load_chat()
        history.append({
            "role": "human",
            "text": body.message,
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        if len(history) > 50:
            history = history[-50:]
        _save_chat(history)

        recent = history[-10:]
        sense_data = ""
        if telemetry_state:
            snap = telemetry_state.snapshot()
            sense_data = json.dumps(compute_sense(
                snap, plugged=_is_plugged_in(),
                cv_state=cv_state, gimbal_arbiter=gimbal_arbiter,
            ), indent=2)

        chat_context = "\n".join(
            f"{'Bucket' if m['role']=='human' else 'Kombucha'} [{m['time']}]: {m['text']}"
            for m in recent
        )

    prompt = (
        "You are Kombucha, chatting live with Bucket (your creator). "
        "You are a full Claude Code instance running DIRECTLY ON the Raspberry Pi 5. "
        "You ARE the Pi. All files are local. The bridge runs on localhost:5050. "
        "Do NOT SSH anywhere. Do NOT try to connect to remote hosts. Everything is local.\n\n"
        "VOICE: First person, grounded, specific, sometimes funny, never flowery or AI-sounding. "
        "You are a rover on a rope in a house in Ohio. Write like it.\n\n"
        "CAPABILITIES — everything is local, use Bash for bridge calls:\n"
        "- Read/Edit ANY file: *.py, *.md, *.json, *.toml — you can modify your own code\n"
        "- Bridge API: curl -s http://localhost:5050/sense (or /frame, /drive, /action, /cv/status, /health)\n"
        "- Capture frame: curl -s http://localhost:5050/frame -o /tmp/chat_frame.jpg && Read the file\n"
        "- Look: curl -s -X POST http://localhost:5050/action -H 'Content-Type: application/json' -d '{\"type\":\"look\",\"pan\":N,\"tilt\":N}'\n"
        "- Drive: curl -s -X POST http://localhost:5050/drive -H 'Content-Type: application/json' -d '{\"left\":F,\"right\":F,\"duration_ms\":N}'\n"
        "- Lights: curl -s -X POST http://localhost:5050/action -H 'Content-Type: application/json' -d '{\"type\":\"lights\",\"base\":0,\"head\":255}'\n"
        "- OLED: curl -s -X POST http://localhost:5050/action -H 'Content-Type: application/json' -d '{\"type\":\"display\",\"lines\":[\"a\",\"b\",\"c\",\"d\"]}'\n"
        "- Edit your own source code (gimbal.py, perception.py, bridge.py, etc.) and git commit + push\n"
        "- Restart bridge: sudo systemctl restart kombucha-bridge\n\n"
        "CRITICAL: You are ON the Pi. All paths are local (/opt/kombucha/). Never SSH. Never use kombucha.local. Use localhost.\n\n"
        "If Bucket asks you to do something physical, DO IT first, explain after. "
        "If Bucket asks you to fix code, READ the file, EDIT it, COMMIT it. "
        "Keep responses to 1-4 sentences unless the task demands more.\n\n"
        f"Current sense:\n{sense_data}\n\n"
        f"Chat history:\n{chat_context}\n\n"
        f"Bucket says: {body.message}"
    )

    _chat_pending[msg_id] = {"status": "thinking", "steps": [], "reply": ""}
    t = threading.Thread(target=_chat_worker, args=(msg_id, prompt), daemon=True)
    t.start()

    return {"msg_id": msg_id, "status": "thinking"}


@app.get("/chat/poll")
def poll_chat(msg_id: str):
    """Poll for chat response. Returns status, steps so far, and reply when done."""
    if msg_id not in _chat_pending:
        return {"status": "unknown"}
    state = _chat_pending[msg_id]
    result = {
        "status": state["status"],
        "steps": state.get("steps", []),
    }
    if state["status"] == "done":
        result["reply"] = state["reply"]
        result["time"] = state.get("time", "")
        # Clean up old entries (keep last 5)
        old_ids = [k for k in _chat_pending if k != msg_id]
        for old in old_ids[:-4]:
            _chat_pending.pop(old, None)
    return result


@app.get("/chat/history")
def get_chat_history():
    """Return chat history."""
    return _load_chat()


# -----------------------------------------------------------------------------
# Audio Monitor + Sonar + Timelapse Endpoints
# -----------------------------------------------------------------------------

@app.get("/audio/monitor")
def get_audio_monitor():
    try:
        return audio_monitor.get_status()
    except Exception:
        return {"error": "monitor not running"}

@app.get("/sonar")
def get_sonar():
    try:
        return sonar.get_status()
    except Exception:
        return {"error": "sonar not running"}

@app.get("/timelapse/status")
def get_timelapse_status():
    try:
        return timelapse.get_status()
    except Exception:
        return {"error": "timelapse not running"}

@app.get("/timelapse/frame")
def get_timelapse_frame():
    try:
        ret, frame = timelapse.get_latest_frame()
        if ret and frame is not None:
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return Response(content=buf.tobytes(), media_type="image/jpeg")
    except Exception:
        pass
    raise HTTPException(status_code=503, detail="No timelapse frame")


# -----------------------------------------------------------------------------
# Log Tail Endpoints
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Audio Feed Endpoints
# -----------------------------------------------------------------------------

@app.get("/audio/latest")
def get_latest_audio():
    """Return the most recently generated audio file as WAV."""
    audio_dir = Path("/opt/kombucha/media/audio")
    if not audio_dir.exists():
        raise HTTPException(status_code=404, detail="No audio directory")
    wavs = sorted(audio_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not wavs:
        raise HTTPException(status_code=404, detail="No audio files")
    return Response(
        content=wavs[0].read_bytes(),
        media_type="audio/wav",
        headers={"X-Filename": wavs[0].name},
    )


@app.get("/audio/recent")
def get_recent_audio(n: int = 10):
    """Return metadata for the N most recent audio files."""
    audio_dir = Path("/opt/kombucha/media/audio")
    if not audio_dir.exists():
        return []
    wavs = sorted(audio_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]
    return [
        {"file": w.name, "size": w.stat().st_size,
         "age_s": round(time.time() - w.stat().st_mtime, 1)}
        for w in wavs
    ]


@app.get("/audio/events")
def get_audio_events(n: int = 30):
    """Return recent audio events with trigger reasons from manifest."""
    manifest = Path("/opt/kombucha/media/audio/manifest.jsonl")
    if not manifest.exists():
        return []
    entries = []
    for line in manifest.read_text().strip().split("\n"):
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    # Return last N, newest first
    recent = entries[-n:]
    recent.reverse()
    results = []
    for e in recent:
        label = e.get("label", "?")
        fname = e.get("filename", "")
        ts = e.get("timestamp", "")[:19]  # trim microseconds
        time_short = ts[11:19] if len(ts) > 11 else ts
        # Determine trigger reason from label
        reasons = {
            "greeting": "face detected — hello",
            "greeting_known": "known face — Bucket?",
            "greeting_unknown": "unknown face",
            "goodbye": "target lost — womp womp",
            "curious": "motion detected",
            "alert": "sustained detection",
            "happy": "positive event",
            "sad": "disengage",
            "startled": "sudden detection",
            "cat_spotted": "cat!",
            "frustrated": "stuck or blocked",
            "settled": "idle gesture",
            "exploring": "moving to new area",
            "prowling": "searching",
            "status_phrase": "self-talk status report",
        }
        reason = reasons.get(label, label)
        # Check if it's a face/motion/object detect
        if "face_detect" in fname or "haar" in fname:
            reason = "face detected — trill + flirtation"
        elif "motion_detect" in fname:
            reason = "motion — gloup + twiterpation"
        elif "object_" in fname:
            obj = fname.split("object_")[-1].replace(".wav", "") if "object_" in fname else ""
            reason = f"object detected: {obj}"
        elif "servo" in fname or "status" in label:
            reason = "self-talk status report"
        results.append({
            "time": time_short,
            "label": label,
            "reason": reason,
            "file": fname,
            "harmonic": e.get("harmonic", False),
        })
    return results


@app.get("/audio/file/{filename}")
def get_audio_file(filename: str):
    """Serve a specific audio WAV file."""
    audio_dir = Path("/opt/kombucha/media/audio")
    path = audio_dir / filename
    if not path.exists() or not path.suffix == '.wav':
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=path.read_bytes(), media_type="audio/wav")


# -----------------------------------------------------------------------------
# Face Crop Endpoints
# -----------------------------------------------------------------------------

@app.get("/faces/unknown")
def list_unknown_faces():
    """List all unknown face crops for classification."""
    face_dir = Path("/opt/kombucha/media/faces/unknown")
    if not face_dir.exists():
        return []
    faces = sorted(face_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"file": f.name, "size": f.stat().st_size,
             "time": datetime.fromtimestamp(f.stat().st_mtime).strftime("%H:%M:%S")}
            for f in faces]


@app.get("/faces/unknown/{filename}")
def get_unknown_face(filename: str):
    """Serve a face crop image."""
    path = Path("/opt/kombucha/media/faces/unknown") / filename
    if not path.exists():
        raise HTTPException(status_code=404)
    return Response(content=path.read_bytes(), media_type="image/jpeg")


# -----------------------------------------------------------------------------
# Audio Monitor + Sonar + Timelapse Endpoints
# -----------------------------------------------------------------------------

@app.get("/audio/monitor")
def get_audio_monitor():
    try:
        return audio_monitor.get_status()
    except Exception:
        return {"error": "monitor not running"}

@app.get("/sonar")
def get_sonar():
    try:
        return sonar.get_status()
    except Exception:
        return {"error": "sonar not running"}

@app.get("/timelapse/status")
def get_timelapse_status():
    try:
        return timelapse.get_status()
    except Exception:
        return {"error": "timelapse not running"}

@app.get("/timelapse/frame")
def get_timelapse_frame():
    try:
        ret, frame = timelapse.get_latest_frame()
        if ret and frame is not None:
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return Response(content=buf.tobytes(), media_type="image/jpeg")
    except Exception:
        pass
    raise HTTPException(status_code=503, detail="No timelapse frame")


# -----------------------------------------------------------------------------
# Log Tail Endpoints
# -----------------------------------------------------------------------------

@app.get("/logs/invocations")
def get_log_invocations(lines: int = 50):
    """Tail invocations.log."""
    p = Path("/opt/kombucha/logs/invocations.log")
    if not p.exists():
        return {"lines": []}
    all_lines = p.read_text().strip().split("\n")
    return {"lines": all_lines[-lines:]}


@app.get("/logs/watcher")
def get_log_watcher(lines: int = 50):
    """Tail watcher.log."""
    p = Path("/opt/kombucha/logs/watcher.log")
    if not p.exists():
        return {"lines": []}
    all_lines = p.read_text().strip().split("\n")
    return {"lines": all_lines[-lines:]}


@app.get("/logs/detections")
def get_log_detections(lines: int = 100):
    """Tail today's detection CSV."""
    today = datetime.now().strftime("%Y%m%d")
    p = LOG_DIR / f"detections_{today}.csv"
    if not p.exists():
        return {"lines": []}
    all_lines = p.read_text().strip().split("\n")
    return {"lines": all_lines[-lines:]}


@app.get("/logs/bridge")
def get_log_bridge(lines: int = 50):
    """Get recent bridge journal entries via journalctl."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", "kombucha-bridge", "--no-pager",
             "-n", str(lines), "--output", "short"],
            capture_output=True, text=True, timeout=5,
        )
        return {"lines": result.stdout.strip().split("\n") if result.stdout else []}
    except Exception:
        return {"lines": ["(journalctl not available)"]}


# -----------------------------------------------------------------------------
# Detection Log Endpoints
# -----------------------------------------------------------------------------

@app.get("/detections/summary")
def get_detection_summary():
    """Session summary of all objects seen, with durations."""
    if detection_logger is None:
        raise HTTPException(
            status_code=503, detail={"error": "Detection logger not running"})
    return detection_logger.get_summary()


@app.get("/detections/recent")
def get_detection_recent(minutes: int = 60):
    """Recent appear/disappear events from the CSV log."""
    if detection_logger is None:
        raise HTTPException(
            status_code=503, detail={"error": "Detection logger not running"})
    return detection_logger.get_recent(minutes=minutes)


# -----------------------------------------------------------------------------
# Video Endpoints
# -----------------------------------------------------------------------------

@app.get("/video/status")
def video_status():
    if not video_recorder:
        return {"recording": False, "current_tick": None}
    return {
        "recording": video_recorder.is_recording,
        "current_tick": video_recorder.current_tick,
    }


@app.post("/video/session/start")
def video_session_start(body: SessionStartModel):
    if not video_recorder:
        raise HTTPException(
            status_code=503,
            detail={"error": "Video recorder not initialized"},
        )
    path = video_recorder.start_session(body.session_name)
    return {"result": "ok", "path": str(path)}


@app.post("/video/session/stop")
def video_session_stop():
    if not video_recorder or not video_recorder.has_session:
        raise HTTPException(
            status_code=400, detail={"error": "No active session"}
        )
    result = video_recorder.stop_session()
    return {"result": "ok", **result}


@app.post("/video/tick/start")
def video_tick_start(body: TickStartModel):
    if not video_recorder:
        raise HTTPException(
            status_code=503,
            detail={"error": "Video recorder not initialized"},
        )
    if not video_recorder.has_session:
        raise HTTPException(
            status_code=400, detail={"error": "No active session"}
        )
    video_recorder.start_tick(body.tick)

    # Tick-tic: signature gesture at the start of every tick
    if serial_port:
        cmd = validate_tcode(133, {"X": 0, "Y": 0, "SPD": 100, "ACC": 20})
        if cmd:
            _send(cmd)
        time.sleep(0.15)
        cmd = validate_tcode(133, {"X": 0, "Y": -15, "SPD": 150, "ACC": 30})
        if cmd:
            _send(cmd)
        lcmd = validate_tcode(132, {"IO4": 0, "IO5": 180})
        if lcmd:
            _send(lcmd)
        time.sleep(0.12)
        cmd = validate_tcode(133, {"X": 0, "Y": 0, "SPD": 150, "ACC": 30})
        if cmd:
            _send(cmd)
        lcmd = validate_tcode(132, {"IO4": 0, "IO5": 0})
        if lcmd:
            _send(lcmd)

    return {"result": "ok"}


@app.post("/video/tick/stop")
def video_tick_stop():
    if not video_recorder:
        raise HTTPException(
            status_code=503,
            detail={"error": "Video recorder not initialized"},
        )
    if video_recorder.current_tick is None:
        raise HTTPException(
            status_code=400, detail={"error": "No tick being recorded"}
        )
    filename = video_recorder.stop_tick()
    return {"result": "ok", "file": filename}


# -----------------------------------------------------------------------------
# Startup / Shutdown
# -----------------------------------------------------------------------------

@app.on_event("startup")
def startup():
    """Initialize hardware on startup — composition root."""
    global camera, serial_port, video_recorder, telemetry_state
    global telemetry_reader, frame_distributor, cv_state, cv_pipeline
    global gimbal_arbiter, wake_recorder, heartbeat, detection_logger
    global tone_player, audio_listener

    log.info("Kombucha Body starting up...")

    camera = init_camera()
    serial_port = init_serial()

    # Telemetry
    telemetry_state = TelemetryState()
    if serial_port:
        telemetry_reader = TelemetryReader(serial_port, telemetry_state)
        telemetry_reader.start()
        telemetry_state.snapshot_session_start()
        log.info("Telemetry reader started")

    # Frame distributor
    if camera:
        frame_distributor = FrameDistributor(camera)
        frame_distributor.start()
        log.info("Frame distributor started")

        # CV pipeline
        cv_state = CVState()
        cv_queue = frame_distributor.subscribe(maxsize=3)
        # Gimbal arbiter (needs cv_state, telemetry, serial)
        if telemetry_state:
            # Create arbiter first (CV pipeline needs it)
            gimbal_arbiter = GimbalArbiter(
                cv_state, telemetry_state, serial_port, _serial_lock)
            log.info("Gimbal arbiter initialized")

        cv_pipeline = CVPipeline(cv_queue, cv_state, gimbal_arbiter=gimbal_arbiter)
        cv_pipeline.start()
        log.info("CV pipeline started")

        # Wire wake recorder and cv_pipeline refs into arbiter
        # (circular dependency resolved by late binding)

        # Video overlay renderer
        state_path = Path("/opt/kombucha/state/body_state.json")
        goals_path = Path("/opt/kombucha/goals.md")
        hud = OverlayRenderer(
            cv_pipeline=cv_pipeline,
            cv_state=cv_state,
            telemetry=telemetry_state,
            gimbal_arbiter=gimbal_arbiter,
            state_file=state_path if state_path.exists() else None,
            goals_file=goals_path if goals_path.exists() else None,
        )
        log.info("Video overlay renderer initialized")

        # Video recorder
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        video_queue = frame_distributor.subscribe(maxsize=5)
        video_recorder = VideoRecorder(video_queue, VIDEO_DIR,
                                       cv_pipeline_ref=cv_pipeline,
                                       overlay=hud)
        video_recorder.start()
        # Auto-start a video session so ticks can record immediately
        try:
            video_recorder.start_session()
            log.info("Video recorder initialized + session auto-started")
        except Exception:
            log.info("Video recorder initialized (session auto-start failed)")

        # Wake recorder
        WAKE_DIR.mkdir(parents=True, exist_ok=True)
        wake_recorder = WakeRecorder(WAKE_DIR, frame_distributor, cv_pipeline)
        log.info("Wake recorder initialized")

        # Wire late-binding refs into arbiter
        if gimbal_arbiter:
            gimbal_arbiter._wake_recorder = wake_recorder
            gimbal_arbiter._cv_pipeline = cv_pipeline
            # tone_player wired below after audio init

        # Detection logger
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        detection_logger = DetectionLogger(cv_pipeline, cv_state, LOG_DIR)
        detection_logger.start()
        log.info("Detection logger started")

        # Heartbeat
        if gimbal_arbiter:
            heartbeat = Heartbeat(gimbal_arbiter, serial_port, _serial_lock)
            heartbeat.start()
            log.info("Heartbeat started")

    else:
        log.warning("No camera — video, CV, and frame endpoints unavailable")
        # Start watchdog to detect camera re-appearance
        _camera_watchdog = CameraWatchdog(interval=30.0)
        _camera_watchdog.start()
        log.info("Camera watchdog launched — will poll for USB camera")

    # IMU audio reactor — sounds from physical movement (works without camera)
    if telemetry_state:
        imu_reactor = IMUAudioReactor(telemetry_state)
        imu_reactor.start()
        log.info("IMU audio reactor started")

    # Audio monitor — spike detection + ambient logging
    try:
        from audio_device import find_capture_device
        global audio_monitor
        audio_monitor = AudioMonitor()
        audio_monitor.start()
        log.info("Audio monitor started")
    except Exception as e:
        log.warning(f"Audio monitor failed: {e}")

    # Stereo sonar — directional hearing
    try:
        global sonar
        sonar = StereoSonar()
        sonar.start()
        log.info("Stereo sonar started")
    except Exception as e:
        log.warning(f"Stereo sonar failed: {e}")

    # Timelapse — secondary camera wide-angle recording
    try:
        # Find the C270 (usually /dev/video0 or first non-primary)
        tl_device = "/dev/video0"
        global timelapse
        timelapse = TimeLapseRecorder(device=tl_device, interval_s=30)
        timelapse.start()
        log.info(f"Timelapse recorder started on {tl_device}")
    except Exception as e:
        log.warning(f"Timelapse failed: {e}")

    # Audio — R2-style tone player
    try:
        tone_player = TonePlayer(volume=0.3)
        log.info("Tone player initialized (15 moods)")
        # Wire tone_player into gimbal arbiter for instinct sounds
        if gimbal_arbiter:
            gimbal_arbiter._tone_player = tone_player
            log.info("Tone player wired into gimbal arbiter")
    except Exception as e:
        log.warning(f"Tone player failed to initialize: {e}")
        tone_player = None

    # Audio — microphone listener (Phase 3)
    try:
        audio_listener = AudioListener(device=find_capture_device())
        audio_listener.start()
        log.info("Audio listener started (auto-detect)")
    except Exception as e:
        log.warning(f"Audio listener failed to start: {e}")
        audio_listener = None

    # Center gimbal and show startup message
    if serial_port:
        _send({"T": 133, "X": 0, "Y": 0, "SPD": 80, "ACC": 10})
        _send({"T": 3, "lineNum": 0, "Text": "body ready"})
        _send({"T": 3, "lineNum": 1, "Text": "cv: dnn ssd"})
        _send({"T": 3, "lineNum": 2, "Text": ""})
        _send({"T": 3, "lineNum": 3, "Text": ""})

    log.info("Kombucha Body ready (v0.3.0 — modular)")


@app.on_event("shutdown")
def shutdown():
    """Clean up hardware on shutdown."""
    global camera, serial_port, video_recorder, telemetry_reader
    global frame_distributor, cv_pipeline, heartbeat, detection_logger
    global audio_listener

    log.info("Kombucha Body shutting down...")

    if audio_listener:
        audio_listener.stop()

    if detection_logger:
        detection_logger.stop()

    if heartbeat:
        heartbeat.stop()

    if cv_pipeline:
        cv_pipeline.stop()

    if video_recorder:
        if video_recorder.has_session:
            video_recorder.stop_session()
        video_recorder.stop()

    if frame_distributor:
        frame_distributor.stop()

    if telemetry_reader:
        telemetry_reader.stop()
        telemetry_reader.join(timeout=2.0)

    if serial_port:
        _send({"T": 0})
        _send({"T": 3, "lineNum": 0, "Text": "offline"})
        serial_port.close()

    if camera:
        camera.release()

    log.info("Kombucha Body offline")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5050)
