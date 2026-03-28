#!/usr/bin/env python3
"""
bridge.py - HTTP bridge for Kombucha rover hardware.

Composition root: creates all objects, wires dependencies, exposes REST API.
"""

import csv
import io
import logging
import os
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


# Detection logger
detection_logger = None

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

    # Append detection session summary (what's been seen and for how long)
    if detection_logger:
        result["detection_summary"] = detection_logger.get_summary()

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
        raise HTTPException(
            status_code=503, detail={"error": "CV not initialized"}
        )

    result = cv_state.snapshot()
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
        log.info("Video recorder initialized (with HUD overlay)")

        # Wake recorder
        WAKE_DIR.mkdir(parents=True, exist_ok=True)
        wake_recorder = WakeRecorder(WAKE_DIR, frame_distributor, cv_pipeline)
        log.info("Wake recorder initialized")

        # Wire late-binding refs into arbiter
        if gimbal_arbiter:
            gimbal_arbiter._wake_recorder = wake_recorder
            gimbal_arbiter._cv_pipeline = cv_pipeline

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

    log.info("Kombucha Body shutting down...")

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
