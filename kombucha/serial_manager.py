"""ESP32 serial communication manager for Kombucha v2.

Handles serial port initialization, T-code command sending,
telemetry reading, and automatic reconnection.
"""

import json
import logging
import time
from typing import Optional

from kombucha.config import SerialConfig

log = logging.getLogger("kombucha.serial")


def _clamp(val, lo, hi):
    return max(lo, min(hi, val))


# --- T-Code Validators -------------------------------------------------------

TCODE_VALIDATORS = {
    0: lambda p: {},
    1: lambda p: {
        "T": 1,
        "L": _clamp(float(p.get("L", 0)), -1.3, 1.3),
        "R": _clamp(float(p.get("R", 0)), -1.3, 1.3),
    },
    3: lambda p: {
        "T": 3,
        "lineNum": _clamp(int(p.get("lineNum", 0)), 0, 3),
        "Text": str(p.get("Text", ""))[:20],
    },
    -3: lambda p: {},
    132: lambda p: {
        "T": 132,
        "IO4": _clamp(int(p.get("IO4", 0)), 0, 255),
        "IO5": _clamp(int(p.get("IO5", 0)), 0, 255),
    },
    133: lambda p: {
        "T": 133,
        "X": _clamp(int(p.get("X", 0)), -180, 180),
        "Y": _clamp(int(p.get("Y", 0)), -30, 90),
        "SPD": _clamp(int(p.get("SPD", 100)), 1, 200),
        "ACC": _clamp(int(p.get("ACC", 10)), 1, 50),
    },
    141: lambda p: {
        "T": 141,
        "X": _clamp(int(p.get("X", 0)), -180, 180),
        "Y": _clamp(int(p.get("Y", 0)), -30, 90),
        "SPD": _clamp(int(p.get("SPD", 50)), 1, 200),
    },
    210: lambda p: {
        "T": 210,
        "id": _clamp(int(p.get("id", 1)), 1, 2),
        "cmd": 1 if p.get("cmd") else 0,
    },
}


ESP32_INIT_CMDS = [
    {"T": 142, "cmd": 50},              # Set feedback interval
    {"T": 131, "cmd": 1},               # Serial feedback flow on
    {"T": 143, "cmd": 0},               # Serial echo off
    {"T": 4, "cmd": 2},                 # Select module: Gimbal
    {"T": 900, "main": 2, "module": 2}, # Set version: UGV Rover + Gimbal
]


def validate_tcode(t_code, params):
    """Validate and sanitize a T-code command."""
    validator = TCODE_VALIDATORS.get(t_code)
    if validator is None:
        log.warning(f"Blocked unknown T-code: {t_code}")
        return None
    try:
        validated = validator(params)
        validated["T"] = t_code
        return validated
    except (ValueError, TypeError, KeyError) as e:
        log.warning(f"T-code {t_code} validation failed: {e}")
        return None


class SerialManager:
    """Manages the ESP32 serial connection."""

    def __init__(self, config: SerialConfig, debug_mode: bool = False):
        self.config = config
        self.debug_mode = debug_mode
        self.port = None
        self.last_command = None

    def connect(self) -> bool:
        """Open the serial port and send ESP32 init commands."""
        if self.debug_mode:
            log.info("[DEBUG] Serial skipped (debug mode)")
            return True
        try:
            import serial
            ser = serial.Serial(
                self.config.port,
                self.config.baud_rate,
                timeout=1.0,
            )
            time.sleep(2.0)  # Wait for ESP32 boot after DTR reset
            if ser.in_waiting:
                ser.read(ser.in_waiting)
            # Send ESP32 initialization commands
            for cmd in ESP32_INIT_CMDS:
                ser.write((json.dumps(cmd) + "\n").encode())
                time.sleep(self.config.cmd_delay_s)
            log.info(f"Serial open: {self.config.port} @ {self.config.baud_rate} (ESP32 init sent)")
            self.port = ser
            return True
        except Exception as e:
            log.warning(f"Serial init failed: {e}")
            self.port = None
            return False

    def reconnect(self) -> bool:
        """Close and reopen the serial port."""
        if self.debug_mode:
            return True
        self.close()
        return self.connect()

    def send(self, cmd_dict: dict) -> bool:
        """Send a JSON T-code command to the ESP32."""
        self.last_command = cmd_dict
        if self.debug_mode:
            log.info(f"  [DEBUG] WOULD SEND: {json.dumps(cmd_dict)}")
            return True
        if self.port is None:
            return False
        try:
            import serial
            payload = json.dumps(cmd_dict) + "\n"
            self.port.write(payload.encode())
            time.sleep(self.config.cmd_delay_s)
            return True
        except Exception as e:
            log.error(f"Serial write error: {e}")
            self.reconnect()
            return False

    def read_telemetry(self) -> dict:
        """Read ESP32 feedback and system metrics.

        Returns dict with battery_v, cpu_temp_c, IMU data, odometry, cpu_load, uptime_s.
        """
        telemetry = {}
        # CPU temperature
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                telemetry["cpu_temp_c"] = round(int(f.read().strip()) / 1000, 1)
        except Exception:
            pass
        # CPU load (1-min average)
        try:
            with open("/proc/loadavg") as f:
                telemetry["cpu_load"] = float(f.read().split()[0])
        except Exception:
            pass
        # Uptime
        try:
            with open("/proc/uptime") as f:
                telemetry["uptime_s"] = int(float(f.read().split()[0]))
        except Exception:
            pass
        # ESP32 feedback
        if self.port and not self.debug_mode:
            try:
                if self.port.in_waiting:
                    raw = self.port.read(self.port.in_waiting)
                    for line in raw.decode(errors="replace").strip().split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                            if d.get("T") == 1001:
                                if "v" in d:
                                    telemetry["battery_v"] = round(d["v"] / 100, 2)
                                telemetry["odometer_l"] = d.get("odl", 0)
                                telemetry["odometer_r"] = d.get("odr", 0)
                                # Motor speeds
                                if "sl" in d:
                                    telemetry["motor_speed_l"] = round(d["sl"] / 100, 3)
                                if "sr" in d:
                                    telemetry["motor_speed_r"] = round(d["sr"] / 100, 3)
                                # IMU accelerometer (raw units)
                                if "ax" in d:
                                    telemetry["imu_accel_x"] = round(d["ax"] / 16384.0, 3)
                                    telemetry["imu_accel_y"] = round(d.get("ay", 0) / 16384.0, 3)
                                    telemetry["imu_accel_z"] = round(d.get("az", 0) / 16384.0, 3)
                                # IMU gyroscope (raw units)
                                if "gx" in d:
                                    telemetry["imu_gyro_x"] = round(d["gx"] / 131.0, 2)
                                    telemetry["imu_gyro_y"] = round(d.get("gy", 0) / 131.0, 2)
                                    telemetry["imu_gyro_z"] = round(d.get("gz", 0) / 131.0, 2)
                        except (json.JSONDecodeError, ValueError):
                            pass
            except Exception:
                pass
        return telemetry

    def close(self) -> None:
        """Close the serial port."""
        if self.port is not None:
            try:
                self.port.close()
            except Exception:
                pass
        self.port = None

    @property
    def is_connected(self) -> bool:
        return self.port is not None or self.debug_mode
