"""
perception.py - Frame distribution and computer vision pipeline.

FrameDistributor owns the camera. CVPipeline runs YOLO + motion detection.
"""

import dataclasses
import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from hardware import (
    CAPTURE_W, CAPTURE_H, CAMERA_FLIP,
    CV_PROCESS_EVERY_N, CV_MOTION_MIN_AREA, CV_MOTION_SUPPRESS_S,
)

log = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Frame Distributor
# -----------------------------------------------------------------------------

class FrameDistributor(threading.Thread):
    """Single owner of cv2.VideoCapture. Distributes frames to consumers."""

    def __init__(self, cap: cv2.VideoCapture):
        super().__init__(daemon=True)
        self._cap = cap
        self._running = False
        self._lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_id: int = 0
        self._last_frame_time: float = 0.0
        self._subscribers: list[queue.Queue] = []

    def get_latest_frame(self) -> tuple[bool, Optional[np.ndarray], int]:
        """Non-blocking read of most recent frame."""
        with self._lock:
            if self._latest_frame is None:
                return False, None, 0
            return True, self._latest_frame.copy(), self._frame_id

    def get_fresh_frame(self, timeout_s: float = 2.0) -> tuple[bool, Optional[np.ndarray], int]:
        """Wait for a frame newer than the current one.

        If no fresh frame arrives within timeout, auto-resets the camera
        and retries once before returning the stale frame.
        """
        with self._lock:
            old_id = self._frame_id
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(0.05)
            with self._lock:
                if self._frame_id > old_id and self._latest_frame is not None:
                    return True, self._latest_frame.copy(), self._frame_id
        # No fresh frame — camera is likely frozen. Auto-reset and retry.
        log.warning(f"No fresh frame in {timeout_s}s (frame_id stuck at {old_id}). Auto-resetting camera.")
        if self.reset_camera():
            # reset_camera sets _frame_id to 0, so use that as new baseline
            with self._lock:
                old_id = self._frame_id
            # Wait for a fresh frame after reset
            retry_deadline = time.time() + 3.0
            while time.time() < retry_deadline:
                time.sleep(0.1)
                with self._lock:
                    if self._frame_id > old_id and self._latest_frame is not None:
                        log.info("Camera auto-reset recovered fresh frame.")
                        return True, self._latest_frame.copy(), self._frame_id
            log.warning("Camera auto-reset did not produce fresh frame.")
        # Return failure instead of stale cached frame — honest about camera state
        return False, None, 0

    def subscribe(self, maxsize: int = 2) -> queue.Queue:
        """Create a subscription queue for continuous consumers."""
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        """Remove a subscription queue."""
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def run(self):
        self._running = True
        while self._running:
            if self._cap is None or not self._cap.isOpened():
                time.sleep(0.1)
                continue
            ret, frame = self._cap.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue
            if CAMERA_FLIP:
                frame = cv2.flip(frame, -1)
            with self._lock:
                self._latest_frame = frame
                self._frame_id += 1
                self._last_frame_time = time.time()
            fid = self._frame_id
            for q in self._subscribers:
                try:
                    q.put_nowait((frame, fid))
                except queue.Full:
                    pass

    @property
    def camera_ok(self) -> bool:
        """True if camera is producing fresh frames (updated within last 5s)."""
        with self._lock:
            if self._latest_frame is None:
                return False
            return (time.time() - self._last_frame_time) < 5.0

    def _find_camera_usb_ids(self) -> list[str]:
        """Scan /sys/bus/usb/devices for video-class USB devices (camera).

        Returns list of USB device IDs (e.g. ['1-2.1']) that expose a
        video4linux interface. This replaces the hardcoded '1-1' path.
        """
        import subprocess
        usb_path = Path("/sys/bus/usb/devices")
        if not usb_path.exists():
            return []
        camera_ids = []
        for dev_dir in usb_path.iterdir():
            if not dev_dir.is_dir() or dev_dir.name.startswith("usb"):
                continue
            # Check if this device or its interfaces have a video4linux child
            for iface in dev_dir.glob("*/video4linux"):
                camera_ids.append(dev_dir.name)
                break
            else:
                # Also check direct video4linux child (some device trees)
                if (dev_dir / "video4linux").exists():
                    camera_ids.append(dev_dir.name)
        if camera_ids:
            log.info(f"Found camera USB device(s): {camera_ids}")
        else:
            log.warning("No USB video devices found in /sys/bus/usb/devices")
        return camera_ids

    def reset_camera(self) -> bool:
        """Release and re-open the camera to fix frozen frames.

        Dynamically finds the camera USB device and performs unbind/rebind
        to recover from autosuspend freezes.
        """
        with self._lock:
            old_cap = self._cap
            if old_cap is not None:
                old_cap.release()
                log.info("Released old camera capture")
                self._cap = None
            self._latest_frame = None  # Clear stale frame immediately
            self._frame_id = 0  # Reset so next real frame is detected as fresh

        # Find camera USB device dynamically instead of hardcoding
        import subprocess
        usb_ids = self._find_camera_usb_ids()
        if not usb_ids:
            # Fallback: try common paths
            usb_ids = ["1-1", "1-2", "1-2.1", "1-2.2"]
            log.warning(f"No camera USB device found, trying fallback IDs: {usb_ids}")

        unbind = Path("/sys/bus/usb/drivers/usb/unbind")
        bind = Path("/sys/bus/usb/drivers/usb/bind")
        for usb_id in usb_ids:
            try:
                subprocess.run(["sudo", "sh", "-c", f"echo {usb_id} > {unbind}"],
                               timeout=3, capture_output=True)
                time.sleep(1.0)
                subprocess.run(["sudo", "sh", "-c", f"echo {usb_id} > {bind}"],
                               timeout=3, capture_output=True)
                log.info(f"USB unbind/rebind for {usb_id}")
            except Exception as e:
                log.warning(f"USB rebind failed for {usb_id}: {e}")

        # Pause for USB device to settle
        time.sleep(2.0)

        # Re-init camera using hardware helper
        from hardware import init_camera
        new_cap = init_camera()
        if new_cap is None:
            log.error("Camera reset failed — could not re-open camera")
            return False

        with self._lock:
            self._cap = new_cap
            self._latest_frame = None  # Force fresh frame
            log.info("Camera reset successful — new capture active")
        return True

    def stop(self):
        self._running = False


