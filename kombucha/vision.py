"""Vision module for Kombucha v2.

Handles camera initialization, frame capture, frame delta computation,
self-model error calculation, sentry mode with motion detection,
YOLO object detection, and centroid tracking.
"""

import asyncio
import base64
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from kombucha.config import CameraConfig, MotionConfig
from kombucha.schemas import SceneObject, SceneState

log = logging.getLogger("kombucha.vision")

# Optional imports — guarded for testing without hardware
try:
    import cv2
    import numpy as np
    HAS_VISION = True
except ImportError:
    HAS_VISION = False

try:
    import ncnn
    HAS_NCNN = True
except ImportError:
    HAS_NCNN = False


def init_camera(config: CameraConfig):
    """Initialize the USB camera with fallback index probing."""
    if not HAS_VISION:
        log.warning("OpenCV not available, camera disabled")
        return None

    cap = None
    for idx in (config.device_index, 0, 1, 2):
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if cap.isOpened():
            break
        cap.release()
        cap = cv2.VideoCapture(idx)
        if cap.isOpened():
            break
        cap.release()
        cap = None
        log.warning(f"Camera index {idx} failed, trying next...")

    if cap is None or not cap.isOpened():
        log.error("Failed to open camera on any index")
        return None

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.resolution_w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.resolution_h)

    # Warm up — let auto-exposure settle
    for _ in range(config.warmup_frames):
        cap.read()

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    log.info(f"Camera ready: {w}x{h}")
    return cap


def capture_frame_b64(cap, config: CameraConfig, tick_count: int = 0,
                      frame_log_dir: Optional[str] = None) -> str:
    """Capture a frame, optionally save to disk, return base64 JPEG."""
    if not HAS_VISION or cap is None:
        raise RuntimeError("Camera not available")

    # Drain stale buffered frames
    for _ in range(config.drain_frames):
        cap.grab()

    ret, frame = cap.read()
    if not ret or frame is None:
        raise RuntimeError("Camera capture returned empty frame")

    _, jpeg_buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, config.jpeg_quality])
    jpeg_bytes = jpeg_buf.tobytes()

    if frame_log_dir:
        try:
            fld = Path(frame_log_dir)
            fld.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            frame_path = fld / f"tick_{tick_count:05d}_{ts}.jpg"
            frame_path.write_bytes(jpeg_bytes)
            _prune_frame_log(fld, config.frame_log_max)
        except Exception as e:
            log.warning(f"Frame log write failed: {e}")

    return base64.b64encode(jpeg_bytes).decode()


def _prune_frame_log(frame_log_dir: Path, max_frames: int):
    """Keep only the most recent max_frames frames."""
    try:
        frames = sorted(frame_log_dir.glob("tick_*.jpg"))
        if len(frames) > max_frames:
            for old in frames[:-max_frames]:
                old.unlink()
    except Exception:
        pass


def compute_frame_delta(prev_frame_b64: Optional[str],
                        curr_frame_b64: Optional[str]) -> Optional[float]:
    """Compute normalized pixel difference between two frames.

    Returns a float 0.0 (identical) to 1.0 (completely different).
    """
    if not prev_frame_b64 or not curr_frame_b64:
        return None
    if not HAS_VISION:
        return None
    try:
        prev = cv2.imdecode(
            np.frombuffer(base64.b64decode(prev_frame_b64), np.uint8),
            cv2.IMREAD_GRAYSCALE,
        )
        curr = cv2.imdecode(
            np.frombuffer(base64.b64decode(curr_frame_b64), np.uint8),
            cv2.IMREAD_GRAYSCALE,
        )
        diff = cv2.absdiff(prev, curr)
        return float(np.mean(diff)) / 255.0
    except Exception:
        return None


