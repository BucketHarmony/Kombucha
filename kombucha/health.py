"""Health monitoring for Kombucha v2.

Checks subsystem health and reports degradation.
"""

import logging
import time
from datetime import datetime
from typing import Optional

from kombucha.schemas import SubsystemHealth

log = logging.getLogger("kombucha.health")


class HealthMonitor:
    """Tracks health of all rover subsystems."""

    def __init__(self):
        self._last_checks: dict[str, SubsystemHealth] = {}
        self._api_consecutive_failures: int = 0
        self._api_last_success: Optional[float] = None

    def check_camera(self, cap) -> SubsystemHealth:
        """Check if the camera is functional."""
        name = "camera"
        now = datetime.now().isoformat()
        if cap is None:
            result = SubsystemHealth(name=name, status="error", last_check=now,
                                     message="Camera not initialized")
        else:
            try:
                opened = cap.isOpened()
                if opened:
                    result = SubsystemHealth(name=name, status="ok", last_check=now)
                else:
                    result = SubsystemHealth(name=name, status="error", last_check=now,
                                             message="Camera not open")
            except Exception as e:
                result = SubsystemHealth(name=name, status="error", last_check=now,
                                         message=str(e))
        self._last_checks[name] = result
        return result

    def check_serial(self, serial_manager) -> SubsystemHealth:
        """Check if the serial port is connected."""
        name = "serial"
        now = datetime.now().isoformat()
        if serial_manager is None:
            result = SubsystemHealth(name=name, status="error", last_check=now,
                                     message="Serial manager not initialized")
        elif serial_manager.debug_mode:
            result = SubsystemHealth(name=name, status="ok", last_check=now,
                                     message="Debug mode")
        elif serial_manager.is_connected:
            result = SubsystemHealth(name=name, status="ok", last_check=now)
        else:
            result = SubsystemHealth(name=name, status="degraded", last_check=now,
                                     message="Serial disconnected, will reconnect")
        self._last_checks[name] = result
        return result

    def check_memory(self, db) -> SubsystemHealth:
        """Check if the memory database is accessible."""
        name = "memory"
        now = datetime.now().isoformat()
        if db is None:
            result = SubsystemHealth(name=name, status="error", last_check=now,
                                     message="Database not initialized")
        else:
            try:
                count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                result = SubsystemHealth(name=name, status="ok", last_check=now,
                                         metrics={"total_memories": count})
            except Exception as e:
                result = SubsystemHealth(name=name, status="error", last_check=now,
                                         message=str(e))
        self._last_checks[name] = result
        return result

    def check_audio(self, stt_listener=None, is_speaking: bool = False) -> SubsystemHealth:
        """Check audio subsystem: mic (STT listener) and speaker availability."""
        name = "audio"
        now = datetime.now().isoformat()
        metrics = {}

        mic_ok = stt_listener is not None and stt_listener.is_alive() if hasattr(stt_listener, 'is_alive') else stt_listener is not None
        metrics["mic_connected"] = mic_ok
        metrics["is_speaking"] = is_speaking

        if not mic_ok and stt_listener is not None:
            result = SubsystemHealth(name=name, status="degraded", last_check=now,
                                     message="STT listener not running", metrics=metrics)
        elif stt_listener is None:
            result = SubsystemHealth(name=name, status="degraded", last_check=now,
                                     message="No STT backend available", metrics=metrics)
        else:
            result = SubsystemHealth(name=name, status="ok", last_check=now,
                                     metrics=metrics)
        self._last_checks[name] = result
        return result

    def check_api(self, last_call_ok: bool = True, consecutive_errors: int = 0) -> SubsystemHealth:
        """Check Claude API connectivity based on recent call results."""
        name = "api"
        now = datetime.now().isoformat()
        metrics = {"consecutive_errors": consecutive_errors}

        if last_call_ok:
            self._api_consecutive_failures = 0
            self._api_last_success = time.time()
            result = SubsystemHealth(name=name, status="ok", last_check=now,
                                     metrics=metrics)
        elif consecutive_errors >= 3:
            result = SubsystemHealth(name=name, status="error", last_check=now,
                                     message=f"{consecutive_errors} consecutive API failures",
                                     metrics=metrics)
        else:
            result = SubsystemHealth(name=name, status="degraded", last_check=now,
                                     message=f"{consecutive_errors} consecutive API failures",
                                     metrics=metrics)
        self._last_checks[name] = result
        return result

    def check_redis(self, bus) -> SubsystemHealth:
        """Check Redis connectivity."""
        name = "redis"
        now = datetime.now().isoformat()
        if bus is None:
            result = SubsystemHealth(name=name, status="error", last_check=now,
                                     message="Redis bus not initialized")
        elif bus.is_fake:
            result = SubsystemHealth(name=name, status="degraded", last_check=now,
                                     message="Using FakeRedis (in-memory)")
        else:
            result = SubsystemHealth(name=name, status="ok", last_check=now)
        self._last_checks[name] = result
        return result

    def check_vision(self, detector_available: bool = False,
                     tracker_available: bool = False) -> SubsystemHealth:
        """Check vision pipeline (YOLO + tracker)."""
        name = "vision"
        now = datetime.now().isoformat()
        metrics = {"detector": detector_available, "tracker": tracker_available}
        if detector_available and tracker_available:
            result = SubsystemHealth(name=name, status="ok", last_check=now,
                                     metrics=metrics)
        elif not detector_available:
            result = SubsystemHealth(name=name, status="degraded", last_check=now,
                                     message="YOLO detector not available",
                                     metrics=metrics)
        else:
            result = SubsystemHealth(name=name, status="degraded", last_check=now,
                                     message="Tracker not available",
                                     metrics=metrics)
        self._last_checks[name] = result
        return result

    def report_all(self) -> dict[str, SubsystemHealth]:
        """Return the most recent health checks for all subsystems."""
        return dict(self._last_checks)

    def is_degraded(self) -> bool:
        """Return True if any subsystem is degraded or in error."""
        return any(
            h.status in ("degraded", "error")
            for h in self._last_checks.values()
        )