# -----------------------------------------------------------------------------
# CV State & Detection
# -----------------------------------------------------------------------------

@dataclasses.dataclass
class FaceDetection:
    x: int
    y: int
    w: int
    h: int
    center_x: float  # Normalized 0-1
    center_y: float  # Normalized 0-1
    confidence: float = 0.0


class CVState:
    """Thread-safe container for computer vision results."""

    PRESENCE_WINDOW = 30.0

    def __init__(self):
        self._lock = threading.Lock()
        self.faces: list[FaceDetection] = []
        self.face_count: int = 0
        self.motion_regions: list[tuple[int, int, int, int]] = []
        self.motion_detected: bool = False
        self.current_target: Optional[FaceDetection] = None
        self.last_update: float = 0.0
        self.fps: float = 0.0
        self.frame_id: int = 0
        self._presence_log: dict[str, deque] = {}
        self._total_frames_in_window: int = 0
        self._frame_times: deque = deque()

    def update(self, faces: list[FaceDetection],
               motion_regions: list[tuple[int, int, int, int]],
               frame_id: int, fps: float):
        with self._lock:
            self.faces = faces
            self.face_count = len(faces)
            self.motion_regions = motion_regions
            self.motion_detected = len(motion_regions) > 0
            self.current_target = (
                max(faces, key=lambda f: f.w * f.h) if faces else None
            )
            self.last_update = time.time()
            self.fps = fps
            self.frame_id = frame_id

    def update_presence(self, detections: list[dict]):
        """Update rolling presence history from YOLO detections."""
        now = time.time()
        cutoff = now - self.PRESENCE_WINDOW
        with self._lock:
            self._frame_times.append(now)
            while self._frame_times and self._frame_times[0] < cutoff:
                self._frame_times.popleft()

            seen_this_frame = set()
            for det in detections:
                name = det.get("class_name", "unknown")
                seen_this_frame.add(name)

            all_classes = set(self._presence_log.keys()) | seen_this_frame
            for cls in all_classes:
                if cls not in self._presence_log:
                    self._presence_log[cls] = deque()
                self._presence_log[cls].append((now, cls in seen_this_frame))
                while self._presence_log[cls] and self._presence_log[cls][0][0] < cutoff:
                    self._presence_log[cls].popleft()
                if not self._presence_log[cls]:
                    del self._presence_log[cls]

    def _get_presence_unlocked(self) -> dict:
        """Get presence percentages. Caller must hold _lock."""
        total = len(self._frame_times)
        if total == 0:
            return {}
        result = {}
        for cls, plog in self._presence_log.items():
            seen_count = sum(1 for _, present in plog if present)
            pct = round(seen_count / total * 100, 1)
            if pct > 0:
                result[cls] = pct
        return dict(sorted(result.items(), key=lambda x: -x[1]))

    def get_presence(self) -> dict:
        with self._lock:
            return self._get_presence_unlocked()

    def snapshot(self) -> dict:
        with self._lock:
            target = None
            if self.current_target:
                t = self.current_target
                target = {
                    "type": "person",
                    "cx": float(t.center_x),
                    "cy": float(t.center_y),
                    "w": int(t.w),
                    "h": int(t.h),
                }
            presence = self._get_presence_unlocked()
            return {
                "face_count": self.face_count,
                "faces": [
                    {"x": int(f.x), "y": int(f.y), "w": int(f.w), "h": int(f.h),
                     "cx": float(f.center_x), "cy": float(f.center_y),
                     "confidence": round(float(f.confidence), 3)}
                    for f in self.faces
                ],
                "motion_detected": self.motion_detected,
                "motion_region_count": len(self.motion_regions),
                "current_target": target,
                "fps": round(self.fps, 1),
                "frame_id": self.frame_id,
                "presence_30s": presence,
            }

    def has_target(self) -> bool:
        with self._lock:
            return self.current_target is not None or self.motion_detected


