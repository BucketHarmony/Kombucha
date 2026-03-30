"""
hardware.py - Hardware abstraction: constants, telemetry, serial, camera init.

Pure functions and data classes. No global state. All dependencies passed explicitly.
"""

import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Optional, Union

import cv2
import serial

log = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Configuration Constants
# -----------------------------------------------------------------------------

SERIAL_PORT = os.environ.get("KOMBUCHA_SERIAL", "/dev/ttyAMA0")
SERIAL_BAUD = 115200
CAMERA_DEVICE = os.environ.get("KOMBUCHA_CAMERA", "/dev/video1")
VIDEO_DIR = Path(os.environ.get("KOMBUCHA_VIDEO_DIR", Path.home() / "kombucha" / "video"))
WAKE_DIR = Path(os.environ.get("KOMBUCHA_WAKE_DIR", Path.home() / "kombucha" / "wake"))
CAPTURE_W = 640
CAPTURE_H = 480
JPEG_QUALITY = 85
CMD_DELAY = 0.05
CAMERA_FLIP = False

# Telemetry constants
TICKS_PER_METER = 1000.0
STUCK_TIMEOUT_S = 0.5
STUCK_ODOM_THRESHOLD = 2.0
BATTERY_MIN_V = 9.0
BATTERY_MAX_V = 12.6
BATTERY_CHARGING_V = 12.7
DRIVE_MAX_DURATION_MS = 5000
TELEMETRY_POLL_HZ = 50

# CV constants
CV_PROCESS_EVERY_N = 3
CV_MOTION_MIN_AREA = 2000
CV_MOTION_SUPPRESS_S = 2.0
CV_HYSTERESIS_S = 2.0
CV_MANUAL_TIMEOUT_S = 30.0
CV_QUEUE_MAX_DEPTH = 6
CV_QUEUE_STALE_S = 30.0
CV_DEAD_ZONE_PX = 30
CV_KP_PAN = 80.0
CV_KP_TILT = 40.0
CV_MAX_STEP_DEG = 6.0
CV_SMOOTHING = 0.5

# Heartbeat constants
HEARTBEAT_INTERVAL_S = 30.0

# ESP32 initialization commands
ESP32_INIT_CMDS = [
    {"T": 142, "cmd": 50},
    {"T": 131, "cmd": 1},
    {"T": 143, "cmd": 0},
    {"T": 4, "cmd": 2},
    {"T": 900, "main": 2, "module": 2},
]


# -----------------------------------------------------------------------------
# Utility
# -----------------------------------------------------------------------------

def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


# -----------------------------------------------------------------------------
# Telemetry State
# -----------------------------------------------------------------------------

class TelemetryState:
    """Thread-safe container for latest ESP32 sensor data."""

    def __init__(self):
        self._lock = threading.Lock()
        self.wheel_speed_l = 0.0
        self.wheel_speed_r = 0.0
        self.odl = 0.0
        self.odr = 0.0
        self.ax = 0.0
        self.ay = 0.0
        self.az = 0.0
        self.gx = 0.0
        self.gy = 0.0
        self.gz = 0.0
        self.mx = 0.0
        self.my = 0.0
        self.mz = 0.0
        self.gimbal_pan = 0.0
        self.gimbal_tilt = 0.0
        self.battery_v = 0.0
        self.servo_pan_status = 0
        self.servo_tilt_status = 0
        self.drive_commanded = False
        self.drive_command_time = 0.0
        self.odom_at_command_l = 0.0
        self.odom_at_command_r = 0.0
        self.odom_session_start_l = 0.0
        self.odom_session_start_r = 0.0
        self.last_update = 0.0

    def update_from_t1001(self, data: dict):
        """Update from T:1001 feedback (main telemetry)."""
        with self._lock:
            self.wheel_speed_l = float(data.get("L", self.wheel_speed_l))
            self.wheel_speed_r = float(data.get("R", self.wheel_speed_r))
            self.odl = float(data.get("odl", self.odl))
            self.odr = float(data.get("odr", self.odr))
            self.ax = float(data.get("ax", self.ax))
            self.ay = float(data.get("ay", self.ay))
            self.az = float(data.get("az", self.az))
            self.gx = float(data.get("gx", self.gx))
            self.gy = float(data.get("gy", self.gy))
            self.gz = float(data.get("gz", self.gz))
            if "v" in data:
                self.battery_v = round(float(data["v"]) / 100, 2)
            if "pan" in data:
                self.gimbal_pan = float(data["pan"])
            if "tilt" in data:
                self.gimbal_tilt = float(data["tilt"])
            self.last_update = time.time()

    def update_from_t1005(self, data: dict):
        """Update from T:1005 feedback (servo status)."""
        with self._lock:
            servo_id = data.get("id", 0)
            status = int(data.get("status", 0))
            if servo_id == 1:
                self.servo_pan_status = status
            elif servo_id == 2:
                self.servo_tilt_status = status

    def mark_drive_start(self):
        """Record state when a drive command is sent."""
        with self._lock:
            self.drive_commanded = True
            self.drive_command_time = time.time()
            self.odom_at_command_l = self.odl
            self.odom_at_command_r = self.odr

    def mark_drive_stop(self):
        """Clear drive tracking."""
        with self._lock:
            self.drive_commanded = False

    def snapshot(self) -> dict:
        """Return a plain dict copy of all fields."""
        with self._lock:
            return {
                "wheel_speed_l": self.wheel_speed_l,
                "wheel_speed_r": self.wheel_speed_r,
                "odl": self.odl,
                "odr": self.odr,
                "ax": self.ax,
                "ay": self.ay,
                "az": self.az,
                "gx": self.gx,
                "gy": self.gy,
                "gz": self.gz,
                "mx": self.mx,
                "my": self.my,
                "mz": self.mz,
                "gimbal_pan": self.gimbal_pan,
                "gimbal_tilt": self.gimbal_tilt,
                "battery_v": self.battery_v,
                "servo_pan_status": self.servo_pan_status,
                "servo_tilt_status": self.servo_tilt_status,
                "drive_commanded": self.drive_commanded,
                "drive_command_time": self.drive_command_time,
                "odom_at_command_l": self.odom_at_command_l,
                "odom_at_command_r": self.odom_at_command_r,
                "odom_session_start_l": self.odom_session_start_l,
                "odom_session_start_r": self.odom_session_start_r,
                "last_update": self.last_update,
            }

    def snapshot_session_start(self):
        """Record current odometry as session baseline."""
        with self._lock:
            self.odom_session_start_l = self.odl
            self.odom_session_start_r = self.odr


