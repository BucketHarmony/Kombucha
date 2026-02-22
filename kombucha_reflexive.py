#!/usr/bin/env python3
"""
kombucha_reflexive.py — Kombucha v2 reflexive layer.

Owns: serial port, camera, hardware telemetry, motor execution.
Publishes: scene state, hardware context, self-model to Redis.
Reads: motor commands from brain, display commands.

    python3 kombucha_reflexive.py [--debug] [--config config.yaml]
"""

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime

from kombucha.config import load_config
from kombucha.serial_manager import SerialManager, validate_tcode
from kombucha.vision import (
    init_camera, capture_frame_b64, compute_frame_delta,
    compute_self_model_error, build_scene_state,
    HAS_NCNN, HAS_VISION,
)
try:
    from kombucha.vision import YOLODetector, CentroidTracker
except ImportError:
    YOLODetector = None
    CentroidTracker = None
from kombucha.redis_bus import RedisBus
from kombucha.schemas import (
    HardwareContext, SelfModelError,
    Event, MotorCommand,
)
from kombucha.health import HealthMonitor

# --- CLI Args -----------------------------------------------------------------

_parser = argparse.ArgumentParser(description="Kombucha v2 reflexive layer")
_parser.add_argument("--debug", action="store_true")
_parser.add_argument("--config", type=str, default=None)
_args = _parser.parse_args()

# --- Config -------------------------------------------------------------------

config = load_config(_args.config)
config.debug_mode = _args.debug or config.debug_mode

logging.basicConfig(
    level=logging.DEBUG if config.debug_mode else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("kombucha.reflexive")

# --- Graceful Shutdown --------------------------------------------------------

running = True


def shutdown_handler(signum, _frame):
    global running
    log.info("Received signal %d, shutting down...", signum)
    running = False


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)


# --- System Telemetry ---------------------------------------------------------

def collect_system_telemetry() -> dict:
    """Collect Pi system metrics: CPU temp, RAM, disk, WiFi."""
    info = {}
    # CPU temperature
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            info["cpu_temp_c"] = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        pass
    # RAM usage
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
            mem = {}
            for line in lines:
                parts = line.split()
                if parts[0] in ("MemTotal:", "MemAvailable:"):
                    mem[parts[0].rstrip(":")] = int(parts[1])
            if "MemTotal" in mem and "MemAvailable" in mem:
                used = mem["MemTotal"] - mem["MemAvailable"]
                info["ram_used_pct"] = round(used / mem["MemTotal"] * 100, 1)
    except Exception:
        pass
    # Disk free
    try:
        st = os.statvfs("/")
        info["disk_free_mb"] = int(st.f_bavail * st.f_frsize / 1024 / 1024)
    except Exception:
        pass
    # WiFi RSSI
    try:
        with open("/proc/net/wireless") as f:
            lines = f.readlines()
            if len(lines) >= 3:
                parts = lines[2].split()
                info["wifi_rssi"] = int(float(parts[3]))
    except Exception:
        pass
    return info


# --- Motor Command Forwarding -------------------------------------------------

def forward_motor_command(motor: MotorCommand, serial: SerialManager, config):
    """Convert MotorCommand to T-codes and send to ESP32."""
    # Drive: convert (drive m/s, turn deg/s) to (left, right) differential
    if motor.drive != 0.0 or motor.turn != 0.0:
        omega = motor.turn * math.pi / 180.0  # deg/s -> rad/s
        v_diff = omega * config.serial.wheel_base_m / 2.0
        left = motor.drive - v_diff
        right = motor.drive + v_diff
        cmd = validate_tcode(1, {"L": left, "R": right})
        if cmd:
            serial.send(cmd)
    else:
        # Explicit stop
        serial.send(validate_tcode(0, {}) or {"T": 0})

    # Pan/tilt
    if motor.pan is not None or motor.tilt is not None:
        pan = motor.pan if motor.pan is not None else 0
        tilt = motor.tilt if motor.tilt is not None else 0
        cmd = validate_tcode(133, {"X": pan, "Y": tilt, "SPD": 100, "ACC": 10})
        if cmd:
            serial.send(cmd)

    # Lights
    if motor.lights_base is not None or motor.lights_head is not None:
        cmd = validate_tcode(132, {
            "IO4": motor.lights_base or 0,
            "IO5": motor.lights_head or 0,
        })
        if cmd:
            serial.send(cmd)


# ==============================================================================
# MAIN LOOP
# ==============================================================================