def compute_basic_self_model_error(prev_actions, prev_frame_b64, curr_frame_b64,
                                    motion_config: Optional[MotionConfig] = None) -> dict:
    """Basic self-model error using frame delta only (no ESP32 dependency)."""
    threshold = motion_config.frame_delta_threshold if motion_config else 0.015
    anomaly_thresh = motion_config.anomaly_threshold if motion_config else 0.08

    error = {
        "frame_delta": None,
        "drive_expected_motion": False,
        "look_expected_change": False,
        "motion_detected": False,
        "anomaly": False,
        "anomaly_reason": None,
    }
    delta = compute_frame_delta(prev_frame_b64, curr_frame_b64)
    if delta is not None:
        error["frame_delta"] = round(delta, 4)
        drive_commands = [
            a for a in (prev_actions or [])
            if isinstance(a, dict)
            and a.get("type") == "drive"
            and (abs(a.get("left", 0)) > 0.05 or abs(a.get("right", 0)) > 0.05)
        ]
        look_commands = [
            a for a in (prev_actions or [])
            if isinstance(a, dict) and a.get("type") == "look"
        ]
        expected_motion = drive_commands or look_commands
        if look_commands:
            error["look_expected_change"] = True
        if drive_commands:
            error["drive_expected_motion"] = True
            error["motion_detected"] = delta > threshold
            if not error["motion_detected"] and not look_commands:
                error["anomaly"] = True
                error["anomaly_reason"] = "drive_commanded_no_motion_detected"
        if not expected_motion and delta > anomaly_thresh:
            error["anomaly"] = True
            error["anomaly_reason"] = "no_drive_but_significant_motion"
    return error


def compute_self_model_error(prev_actions, prev_frame_b64, curr_frame_b64,
                              prev_pan=None, curr_pan=None,
                              prev_tilt=None, curr_tilt=None,
                              motion_config: Optional[MotionConfig] = None) -> dict:
    """Full self-model error: frame delta + gimbal position feedback."""
    error = compute_basic_self_model_error(
        prev_actions, prev_frame_b64, curr_frame_b64, motion_config
    )
    if prev_pan is not None and curr_pan is not None:
        look_commands = [a for a in (prev_actions or [])
                         if isinstance(a, dict) and a.get("type") == "look"]
        if look_commands:
            expected_pan = look_commands[-1].get("pan", prev_pan)
            error["gimbal_error_pan"] = abs(expected_pan - curr_pan)
            if error["gimbal_error_pan"] > 15:
                error["anomaly"] = True
                reason = error.get("anomaly_reason") or ""
                error["anomaly_reason"] = (reason + " gimbal_pan_error").strip()
    if prev_tilt is not None and curr_tilt is not None:
        look_commands = [a for a in (prev_actions or [])
                         if isinstance(a, dict) and a.get("type") == "look"]
        if look_commands:
            expected_tilt = look_commands[-1].get("tilt", prev_tilt)
            error["gimbal_error_tilt"] = abs(expected_tilt - curr_tilt)
            if error["gimbal_error_tilt"] > 15:
                error["anomaly"] = True
                reason = error.get("anomaly_reason") or ""
                error["anomaly_reason"] = (reason + " gimbal_tilt_error").strip()
    return error


async def sentry_sleep(cap, duration_s: float, state: dict,
                       motion_threshold: float = 0.03,
                       tertiary_fn=None) -> str:
    """Sleep for duration_s with motion detection.

    Returns 'motion_detected' or 'timeout'.
    """
    if not HAS_VISION or cap is None:
        await asyncio.sleep(duration_s)
        return "timeout"

    # Tertiary loop trigger
    if tertiary_fn:
        last_tertiary = state.get("last_tertiary_time", 0)
        if time.time() - last_tertiary > 300:
            state["last_tertiary_time"] = time.time()
            asyncio.create_task(tertiary_fn())
        else:
            elapsed = int(time.time() - last_tertiary)
            log.info(f"  [TERTIARY] Cooldown: {300 - elapsed}s remaining, skipping.")

    prev_gray = None
    deadline = time.time() + duration_s

    while time.time() < deadline:
        await asyncio.sleep(1.0)
        ret, frame = cap.read()
        if not ret or frame is None:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        if prev_gray is None:
            prev_gray = gray
            continue
        delta = cv2.absdiff(prev_gray, gray)
        prev_gray = gray
        thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)[1]
        motion_pct = np.count_nonzero(thresh) / thresh.size
        if motion_pct > motion_threshold:
            log.info(f"  MOTION detected ({motion_pct:.1%}), waking up")
            state["wake_reason"] = "motion_detected"
            return "motion_detected"

    return "timeout"


# ===========================================================================
# YOLO Object Detection (NCNN backend)
# ===========================================================================

COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]

CLASSES_OF_INTEREST = {
    "person", "cat", "dog", "chair", "door", "cup", "bottle",
    "laptop", "cell phone", "book", "backpack", "bed", "couch",
    "dining table", "potted plant", "tv", "remote",
}