class TelemetryReader(threading.Thread):
    """Daemon thread that reads and parses ESP32 serial telemetry."""

    def __init__(self, ser: serial.Serial, telemetry: TelemetryState):
        super().__init__(daemon=True)
        self._ser = ser
        self._telemetry = telemetry
        self._running = False
        self._buffer = ""
        self._logged_first_packet = False

    def run(self):
        self._running = True
        while self._running:
            try:
                if self._ser and self._ser.is_open and self._ser.in_waiting:
                    raw = self._ser.read(self._ser.in_waiting)
                    self._buffer += raw.decode(errors="replace")
                    while "\n" in self._buffer:
                        line, self._buffer = self._buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            t = data.get("T")
                            if t == 1001:
                                if not self._logged_first_packet:
                                    log.info(f"T:1001 keys: {list(data.keys())}")
                                    self._logged_first_packet = True
                                self._telemetry.update_from_t1001(data)
                            elif t == 1005:
                                self._telemetry.update_from_t1005(data)
                        except (json.JSONDecodeError, ValueError):
                            pass
                else:
                    time.sleep(1.0 / TELEMETRY_POLL_HZ)
            except serial.SerialException:
                time.sleep(0.1)
            except Exception:
                time.sleep(0.1)

    def stop(self):
        self._running = False


# -----------------------------------------------------------------------------
# Hardware Initialization
# -----------------------------------------------------------------------------

def _disable_usb_autosuspend():
    """Disable USB autosuspend for all USB devices to prevent camera freezes."""
    usb_devices_path = Path("/sys/bus/usb/devices")
    if not usb_devices_path.exists():
        return
    for device_dir in usb_devices_path.iterdir():
        power_control = device_dir / "power" / "control"
        if power_control.exists():
            try:
                current = power_control.read_text().strip()
                if current == "auto":
                    power_control.write_text("on")
                    log.info(f"Disabled USB autosuspend for {device_dir.name}")
            except PermissionError:
                log.debug(f"Cannot set power/control for {device_dir.name} (no permission)")
            except OSError:
                pass


def init_camera() -> Optional[cv2.VideoCapture]:
    """Initialize camera using device path or index."""
    _disable_usb_autosuspend()
    devices_to_try = [CAMERA_DEVICE, "/dev/video0", "/dev/video1", "/dev/video2", 0, 1, 2, 3, 4, 5]

    for device in devices_to_try:
        if isinstance(device, str):
            cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        else:
            cap = cv2.VideoCapture(device)

        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_W)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_H)
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            for _ in range(5):
                cap.read()
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            log.info(f"Camera ready: {device}, {w}x{h}")
            return cap
        cap.release()

    log.error("Failed to open camera on any device")
    return None


