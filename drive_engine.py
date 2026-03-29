#!/usr/bin/env python3
"""
drive_engine.py — Five involuntary pressure accumulators.

Drives charge based on sensor state and decay over time.
The body updates drives before each invocation; the soul sees them in its prompt.

Usage:
    python3 drive_engine.py update [--sense SENSE_JSON] [--elapsed SECONDS]
    python3 drive_engine.py relieve <drive_name>
    python3 drive_engine.py status
"""

import json
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

STATE_FILE = Path("/opt/kombucha/state/body_state.json")

DRIVE_CONFIG = {
    "wanderlust": {
        "charge_rate": 0.0006,      # per second when stationary (~28min to max)
        "decay_rate": 0.01,         # per second when moving
        "threshold": 0.8,
    },
    "curiosity": {
        "charge_per_event": 0.05,   # per novel YOLO detection
        "decay_rate": 0.002,        # per second
        "threshold": 0.7,
    },
    "social": {
        "charge_rate": 0.008,       # per second when face visible but not engaged
        "charge_rate_tracking": 0.002,  # lower rate when already tracking
        "decay_rate": 0.005,        # per second
        "threshold": 0.6,
    },
    "cringe": {
        "charge_per_match": 0.1,    # per cringe phrase found
        "decay_rate": 0.001,        # per second (slow decay)
        "threshold": 0.7,
        "scan_interval": 300,       # seconds between scans
    },
    "attachment": {
        "charge_rate": 0.005,       # per second of repeated gaze fixation
        "decay_rate": 0.003,        # per second
        "threshold": 0.6,
    },
}


def load_state() -> dict:
    """Load body state from disk."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "last_tick": 238,
        "wake_count": 0,
        "last_invocation": None,
        "drives": {name: 0.0 for name in DRIVE_CONFIG},
    }


def save_state(state: dict):
    """Save body state to disk."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def update_drives(state: dict, sense: dict = None, elapsed_s: float = 3600.0) -> dict:
    """Update all drive levels based on sense data and elapsed time."""
    drives = state.get("drives", {name: 0.0 for name in DRIVE_CONFIG})

    # Ensure all drives exist
    for name in DRIVE_CONFIG:
        if name not in drives:
            drives[name] = 0.0

    # --- Wanderlust: charges when stationary, decays when moving ---
    if sense and sense.get("moving", False):
        drives["wanderlust"] = clamp01(
            drives["wanderlust"] - DRIVE_CONFIG["wanderlust"]["decay_rate"] * elapsed_s)
    else:
        drives["wanderlust"] = clamp01(
            drives["wanderlust"] + DRIVE_CONFIG["wanderlust"]["charge_rate"] * elapsed_s)

    # --- Curiosity: decays over time, charges on novel detections ---
    drives["curiosity"] = clamp01(
        drives["curiosity"] - DRIVE_CONFIG["curiosity"]["decay_rate"] * elapsed_s)
    if sense:
        presence = sense.get("presence", {})
        novel_count = len([k for k, v in presence.items() if v < 20.0])
        drives["curiosity"] = clamp01(
            drives["curiosity"] + novel_count * DRIVE_CONFIG["curiosity"]["charge_per_event"])

    # --- Social: charges when face visible ---
    if sense and sense.get("faces", 0) > 0:
        if sense.get("tracking") == "person":
            rate = DRIVE_CONFIG["social"]["charge_rate_tracking"]
        else:
            rate = DRIVE_CONFIG["social"]["charge_rate"]
        drives["social"] = clamp01(drives["social"] + rate * elapsed_s)
    else:
        drives["social"] = clamp01(
            drives["social"] - DRIVE_CONFIG["social"]["decay_rate"] * elapsed_s)

    # --- Cringe: decays slowly, charged by external scanner ---
    drives["cringe"] = clamp01(
        drives["cringe"] - DRIVE_CONFIG["cringe"]["decay_rate"] * elapsed_s)

    # --- Attachment: decays over time, charged by gaze tracker ---
    drives["attachment"] = clamp01(
        drives["attachment"] - DRIVE_CONFIG["attachment"]["decay_rate"] * elapsed_s)

    state["drives"] = drives
    return state


def relieve_drive(state: dict, drive_name: str, amount: float = 0.3) -> dict:
    """Relieve a drive after the soul addresses it.

    Wanderlust gets stronger relief (0.6) because it charges fast between
    hourly ticks (0.003/s * 3600s = 10.8, clamped to 1.0).  A single tick
    with movement should noticeably drain it.
    """
    if amount == 0.3 and drive_name == "wanderlust":
        amount = 0.6
    drives = state.get("drives", {})
    if drive_name in drives:
        drives[drive_name] = clamp01(drives[drive_name] - amount)
    state["drives"] = drives
    return state


def format_drives(drives: dict) -> str:
    """Format drives for soul prompt context."""
    parts = []
    for name, level in drives.items():
        config = DRIVE_CONFIG.get(name, {})
        threshold = config.get("threshold", 0.7)
        if level >= threshold:
            tag = "HIGH"
        elif level >= threshold * 0.5:
            tag = "medium"
        else:
            tag = "low"
        parts.append(f"{name}={level:.2f} ({tag})")
    return "Drives: " + ", ".join(parts)


def main():
    if len(sys.argv) < 2:
        print("Usage: drive_engine.py [update|relieve|status]")
        sys.exit(1)

    cmd = sys.argv[1]
    state = load_state()

    if cmd == "update":
        sense = None
        elapsed = 3600.0

        # Parse optional args
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--sense" and i + 1 < len(sys.argv):
                sense = json.loads(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == "--elapsed" and i + 1 < len(sys.argv):
                elapsed = float(sys.argv[i + 1])
                i += 2
            else:
                i += 1

        state = update_drives(state, sense, elapsed)
        save_state(state)
        print(format_drives(state["drives"]))

    elif cmd == "relieve":
        if len(sys.argv) < 3:
            print("Usage: drive_engine.py relieve <drive_name>")
            sys.exit(1)
        drive_name = sys.argv[2]
        state = relieve_drive(state, drive_name)
        save_state(state)
        print(f"Relieved {drive_name}: {state['drives'].get(drive_name, 0):.2f}")

    elif cmd == "status":
        print(format_drives(state.get("drives", {})))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