@dataclass
class Detection:
    cls: str
    confidence: float
    bbox: tuple  # (x1, y1, x2, y2) in pixel coords
    centroid: tuple  # (cx, cy)
    area: float


class YOLODetector:
    """YOLOv8-nano detector using NCNN backend for Pi 5."""

    def __init__(self, model_path: str, input_size: int = 320,
                 conf_threshold: float = 0.25, nms_threshold: float = 0.45):
        if not HAS_NCNN:
            raise RuntimeError("ncnn not available")
        if not HAS_VISION:
            raise RuntimeError("OpenCV not available")

        self.input_size = input_size
        self.conf_threshold = conf_threshold
        self.nms_threshold = nms_threshold

        self.net = ncnn.Net()
        self.net.opt.use_vulkan_compute = False
        self.net.opt.num_threads = 4
        self.net.load_param(f"{model_path}.param")
        self.net.load_model(f"{model_path}.bin")
        log.info(f"YOLO detector loaded: {model_path}")

    def _letterbox(self, frame):
        """Resize with letterboxing to maintain aspect ratio."""
        h, w = frame.shape[:2]
        scale = min(self.input_size / w, self.input_size / h)
        new_w, new_h = int(w * scale), int(h * scale)
        resized = cv2.resize(frame, (new_w, new_h))

        padded = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        dx = (self.input_size - new_w) // 2
        dy = (self.input_size - new_h) // 2
        padded[dy:dy + new_h, dx:dx + new_w] = resized
        return padded, scale, dx, dy

    def detect(self, frame) -> list:
        """Run detection on a BGR frame. Returns list of Detection objects."""
        if frame is None:
            return []

        orig_h, orig_w = frame.shape[:2]
        padded, scale, dx, dy = self._letterbox(frame)

        # Create NCNN mat from padded image
        mat_in = ncnn.Mat.from_pixels(
            padded, ncnn.Mat.PixelType.PIXEL_BGR2RGB,
            self.input_size, self.input_size,
        )

        # Normalize to 0-1
        norm_vals = [1 / 255.0, 1 / 255.0, 1 / 255.0]
        mat_in.substract_mean_normalize([], norm_vals)

        # Run inference
        ex = self.net.create_extractor()
        ex.input("in0", mat_in)
        _, mat_out = ex.extract("out0")

        # Parse output: (num_detections, 4+num_classes) or (4+num_classes, num_detections)
        # YOLOv8 NCNN output shape varies — handle both
        if mat_out.w > mat_out.h:
            # Shape: (num_dets, 4+80)
            num_dets = mat_out.h
            out_data = np.array(mat_out).reshape(num_dets, -1)
        else:
            # Shape: (4+80, num_dets) — transpose
            out_data = np.array(mat_out).T

        detections = []
        boxes = []
        scores = []
        class_ids = []

        for row in out_data:
            # First 4 values: cx, cy, w, h (in input coords)
            cx, cy, bw, bh = row[:4]
            class_scores = row[4:]
            class_id = int(np.argmax(class_scores))
            confidence = float(class_scores[class_id])

            if confidence < self.conf_threshold:
                continue
            if class_id >= len(COCO_CLASSES):
                continue

            cls_name = COCO_CLASSES[class_id]
            if cls_name not in CLASSES_OF_INTEREST:
                continue

            # Convert from input coords to original image coords
            x1 = (cx - bw / 2 - dx) / scale
            y1 = (cy - bh / 2 - dy) / scale
            x2 = (cx + bw / 2 - dx) / scale
            y2 = (cy + bh / 2 - dy) / scale

            # Clamp to image bounds
            x1 = max(0, min(x1, orig_w))
            y1 = max(0, min(y1, orig_h))
            x2 = max(0, min(x2, orig_w))
            y2 = max(0, min(y2, orig_h))

            boxes.append([x1, y1, x2 - x1, y2 - y1])
            scores.append(confidence)
            class_ids.append((class_id, cls_name, (x1, y1, x2, y2)))

        # NMS
        if boxes:
            indices = cv2.dnn.NMSBoxes(boxes, scores, self.conf_threshold, self.nms_threshold)
            if len(indices) > 0:
                for i in indices:
                    idx = i if isinstance(i, int) else i[0]
                    class_id, cls_name, (x1, y1, x2, y2) = class_ids[idx]
                    det_cx = (x1 + x2) / 2
                    det_cy = (y1 + y2) / 2
                    area = (x2 - x1) * (y2 - y1)
                    detections.append(Detection(
                        cls=cls_name,
                        confidence=scores[idx],
                        bbox=(x1, y1, x2, y2),
                        centroid=(det_cx, det_cy),
                        area=area,
                    ))

        return detections