def init_serial() -> Optional[serial.Serial]:
    """Initialize serial connection to ESP32."""
    try:
        ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=1.0)
        time.sleep(2.0)
        if ser.in_waiting:
            ser.read(ser.in_waiting)
        for cmd in ESP32_INIT_CMDS:
            ser.write((json.dumps(cmd) + "\n").encode())
            time.sleep(CMD_DELAY)
        log.info(f"Serial ready: {SERIAL_PORT} @ {SERIAL_BAUD}")
        return ser
    except serial.SerialException as e:
        log.warning(f"Serial init failed: {e}")
        return None


# -----------------------------------------------------------------------------
# T-Code Validation & Serial Communication
# -----------------------------------------------------------------------------

def validate_tcode(t_code: int, params: dict) -> Optional[dict]:
    """Validate and sanitize a T-code command. Returns None if invalid."""
    try:
        if t_code == 0:
            return {"T": 0}
        elif t_code == 1:
            return {
                "T": 1,
                "L": _clamp(float(params.get("L", 0)), -1.3, 1.3),
                "R": _clamp(float(params.get("R", 0)), -1.3, 1.3),
            }
        elif t_code == 3:
            return {
                "T": 3,
                "lineNum": int(_clamp(int(params.get("lineNum", 0)), 0, 3)),
                "Text": str(params.get("Text", ""))[:20],
            }
        elif t_code == -3:
            return {"T": -3}
        elif t_code == 132:
            return {
                "T": 132,
                "IO4": int(_clamp(int(params.get("IO4", 0)), 0, 255)),
                "IO5": int(_clamp(int(params.get("IO5", 0)), 0, 255)),
            }
        elif t_code == 133:
            return {
                "T": 133,
                "X": int(_clamp(int(params.get("X", 0)), -180, 180)),
                "Y": int(_clamp(int(params.get("Y", 0)), -30, 90)),
                "SPD": int(_clamp(int(params.get("SPD", 100)), 1, 200)),
                "ACC": int(_clamp(int(params.get("ACC", 10)), 1, 50)),
            }
        else:
            log.warning(f"Unknown T-code: {t_code}")
            return None
    except (ValueError, TypeError, KeyError) as e:
        log.warning(f"T-code {t_code} validation failed: {e}")
        return None


def send_tcode(ser: Optional[serial.Serial], cmd: dict,
               serial_lock: Optional[threading.Lock] = None) -> bool:
    """Send a T-code command to ESP32. Thread-safe if lock provided."""
    if ser is None or not ser.is_open:
        return False
    try:
        if serial_lock:
            with serial_lock:
                payload = json.dumps(cmd) + "\n"
                ser.write(payload.encode())
                time.sleep(CMD_DELAY)
        else:
            payload = json.dumps(cmd) + "\n"
            ser.write(payload.encode())
            time.sleep(CMD_DELAY)
        return True
    except serial.SerialException as e:
        log.error(f"Serial write error: {e}")
        return False


# -----------------------------------------------------------------------------
# Action Translation
# -----------------------------------------------------------------------------

def translate_action(action: Union[dict, str], body_state: dict) -> list[dict]:
    """Translate high-level action to list of T-code commands."""
    if not isinstance(action, dict):
        log.warning(f"Action is not a dict: {action!r}")
        return []

    action_type = action.get("type", "")
    results = []

    if action_type == "drive":
        left = float(action.get("left", 0))
        right = float(action.get("right", 0))
        cmd = validate_tcode(1, {"L": left, "R": right})
        if cmd:
            results.append(cmd)

    elif action_type == "stop":
        cmd = validate_tcode(0, {})
        if cmd:
            results.append(cmd)

    elif action_type == "look":
        pan = int(action.get("pan", 0))
        tilt = int(action.get("tilt", 0))
        spd = int(action.get("speed", 100))
        acc = int(action.get("accel", 10))
        cmd = validate_tcode(133, {"X": pan, "Y": tilt, "SPD": spd, "ACC": acc})
        if cmd:
            results.append(cmd)
            body_state["pan_position"] = _clamp(pan, -180, 180)
            body_state["tilt_position"] = _clamp(tilt, -30, 90)

    elif action_type == "display":
        lines = action.get("lines", ["", "", "", ""])
        while len(lines) < 4:
            lines.append("")
        for i, text in enumerate(lines[:4]):
            cmd = validate_tcode(3, {"lineNum": i, "Text": str(text)})
            if cmd:
                results.append(cmd)

    elif action_type == "oled":
        line = int(action.get("line", 0))
        text = str(action.get("text", ""))
        cmd = validate_tcode(3, {"lineNum": line, "Text": text})
        if cmd:
            results.append(cmd)

    elif action_type in ("lights", "light"):
        base_val = int(action.get("base", 0))
        head_val = int(action.get("head", 0))
        cmd = validate_tcode(132, {"IO4": base_val, "IO5": head_val})
        if cmd:
            results.append(cmd)

    else:
        log.warning(f"Unknown action type: {action_type!r}")

    return results


