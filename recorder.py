"""
recorder.py - Video recording and wake event capture.

VideoRecorder manages session/tick video recording.
WakeRecorder captures snapshots and frame sequences during instinct events.
"""

import json
import logging
import queue
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2

from hardware import CAPTURE_W, CAPTURE_H, JPEG_QUALITY

log = logging.getLogger(__name__)

_DET_COLORS = {
    "person": (0, 180, 255), "cat": (255, 100, 255),
    "dog": (255, 180, 0), "chair": (100, 100, 255),
}


def _annotate_detections(frame, detections: list[dict]):
    """Draw YOLO bounding boxes on a frame copy."""
    frame = frame.copy()
    for det in detections:
        x, y, w, h = det["x"], det["y"], det["w"], det["h"]
        color = _DET_COLORS.get(det.get("class_name", "?"), (100, 200, 100))
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
        label = f"{det['class_name']} {det['confidence']:.0%}"
        cv2.putText(frame, label, (x, y - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
    return frame


# -----------------------------------------------------------------------------
# Video Recorder
# -----------------------------------------------------------------------------

class VideoRecorder(threading.Thread):
    """Background thread for video recording.

    Reads frames from a FrameDistributor subscription queue.
    Uses OverlayRenderer for rich HUD when available.
    """

    def __init__(self, frame_queue: queue.Queue, output_dir: Path,
                 cv_pipeline_ref=None, overlay=None):
        super().__init__(daemon=True)
        self._frame_queue = frame_queue
        self.output_dir = output_dir
        self._cv_pipe = cv_pipeline_ref
        self._overlay = overlay
        self._running = False
        self._recording = False
        self._session_dir: Optional[Path] = None
        self._session_start: Optional[float] = None
        self._current_tick: Optional[int] = None
        self._tick_start: Optional[float] = None
        self._writer: Optional[cv2.VideoWriter] = None
        self._current_filename: Optional[str] = None
        self._session_ticks: list = []
        self._frames_written = 0
        self._lock = threading.Lock()

    def run(self):
        self._running = True
        log.info("VideoRecorder thread started")
        frame_count = 0
        while self._running:
            try:
                frame, frame_id = self._frame_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            frame_count += 1
            if frame_count == 1:
                log.info(f"VideoRecorder: first frame received (shape={frame.shape})")

            if self._recording:
                if self._overlay:
                    try:
                        frame = self._overlay.render(frame)
                    except Exception as e:
                        log.error(f"Overlay render failed: {e}")
                        frame = frame.copy()  # Use raw frame on overlay failure
                elif self._cv_pipe:
                    dets = self._cv_pipe.get_detections()
                    if dets:
                        frame = _annotate_detections(frame, dets)
                with self._lock:
                    if self._writer:
                        self._writer.write(frame)
                        self._frames_written += 1

    def stop(self):
        self._running = False

    def start_session(self, session_name: Optional[str] = None) -> Path:
        if session_name:
            dirname = f"session_{session_name}"
        else:
            dirname = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        self._session_dir = self.output_dir / dirname
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._session_start = time.time()
        self._session_ticks = []
        log.info(f"Video session started: {self._session_dir}")
        return self._session_dir

    def stop_session(self) -> dict:
        self.stop_tick()

        if not self._session_dir:
            return {}

        session_data = {
            "session_start": (
                datetime.fromtimestamp(self._session_start).isoformat()
                if self._session_start else None
            ),
            "session_end": datetime.now().isoformat(),
            "ticks": self._session_ticks,
        }

        session_json = self._session_dir / "session.json"
        with open(session_json, "w") as f:
            json.dump(session_data, f, indent=2)

        log.info(f"Video session stopped: {len(self._session_ticks)} ticks")

        result = {
            "path": str(self._session_dir),
            "ticks": len(self._session_ticks),
        }
        self._session_dir = None
        self._session_start = None
        return result

    def start_tick(self, tick_num: int):
        self.stop_tick()

        if not self._session_dir:
            raise ValueError("No active session")

        self._current_tick = tick_num
        self._tick_start = time.time()
        self._frames_written = 0

        filename = f"tick_{tick_num:04d}.mp4"
        self._current_filename = filename
        filepath = self._session_dir / filename

        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        with self._lock:
            self._writer = cv2.VideoWriter(
                str(filepath), fourcc, 30.0, (CAPTURE_W, CAPTURE_H)
            )
            self._recording = True

        log.info(f"Recording tick {tick_num}")

    def stop_tick(self) -> Optional[str]:
        if not self._recording or self._current_tick is None:
            return None

        with self._lock:
            self._recording = False
            if self._writer:
                self._writer.release()
                self._writer = None

        duration = time.time() - self._tick_start if self._tick_start else 0

        self._session_ticks.append({
            "tick": self._current_tick,
            "file": self._current_filename,
            "duration_s": round(duration, 2),
            "frames": self._frames_written,
        })

        filename = self._current_filename
        log.info(
            f"Tick {self._current_tick} stopped: "
            f"{self._frames_written} frames, {duration:.1f}s"
        )

        self._current_tick = None
        self._tick_start = None
        self._current_filename = None

        return filename

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def current_tick(self) -> Optional[int]:
        return self._current_tick

    @property
    def has_session(self) -> bool:
        return self._session_dir is not None


# -----------------------------------------------------------------------------
# Wake Recorder
# -----------------------------------------------------------------------------

class WakeRecorder:
    """Records snapshots and video clips when instinct activates."""

    MAX_EVENTS = 50
    MAX_WAKE_DURATION_S = 300  # Auto-close wakes after 5 minutes

    def __init__(self, output_dir: Path, frame_dist, cv_pipe):
        self._output_dir = output_dir
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._frame_dist = frame_dist
        self._cv_pipe = cv_pipe
        self._lock = threading.Lock()

        self._active = False
        self._wake_start: Optional[float] = None
        self._wake_id: Optional[str] = None
        self._writer: Optional[cv2.VideoWriter] = None
        self._video_path: Optional[Path] = None
        self._snapshot_path: Optional[str] = None
        self._items_seen: set[str] = set()
        self._frames_written = 0

        self._frame_queue: Optional[queue.Queue] = None
        self._recording_thread: Optional[threading.Thread] = None
        self._recording = False

        self._events: deque[dict] = deque(maxlen=self.MAX_EVENTS)
        self._frame_log: list[dict] = []  # Per-frame detection log for dossier

    def engage(self, trigger: str, detections: list[dict]):
        """Called when instinct activates. Snaps a photo, starts video."""
        with self._lock:
            if self._active:
                return

            self._active = True
            self._wake_start = time.time()
            self._wake_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._items_seen = set()
            self._frames_written = 0
            self._frame_log = []

            for d in detections:
                self._items_seen.add(d.get("class_name", "unknown"))

            snapshot_name = f"wake_{self._wake_id}.jpg"
            self._snapshot_path = snapshot_name
            try:
                ret, frame, _ = self._frame_dist.get_fresh_frame(timeout_s=1.0)
                if ret and frame is not None:
                    frame = self._annotate_frame(frame, detections)
                    filepath = self._output_dir / snapshot_name
                    cv2.imwrite(str(filepath), frame,
                                [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                    log.info(f"Wake snapshot: {filepath}")
            except Exception as e:
                log.error(f"Wake snapshot failed: {e}")

            self._recording = True
            self._frame_queue = self._frame_dist.subscribe(maxsize=5)
            self._recording_thread = threading.Thread(
                target=self._record_loop, daemon=True)
            self._recording_thread.start()
            log.info(f"Wake capture started: {self._wake_id}")

    def update_detections(self, detections: list[dict]):
        """Called during instinct to accumulate items seen."""
        with self._lock:
            if not self._active:
                return
            for d in detections:
                self._items_seen.add(d.get("class_name", "unknown"))

    def disengage(self):
        """Called when instinct releases. Stops video, logs event."""
        with self._lock:
            if not self._active:
                return

            self._recording = False
            self._active = False

            if self._frame_queue and self._frame_dist:
                self._frame_dist.unsubscribe(self._frame_queue)
                self._frame_queue = None

            duration = time.time() - self._wake_start if self._wake_start else 0

            event = {
                "wake_id": self._wake_id,
                "snapshot": self._snapshot_path,
                "frames_captured": self._frames_written,
                "duration_s": round(duration, 1),
                "items_seen": sorted(self._items_seen),
                "timestamp": self._wake_start,
            }
            self._events.append(event)

            # Write structured dossier JSON
            self._write_dossier(event, duration)

            log.info(
                f"Wake ended: {self._wake_id} — {duration:.1f}s, "
                f"{self._frames_written} frames, items: {sorted(self._items_seen)}"
            )

            self._wake_id = None
            self._wake_start = None
            self._video_path = None
            self._snapshot_path = None

    def _record_loop(self):
        """Background loop capturing periodic frames during wake events."""
        last_capture = 0.0
        capture_interval = 0.5
        while self._recording:
            try:
                frame, frame_id = self._frame_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            now = time.time()
            if now - last_capture < capture_interval:
                continue
            if self._recording:
                try:
                    dets = []
                    if self._cv_pipe:
                        dets = self._cv_pipe.get_detections()
                        frame = self._annotate_frame(frame, dets)
                        for d in dets:
                            self._items_seen.add(d.get("class_name", "unknown"))
                    fname = f"wake_{self._wake_id}_{self._frames_written:03d}.jpg"
                    cv2.imwrite(
                        str(self._output_dir / fname), frame,
                        [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                    elapsed = now - self._wake_start if self._wake_start else 0
                    self._frame_log.append({
                        "frame": fname,
                        "elapsed_s": round(elapsed, 2),
                        "timestamp": datetime.fromtimestamp(now).strftime(
                            "%H:%M:%S"),
                        "detections": [
                            {"class": d.get("class_name", "unknown"),
                             "confidence": round(d.get("confidence", 0), 2),
                             "bbox": [d["x"], d["y"], d["w"], d["h"]]}
                            for d in dets
                        ],
                    })
                    self._frames_written += 1
                    last_capture = now
                except Exception as e:
                    log.error(f"Wake capture error: {e}")

    def _write_dossier(self, event: dict, duration: float):
        """Write a structured wake dossier JSON for the soul to read."""
        try:
            # Build per-class summary from frame log
            class_summary = {}
            for entry in self._frame_log:
                for det in entry["detections"]:
                    cls = det["class"]
                    if cls not in class_summary:
                        class_summary[cls] = {
                            "first_seen_s": entry["elapsed_s"],
                            "last_seen_s": entry["elapsed_s"],
                            "peak_confidence": det["confidence"],
                            "frame_count": 0,
                        }
                    cs = class_summary[cls]
                    cs["last_seen_s"] = entry["elapsed_s"]
                    cs["frame_count"] += 1
                    if det["confidence"] > cs["peak_confidence"]:
                        cs["peak_confidence"] = det["confidence"]

            dossier = {
                "wake_id": event["wake_id"],
                "start_time": datetime.fromtimestamp(
                    event["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
                    if event["timestamp"] else None,
                "duration_s": round(duration, 1),
                "snapshot": event["snapshot"],
                "total_frames": self._frames_written,
                "items_seen": event["items_seen"],
                "class_summary": class_summary,
                "frame_log": self._frame_log,
            }

            dossier_path = self._output_dir / f"dossier_{event['wake_id']}.json"
            with open(dossier_path, "w") as f:
                json.dump(dossier, f, indent=2)
            log.info(f"Wake dossier written: {dossier_path}")
        except Exception as e:
            log.error(f"Failed to write wake dossier: {e}")

    def _annotate_frame(self, frame, detections: list[dict]):
        """Draw YOLO bounding boxes on a frame."""
        return _annotate_detections(frame, detections)

    def get_recent_events(self, n: int = 10) -> list[dict]:
        """Get the N most recent wake events for /sense."""
        with self._lock:
            events = list(self._events)[-n:]
            if self._active and self._wake_start:
                current = {
                    "wake_id": self._wake_id,
                    "snapshot": self._snapshot_path,
                    "video": self._video_path.name if self._video_path else None,
                    "duration_s": round(time.time() - self._wake_start, 1),
                    "items_seen": sorted(self._items_seen),
                    "active": True,
                    "timestamp": self._wake_start,
                }
                events.append(current)
            return events

    def get_latest_dossier(self) -> Optional[Path]:
        """Return path to most recent wake dossier JSON, if any."""
        dossiers = sorted(self._output_dir.glob("dossier_*.json"))
        return dossiers[-1] if dossiers else None

    def check_timeout(self) -> bool:
        """Auto-disengage if wake has exceeded MAX_WAKE_DURATION_S.

        Returns True if a stale wake was closed.
        Call this periodically from the gimbal update loop.
        """
        should_close = False
        with self._lock:
            if not self._active or not self._wake_start:
                return False
            if time.time() - self._wake_start > self.MAX_WAKE_DURATION_S:
                log.warning(
                    f"Wake {self._wake_id} exceeded {self.MAX_WAKE_DURATION_S}s "
                    f"— auto-closing stale wake"
                )
                should_close = True
        if should_close:
            self.disengage()
            log.info("Stale wake auto-disengaged")
            return True
        return False

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active