# ===========================================================================
# Centroid Tracker
# ===========================================================================

@dataclass
class TrackedObject:
    track_id: int
    cls: str
    centroid: tuple
    bbox: tuple
    confidence: float
    area: float
    age: int = 1  # frames tracked
    disappeared: int = 0
    prev_centroids: list = field(default_factory=list)


class CentroidTracker:
    """Assign persistent IDs to detections across frames using centroid distance."""

    def __init__(self, max_disappeared: int = 10, max_distance: float = 80.0):
        self._next_id = 0
        self._objects: OrderedDict[int, TrackedObject] = OrderedDict()
        self.max_disappeared = max_disappeared
        self.max_distance = max_distance

    def _register(self, detection: Detection) -> int:
        obj = TrackedObject(
            track_id=self._next_id,
            cls=detection.cls,
            centroid=detection.centroid,
            bbox=detection.bbox,
            confidence=detection.confidence,
            area=detection.area,
            prev_centroids=[detection.centroid],
        )
        self._objects[self._next_id] = obj
        self._next_id += 1
        return obj.track_id

    def _deregister(self, track_id: int):
        del self._objects[track_id]

    def update(self, detections: list) -> list:
        """Update tracker with new detections. Returns list of TrackedObjects."""
        # No detections — increment disappeared for all
        if len(detections) == 0:
            for track_id in list(self._objects.keys()):
                self._objects[track_id].disappeared += 1
                if self._objects[track_id].disappeared > self.max_disappeared:
                    self._deregister(track_id)
            return list(self._objects.values())

        # No existing objects — register all
        if len(self._objects) == 0:
            for det in detections:
                self._register(det)
            return list(self._objects.values())

        # Match existing objects to new detections by centroid distance
        object_ids = list(self._objects.keys())
        object_centroids = [self._objects[oid].centroid for oid in object_ids]
        det_centroids = [d.centroid for d in detections]

        # Compute distance matrix
        dist_matrix = np.zeros((len(object_centroids), len(det_centroids)))
        for i, oc in enumerate(object_centroids):
            for j, dc in enumerate(det_centroids):
                dist_matrix[i, j] = math.dist(oc, dc)

        # Greedy assignment: closest pairs first
        used_rows = set()
        used_cols = set()
        matched = []

        # Sort by distance
        flat_indices = np.argsort(dist_matrix, axis=None)
        for flat_idx in flat_indices:
            row = int(flat_idx // dist_matrix.shape[1])
            col = int(flat_idx % dist_matrix.shape[1])
            if row in used_rows or col in used_cols:
                continue
            if dist_matrix[row, col] > self.max_distance:
                break
            matched.append((row, col))
            used_rows.add(row)
            used_cols.add(col)

        # Update matched objects
        for row, col in matched:
            track_id = object_ids[row]
            det = detections[col]
            obj = self._objects[track_id]
            obj.prev_centroids.append(obj.centroid)
            if len(obj.prev_centroids) > 10:
                obj.prev_centroids = obj.prev_centroids[-10:]
            obj.centroid = det.centroid
            obj.bbox = det.bbox
            obj.confidence = det.confidence
            obj.area = det.area
            obj.cls = det.cls
            obj.age += 1
            obj.disappeared = 0

        # Mark unmatched existing objects as disappeared
        for row in range(len(object_ids)):
            if row not in used_rows:
                track_id = object_ids[row]
                self._objects[track_id].disappeared += 1
                if self._objects[track_id].disappeared > self.max_disappeared:
                    self._deregister(track_id)

        # Register unmatched new detections
        for col in range(len(detections)):
            if col not in used_cols:
                self._register(detections[col])

        return list(self._objects.values())

    @property
    def tracked_objects(self) -> list:
        return list(self._objects.values())


# ===========================================================================
# Scene Builder Helpers
# ===========================================================================

def estimate_distance(bbox: tuple, frame_height: int = 480,
                      ref_height_px: float = 300.0,
                      ref_distance_m: float = 1.5) -> float:
    """Estimate distance to an object based on bounding box height.

    Uses a simple pinhole camera model: distance is inversely proportional
    to apparent height in pixels.
    """
    _, y1, _, y2 = bbox
    obj_height = abs(y2 - y1)
    if obj_height < 5:
        return 10.0  # far away / too small
    return round(ref_distance_m * ref_height_px / obj_height, 2)


def pixel_to_bearing(cx: float, frame_width: int = 640,
                     fov_deg: float = 160.0) -> float:
    """Convert pixel x-coordinate to bearing in degrees from center.

    Returns negative for left, positive for right.
    """
    normalized = (cx - frame_width / 2) / (frame_width / 2)
    return round(normalized * (fov_deg / 2), 1)


def classify_motion(tracked: TrackedObject) -> str:
    """Classify tracked object motion state from centroid history."""
    if len(tracked.prev_centroids) < 3:
        return "new"

    recent = tracked.prev_centroids[-3:]
    dx = recent[-1][0] - recent[0][0]
    dy = recent[-1][1] - recent[0][1]
    total_movement = math.sqrt(dx * dx + dy * dy)

    if total_movement < 5:
        return "stationary"

    # Check if object is getting bigger (approaching) or smaller (receding)
    # by looking at area change — but we only have centroids here,
    # so use vertical movement as a proxy (lower in frame = closer)
    if dy > 10:
        return "approaching"
    elif dy < -10:
        return "receding"
    return "moving"


def estimate_light_level(frame_b64: str) -> Optional[str]:
    """Estimate ambient light level from frame brightness."""
    if not HAS_VISION:
        return None
    try:
        import base64 as _b64
        raw = cv2.imdecode(
            np.frombuffer(_b64.b64decode(frame_b64), np.uint8),
            cv2.IMREAD_GRAYSCALE,
        )
        if raw is None:
            return None
        mean_brightness = float(np.mean(raw))
        if mean_brightness < 30:
            return "dark"
        elif mean_brightness < 80:
            return "dim"
        elif mean_brightness < 180:
            return "normal"
        else:
            return "bright"
    except Exception:
        return None


def build_scene_state(frame_b64: str, frame_delta: Optional[float],
                      tracked_objects: list,
                      motion_threshold: float = 0.03,
                      frame_width: int = 640,
                      frame_height: int = 480,
                      frame_delta_history: Optional[list] = None) -> SceneState:
    """Build a SceneState from tracked objects and frame data."""
    frame_area = frame_width * frame_height
    objects = []
    person_count = 0
    nearest_obstacle_cm = None

    for t in tracked_objects:
        if t.disappeared > 0:
            continue  # skip objects not seen this frame

        size_pct = round(t.area / frame_area, 4) if frame_area > 0 else 0
        dist_m = estimate_distance(t.bbox, frame_height)
        obj = SceneObject(
            cls=t.cls,
            track_id=t.track_id,
            confidence=round(t.confidence, 3),
            bbox=t.bbox,
            centroid=t.centroid,
            size_pct=size_pct,
            distance_est_m=dist_m,
            bearing_deg=pixel_to_bearing(t.centroid[0], frame_width),
            frames_tracked=t.age,
            state=classify_motion(t),
        )
        objects.append(obj)
        if t.cls == "person":
            person_count += 1
        else:
            # Track nearest non-person obstacle
            dist_cm = dist_m * 100
            if nearest_obstacle_cm is None or dist_cm < nearest_obstacle_cm:
                nearest_obstacle_cm = round(dist_cm, 1)

    # Frame delta rolling stats
    frame_delta_avg = None
    frame_delta_max = None
    if frame_delta_history:
        recent = [d for d in frame_delta_history if d is not None]
        if recent:
            frame_delta_avg = round(sum(recent) / len(recent), 4)
            frame_delta_max = round(max(recent), 4)

    light_level = estimate_light_level(frame_b64) if frame_b64 else None

    return SceneState(
        timestamp=datetime.now().isoformat(),
        frame_delta=frame_delta,
        frame_delta_avg=frame_delta_avg,
        frame_delta_max=frame_delta_max,
        motion_detected=(frame_delta or 0) > motion_threshold,
        objects=objects,
        person_count=person_count,
        nearest_obstacle_cm=nearest_obstacle_cm,
        light_level=light_level,
        frame_b64=frame_b64,
    )