# -----------------------------------------------------------------------------
# Sense Computation
# -----------------------------------------------------------------------------

def is_plugged_in(override: Optional[bool], telemetry_state: Optional[TelemetryState]) -> bool:
    """Detect plugged-in state. Manual override takes precedence over voltage."""
    if override is not None:
        return override
    if telemetry_state is None:
        return False
    snap = telemetry_state.snapshot()
    return snap["battery_v"] > BATTERY_CHARGING_V


def read_cpu_temp() -> Optional[float]:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        return None


def compute_sense(snap: dict, *,
                  plugged: bool = False,
                  cv_state=None,
                  gimbal_arbiter=None,
                  wake_recorder=None) -> dict:
    """Compute interpreted body state from a telemetry snapshot."""
    wsl = snap["wheel_speed_l"]
    wsr = snap["wheel_speed_r"]

    moving = abs(wsl) > 0.01 or abs(wsr) > 0.01

    stuck = False
    if snap["drive_commanded"]:
        elapsed = time.time() - snap["drive_command_time"]
        if elapsed > STUCK_TIMEOUT_S:
            odom_delta_l = abs(snap["odl"] - snap["odom_at_command_l"])
            odom_delta_r = abs(snap["odr"] - snap["odom_at_command_r"])
            if (odom_delta_l < STUCK_ODOM_THRESHOLD
                    and odom_delta_r < STUCK_ODOM_THRESHOLD):
                stuck = True

    mx, my = snap["mx"], snap["my"]
    if mx == 0.0 and my == 0.0:
        heading_deg = None
    else:
        heading_deg = round((math.degrees(math.atan2(my, mx)) + 360) % 360, 1)

    ax, ay, az = snap["ax"], snap["ay"], snap["az"]
    denom = math.sqrt(ay * ay + az * az)
    tilt_deg = (round(math.degrees(math.atan2(-ax, denom)), 1)
                if denom > 0.001 else 0.0)
    roll_deg = (round(math.degrees(math.atan2(ay, az)), 1)
                if abs(az) > 0.001 else 0.0)

    speed_mps = round((abs(wsl) + abs(wsr)) / 2.0, 3)

    odl, odr = snap["odl"], snap["odr"]
    if odl > odr * 1.1 and odr != 0:
        drift = "right"
    elif odr > odl * 1.1 and odl != 0:
        drift = "left"
    else:
        drift = "none"

    bv = snap["battery_v"]
    battery_pct = round(
        _clamp((bv - BATTERY_MIN_V) / (BATTERY_MAX_V - BATTERY_MIN_V) * 100,
               0, 100), 1)

    odom_delta = (
        (snap["odl"] - snap.get("odom_session_start_l", 0))
        + (snap["odr"] - snap.get("odom_session_start_r", 0))
    ) / 2.0
    distance_session_m = round(abs(odom_delta) / TICKS_PER_METER, 3)

    result = {
        "moving": moving,
        "stuck": stuck,
        "heading_deg": heading_deg,
        "tilt_deg": tilt_deg,
        "roll_deg": roll_deg,
        "speed_mps": speed_mps,
        "drift": drift,
        "battery_pct": battery_pct,
        "distance_session_m": distance_session_m,
        "plugged_in": plugged,
        "wheels_locked": plugged,
    }

    # CV state
    if cv_state:
        cv_snap = cv_state.snapshot()
        result["faces"] = cv_snap["face_count"]
        result["tracking"] = (
            cv_snap["current_target"]["type"]
            if cv_snap["current_target"] else None
        )
    else:
        result["faces"] = 0
        result["tracking"] = None

    # Gimbal arbiter state
    if gimbal_arbiter:
        arb_snap = gimbal_arbiter.snapshot()
        result["gimbal_mode"] = arb_snap["mode"]
        result["queue_depth"] = arb_snap["queue_depth"]
    else:
        result["gimbal_mode"] = "idle"
        result["queue_depth"] = 0

    # Presence
    if cv_state:
        result["presence"] = cv_state.get_presence()
    else:
        result["presence"] = {}

    # Wake events
    if wake_recorder:
        events = wake_recorder.get_recent_events(n=5)
        result["wake_events"] = events
        result["wake_active"] = wake_recorder.is_active
    else:
        result["wake_events"] = []
        result["wake_active"] = False

    return result
