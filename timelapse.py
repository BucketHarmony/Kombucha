"""
timelapse.py — Second camera time-lapse recorder.

Captures one frame every N seconds from the Logitech C270 (or any secondary camera).
Saves timestamped JPEGs for post-production wide-angle coverage.
Also serves as a backup eye when the primary gimbal camera freezes.
"""

import cv2
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

TIMELAPSE_DIR = Path("/opt/kombucha/media/timelapse")


class TimeLapseRecorder(threading.Thread):
    """Background thread capturing periodic frames from a secondary camera."""

    def __init__(self, device="/dev/video0", interval_s=30, width=640, height=480):
        super().__init__(daemon=True)
        self._device = device
        self._interval = interval_s
        self._width = width
        self._height = height
        self._running = False
        self._frame_count = 0
        self._last_capture = 0.0
        self._cap = None
        self._lock = threading.Lock()
        self._latest_frame = None

        TIMELAPSE_DIR.mkdir(parents=True, exist_ok=True)

    def _open_camera(self):
        """Open the secondary camera."""
        try:
            cap = cv2.VideoCapture(self._device, cv2.CAP_V4L2)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                # Warm up
                for _ in range(3):
                    cap.read()
                log.info(f"Timelapse camera opened: {self._device}")
                return cap
            cap.release()
        except Exception as e:
            log.warning(f"Timelapse camera failed: {e}")
        return None

    def get_latest_frame(self):
        """Get the most recent timelapse frame (for backup/pip use)."""
        with self._lock:
            if self._latest_frame is not None:
                return True, self._latest_frame.copy()
        return False, None

    def run(self):
        self._running = True
        log.info(f"Timelapse recorder started: {self._device} every {self._interval}s")

        self._cap = self._open_camera()
        if not self._cap:
            log.error("Timelapse: could not open camera, stopping")
            return

        while self._running:
            now = time.time()
            if now - self._last_capture < self._interval:
                # Between captures, still grab frames to keep buffer fresh
                if self._cap and self._cap.isOpened():
                    self._cap.grab()
                time.sleep(1)
                continue

            try:
                if not self._cap or not self._cap.isOpened():
                    self._cap = self._open_camera()
                    if not self._cap:
                        time.sleep(10)
                        continue

                ret, frame = self._cap.read()
                if not ret or frame is None:
                    time.sleep(5)
                    continue

                with self._lock:
                    self._latest_frame = frame

                # Save timestamped frame
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                date_dir = TIMELAPSE_DIR / datetime.now().strftime("%Y%m%d")
                date_dir.mkdir(exist_ok=True)
                path = date_dir / f"tl_{ts}.jpg"
                cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                self._frame_count += 1
                self._last_capture = now

                if self._frame_count % 10 == 0:
                    log.info(f"Timelapse: {self._frame_count} frames captured")

            except Exception as e:
                log.warning(f"Timelapse capture error: {e}")
                time.sleep(5)

    def get_status(self) -> dict:
        return {
            "device": self._device,
            "frames_captured": self._frame_count,
            "interval_s": self._interval,
            "last_capture_age_s": round(time.time() - self._last_capture, 1) if self._last_capture else None,
            "camera_ok": self._cap is not None and self._cap.isOpened() if self._cap else False,
        }

    def stop(self):
        self._running = False
        if self._cap:
            self._cap.release()