# -----------------------------------------------------------------------------
# CV Pipeline
# -----------------------------------------------------------------------------

class CVPipeline(threading.Thread):
    """Background CV: YOLO v8 nano for multi-object detection + MOG2 motion."""

    TRACK_CLASSES = {0: "person", 15: "cat", 16: "dog"}
    COCO_NAMES = {
        0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 4: "airplane",
        5: "bus", 6: "train", 7: "truck", 8: "boat", 9: "traffic light",
        10: "fire hydrant", 11: "stop sign", 12: "parking meter", 13: "bench",
        14: "bird", 15: "cat", 16: "dog", 17: "horse", 18: "sheep",
        19: "cow", 20: "elephant", 21: "bear", 22: "zebra", 23: "giraffe",
        24: "backpack", 25: "umbrella", 26: "handbag", 27: "tie",
        28: "suitcase", 29: "frisbee", 30: "skis", 31: "snowboard",
        32: "sports ball", 33: "kite", 34: "baseball bat", 35: "baseball glove",
        36: "skateboard", 37: "surfboard", 38: "tennis racket", 39: "bottle",
        40: "wine glass", 41: "cup", 42: "fork", 43: "knife", 44: "spoon",
        45: "bowl", 46: "banana", 47: "apple", 48: "sandwich", 49: "orange",
        50: "broccoli", 51: "carrot", 52: "hot dog", 53: "pizza", 54: "donut",
        55: "cake", 56: "chair", 57: "couch", 58: "potted plant", 59: "bed",
        60: "dining table", 61: "toilet", 62: "tv", 63: "laptop", 64: "mouse",
        65: "remote", 66: "keyboard", 67: "cell phone", 68: "microwave",
        69: "oven", 70: "toaster", 71: "sink", 72: "refrigerator", 73: "book",
        74: "clock", 75: "vase", 76: "scissors", 77: "teddy bear",
        78: "hair drier", 79: "toothbrush",
    }

    def __init__(self, frame_queue: queue.Queue, cv_state: CVState,
                 gimbal_arbiter=None):
        super().__init__(daemon=True)
        self._queue = frame_queue
        self._cv_state = cv_state
        self._gimbal_arbiter = gimbal_arbiter
        self._running = False

        try:
            from ultralytics import YOLO
            self._model = YOLO("yolov8n.pt")
            self._use_yolo = True
            log.info("CV: YOLO v8 nano loaded")
        except Exception as e:
            log.warning(f"CV: YOLO failed ({e}), falling back to Haar")
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            self._face_cascade = cv2.CascadeClassifier(cascade_path)
            self._use_yolo = False

        self._bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=200, varThreshold=40, detectShadows=False
        )

        self._motion_suppress_until = 0.0
        self._frame_counter = 0
        self._fps_counter = 0
        self._fps_timer = time.time()
        self._current_fps = 0.0

        self._detections: list[dict] = []
        self._detections_lock = threading.Lock()

    def suppress_motion(self, duration_s: float = CV_MOTION_SUPPRESS_S):
        self._motion_suppress_until = time.time() + duration_s

    def get_detections(self) -> list[dict]:
        """Get all YOLO detections for annotation."""
        with self._detections_lock:
            return list(self._detections)

    def run(self):
        self._running = True
        log.info("CV pipeline running (YOLO + arbiter tick)")
        while self._running:
            try:
                frame, frame_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            self._frame_counter += 1
            if self._frame_counter % CV_PROCESS_EVERY_N != 0:
                continue

            h, w = frame.shape[:2]
            faces = []
            all_dets = []

            if self._use_yolo:
                results = self._model(frame, verbose=False, conf=0.5, imgsz=320)
                for r in results:
                    for box in r.boxes:
                        cls_id = int(box.cls[0])
                        conf = float(box.conf[0])
                        x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
                        x1, y1 = max(0, x1), max(0, y1)
                        x2, y2 = min(w, x2), min(h, y2)
                        bw, bh = x2 - x1, y2 - y1
                        cls_name = self.COCO_NAMES.get(cls_id, f"cls{cls_id}")
                        det = {
                            "class_id": cls_id, "class_name": cls_name,
                            "confidence": round(conf, 3),
                            "x": int(x1), "y": int(y1), "w": int(bw), "h": int(bh),
                            "cx": float((x1 + bw / 2) / w),
                            "cy": float((y1 + bh / 2) / h),
                        }
                        all_dets.append(det)
                        if cls_id == 0 and bw > 10 and bh > 10:
                            head_cy = (y1 + bh * 0.2) / h
                            faces.append(FaceDetection(
                                x=x1, y=y1, w=bw, h=bh,
                                center_x=(x1 + bw / 2) / w,
                                center_y=head_cy,
                                confidence=conf,
                            ))
            else:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                raw = self._face_cascade.detectMultiScale(
                    gray, scaleFactor=1.2, minNeighbors=4,
                    minSize=(40, 40), maxSize=(300, 300))
                for (fx, fy, fw, fh) in (raw if len(raw) > 0 else []):
                    cx = (fx + fw / 2) / w
                    cy = (fy + fh / 2) / h
                    if (cx < 0.15 and cy < 0.15) or (cx > 0.85 and cy < 0.15) or \
                       (cx < 0.15 and cy > 0.85) or (cx > 0.85 and cy > 0.85):
                        continue
                    faces.append(FaceDetection(
                        x=int(fx), y=int(fy), w=int(fw), h=int(fh),
                        center_x=cx, center_y=cy,
                    ))

            with self._detections_lock:
                self._detections = all_dets

            self._cv_state.update_presence(all_dets)

            # Motion detection
            motion_regions: list[tuple[int, int, int, int]] = []
            if time.time() > self._motion_suppress_until:
                fg_mask = self._bg_sub.apply(frame)
                _, thresh = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)
                contours, _ = cv2.findContours(
                    thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                for c in contours:
                    if cv2.contourArea(c) > CV_MOTION_MIN_AREA:
                        motion_regions.append(cv2.boundingRect(c))
            else:
                self._bg_sub.apply(frame, learningRate=0.1)

            # FPS
            self._fps_counter += 1
            elapsed = time.time() - self._fps_timer
            if elapsed > 1.0:
                self._current_fps = self._fps_counter / elapsed
                self._fps_counter = 0
                self._fps_timer = time.time()

            self._cv_state.update(faces, motion_regions, frame_id,
                                  self._current_fps)

            # Tick gimbal arbiter
            if self._gimbal_arbiter:
                try:
                    self._gimbal_arbiter.tick()
                except Exception as e:
                    log.error(f"Arbiter tick error: {e}")

    def stop(self):
        self._running = False