async def main():
    serial = SerialManager(config.serial, debug_mode=config.debug_mode)
    serial.connect()

    cap = init_camera(config.camera)
    bus = RedisBus(config.redis)
    health = HealthMonitor()

    if config.debug_mode:
        log.info("=" * 60)
        log.info("  REFLEXIVE DEBUG MODE — no hardware actions")
        log.info("=" * 60)

    log.info("Reflexive layer started.")

    # --- YOLO detector + tracker init ---
    detector = None
    tracker = None
    if HAS_NCNN and HAS_VISION and YOLODetector and CentroidTracker:
        yolo_model_path = getattr(config, "yolo_model_path", None)
        if yolo_model_path is None:
            # Default path
            from pathlib import Path
            default_path = Path.home() / "kombucha" / "models" / "yolo" / "yolov8n"
            if (default_path.parent / "yolov8n.param").exists():
                yolo_model_path = str(default_path)
        if yolo_model_path:
            try:
                detector = YOLODetector(yolo_model_path)
                tracker = CentroidTracker()
                log.info("YOLO detector + centroid tracker initialized")
            except Exception as e:
                log.warning(f"YOLO init failed: {e}")
    if detector is None:
        log.info("YOLO not available — scene will have no object detections")

    # Startup hardware
    if serial.is_connected:
        serial.send({"T": 132, "IO4": 0, "IO5": 32})  # dim head LED

    prev_frame_b64 = None
    prev_actions = []
    prev_person_ids = set()
    loop_interval = 1.0 / config.camera.fps_target

    try:
        while running:
            loop_start = time.time()

            # --- Vision ---
            try:
                frame_b64 = capture_frame_b64(cap, config.camera)
            except Exception as e:
                log.warning(f"Camera capture failed: {e}")
                await asyncio.sleep(loop_interval)
                continue

            frame_delta = compute_frame_delta(prev_frame_b64, frame_b64)

            # --- Object detection ---
            tracked_objects = []
            if detector and tracker:
                try:
                    import cv2 as _cv2
                    import numpy as _np
                    import base64 as _b64
                    raw_frame = _cv2.imdecode(
                        _np.frombuffer(_b64.b64decode(frame_b64), _np.uint8),
                        _cv2.IMREAD_COLOR,
                    )
                    detections = detector.detect(raw_frame)
                    tracked_objects = tracker.update(detections)
                except Exception as e:
                    log.debug(f"YOLO detection failed: {e}")

            scene = build_scene_state(
                frame_b64=frame_b64,
                frame_delta=frame_delta,
                tracked_objects=tracked_objects,
                motion_threshold=config.motion.sentry_wake_threshold,
                frame_width=config.camera.resolution_w,
                frame_height=config.camera.resolution_h,
            )

            # --- Self-model error ---
            sme_dict = compute_self_model_error(
                prev_actions, prev_frame_b64, frame_b64,
                motion_config=config.motion,
            )
            sme = SelfModelError.from_dict(sme_dict)

            # --- Hardware telemetry ---
            esp_data = serial.read_telemetry()
            sys_data = collect_system_telemetry()

            hardware = HardwareContext(
                timestamp=datetime.now().isoformat(),
                battery_v=esp_data.get("battery_v"),
                cpu_temp_c=esp_data.get("cpu_temp_c") or sys_data.get("cpu_temp_c"),
                odometer_l=esp_data.get("odometer_l", 0),
                odometer_r=esp_data.get("odometer_r", 0),
                wifi_rssi=sys_data.get("wifi_rssi"),
                disk_free_mb=sys_data.get("disk_free_mb"),
                ram_used_pct=sys_data.get("ram_used_pct"),
            )

            # --- Publish to Redis ---
            bus.set_scene(scene)
            bus.set_hardware(hardware)
            bus.set_self_model(sme)

            # --- Publish events ---
            if sme.anomaly:
                bus.publish_event(Event(
                    event_type="self_model_anomaly",
                    source="reflexive",
                    data=sme.to_dict(),
                ))
            if scene.motion_detected:
                bus.publish_wake("motion")

            # --- Person detection events ---
            current_person_ids = {
                o.track_id for o in scene.objects if o.cls == "person"
            }
            new_persons = current_person_ids - prev_person_ids
            for pid in new_persons:
                person_obj = next((o for o in scene.objects if o.track_id == pid), None)
                if person_obj:
                    bus.publish_event(Event(
                        event_type="person_entered",
                        source="reflexive",
                        data={
                            "track_id": pid,
                            "bearing_deg": person_obj.bearing_deg,
                            "distance_est_m": person_obj.distance_est_m,
                        },
                    ))
                    bus.publish_wake("person_entered")
            prev_person_ids = current_person_ids

            # --- Read motor command from brain ---
            motor = bus.get_motor()
            if motor:
                forward_motor_command(motor, serial, config)
                # Build prev_actions for self-model error compatibility
                prev_actions = []
                if motor.drive != 0 or motor.turn != 0:
                    omega = motor.turn * math.pi / 180.0
                    v_diff = omega * config.serial.wheel_base_m / 2.0
                    prev_actions.append({
                        "type": "drive",
                        "left": motor.drive - v_diff,
                        "right": motor.drive + v_diff,
                    })
                if motor.pan is not None or motor.tilt is not None:
                    prev_actions.append({
                        "type": "look",
                        "pan": motor.pan or 0,
                        "tilt": motor.tilt or 0,
                    })
            else:
                prev_actions = []

            # --- Relay display from brain to ESP32 ---
            display_lines = bus.get_display()
            if display_lines:
                for i, text in enumerate(display_lines[:4]):
                    cmd = validate_tcode(3, {"lineNum": i, "Text": str(text)})
                    if cmd:
                        serial.send(cmd)

            # --- Health ---
            health_report = {
                "camera": health.check_camera(cap),
                "serial": health.check_serial(serial),
            }
            bus.set_status("reflexive", health_report)

            # Stash frame
            prev_frame_b64 = frame_b64

            # --- Pace the loop ---
            elapsed = time.time() - loop_start
            sleep_time = max(0.0, loop_interval - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    finally:
        log.info("Reflexive layer shutting down...")
        if serial.is_connected:
            serial.send({"T": 0})  # stop motors
            serial.send({"T": 132, "IO4": 0, "IO5": 0})  # lights off
            serial.close()
        if cap:
            try:
                cap.release()
            except Exception:
                pass
        log.info("Reflexive layer stopped.")


if __name__ == "__main__":
    asyncio.run(main())
