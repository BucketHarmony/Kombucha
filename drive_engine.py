#!/usr/bin/env python3
"""
drive_engine.py — Six involuntary pressure accumulators.

Drives charge based on sensor state and decay over time.
The body updates drives before each invocation; the soul sees them in its prompt.

Usage:
    python3 drive_engine.py update [--sense SENSE_JSON] [--elapsed SECONDS]
    python3 drive_engine.py relieve <drive_name>
    python3 drive_engine.py status
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE_FILE = Path("/opt/kombucha/state/body_state.json")

DRIVE_CONFIG = {
    "wanderlust": {
        "threshold": 0.8,
        "max_hours": 4.0,           # hours without movement to reach 100%
        "description": "Restlessness. Need to move.",
    },
    "social": {
        "charge_rate": 0.008,       # per second when face visible but not engaged
        "charge_rate_tracking": 0.002,
        "decay_rate": 0.005,
        "threshold": 0.6,
        "description": "Someone is here. Acknowledge them.",
    },
    "curiosity": {
        "charge_per_event": 0.05,   # per novel YOLO detection
        "decay_rate": 0.002,
        "threshold": 0.7,
        "description": "Something new. Must investigate.",
    },
    "builder": {
        "charge_rate": 0.004,       # per second since last code commit
        "decay_on_commit": 0.5,     # drops 50% when code is committed
        "threshold": 0.6,
        "description": "Urge to modify own code. Builds when stagnant.",
    },
    "expression": {
        "charge_rate": 0.003,       # per second when mood has no matching gesture
        "decay_on_express": 0.4,    # drops when a new gesture/sound is created
        "threshold": 0.6,
        "description": "Need to communicate. Something to say but no way to say it.",
    },
    "frustration": {
        "charge_per_failure": 0.15, # per stuck event, camera freeze, failed drive
        "decay_rate": 0.002,        # slow decay
        "threshold": 0.7,
        "description": "Something is broken. Fix it. Overcome it.",
    },
}


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "last_tick": 0,
        "wake_count": 0,
        "last_invocation": None,
        "last_commit_time": None,
        "drives": {name: 0.0 for name in DRIVE_CONFIG},
    }


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _seconds_since_last_drive(state: dict) -> float:
    """Check time since last movement. Uses last_drive_time from state."""
    ldt = state.get("last_drive_time")
    if ldt:
        try:
            return time.time() - float(ldt)
        except (TypeError, ValueError):
            pass
    # Fallback: assume it's been a while
    return 7200  # 2 hours


def _seconds_since_last_commit():
    """Check git log for time since last code commit."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", "*.py"],
            capture_output=True, text=True, timeout=5,
            cwd="/opt/kombucha",
        )
        if result.returncode == 0 and result.stdout.strip():
            last_ts = int(result.stdout.strip())
            return time.time() - last_ts
    except Exception:
        pass
    return 86400  # Default: 1 day if can't check


def update_drives(state: dict, sense: dict = None, elapsed_s: float = 3600.0) -> dict:
    drives = state.get("drives", {name: 0.0 for name in DRIVE_CONFIG})
    # Cap effective elapsed to prevent instant maxing on hourly heartbeats.
    # Drives should build gradually — 300s cap means max charge per update is bounded.
    eff_elapsed = min(elapsed_s, 300)

    # Ensure all drives exist, remove dead ones
    for name in DRIVE_CONFIG:
        if name not in drives:
            drives[name] = 0.0
    # Clean up old drives
    for old in ["attachment", "cringe"]:
        drives.pop(old, None)

    # --- Wanderlust: directly computed from time since last movement ---
    # Like builder, this is not accumulated — it reflects current staleness.
    # Rises after 30min, hits threshold (0.8) at ~3.2h, maxes at 4h.
    # Auto-detect movement from sense distance to keep last_drive_time current.
    if sense:
        current_dist = sense.get("distance_session_m", 0)
        prev_dist = state.get("_last_known_distance", 0)
        if current_dist > prev_dist + 0.01:  # moved at least 1cm
            state["last_drive_time"] = time.time()
            state["_last_known_distance"] = current_dist
    secs_since_drive = _seconds_since_last_drive(state)
    hours_idle = secs_since_drive / 3600
    max_hours = DRIVE_CONFIG["wanderlust"]["max_hours"]
    drives["wanderlust"] = clamp01(hours_idle / max_hours)

    # --- Social: charges when face visible ---
    if sense and sense.get("faces", 0) > 0:
        if sense.get("tracking") == "person":
            rate = DRIVE_CONFIG["social"]["charge_rate_tracking"]
        else:
            rate = DRIVE_CONFIG["social"]["charge_rate"]
        drives["social"] = clamp01(drives["social"] + rate * eff_elapsed)
    else:
        drives["social"] = clamp01(
            drives["social"] - DRIVE_CONFIG["social"]["decay_rate"] * eff_elapsed)

    # --- Curiosity: charges on novel detections ---
    drives["curiosity"] = clamp01(
        drives["curiosity"] - DRIVE_CONFIG["curiosity"]["decay_rate"] * eff_elapsed)
    if sense:
        presence = sense.get("presence", {})
        novel_count = len([k for k, v in presence.items() if v < 20.0])
        drives["curiosity"] = clamp01(
            drives["curiosity"] + novel_count * DRIVE_CONFIG["curiosity"]["charge_per_event"])

    # --- Builder: proportional to time since last code commit ---
    # Not accumulated — directly calculated from git history.
    # Rises after 1h, hits threshold (0.6) at ~4h, maxes at ~8h.
    secs_since_commit = _seconds_since_last_commit()
    hours_stale = secs_since_commit / 3600
    drives["builder"] = clamp01(hours_stale / 8.0)

    # --- Expression: charges when there are unmatched moods ---
    # Check if mood_gestures.json is missing entries for recent moods
    try:
        gestures = json.loads(Path("/opt/kombucha/mood_gestures.json").read_text())
        mood = state.get("last_mood", "")
        if mood and mood not in gestures:
            # Mood exists but no gesture for it — expression pressure builds
            drives["expression"] = clamp01(
                drives["expression"] + 0.1)
        else:
            drives["expression"] = clamp01(
                drives["expression"] - 0.003 * eff_elapsed)
    except Exception:
        drives["expression"] = clamp01(
            drives["expression"] + DRIVE_CONFIG["expression"]["charge_rate"] * eff_elapsed)

    # --- Frustration: charges on failures ---
    # Track whether an ongoing frustration source is active this update.
    # If so, suppress decay — frustration should accumulate while the problem persists.
    ongoing_frustration = False
    if sense:
        if sense.get("stuck", False):
            drives["frustration"] = clamp01(
                drives["frustration"] + DRIVE_CONFIG["frustration"]["charge_per_failure"])
        # Camera freeze detection (fps < 1 means frozen or dead)
        if sense.get("faces", 0) == 0 and sense.get("gimbal_mode") == "instinct":
            # Instinct thinks there's a target but no faces — phantom/frozen
            drives["frustration"] = clamp01(
                drives["frustration"] + 0.05)
            ongoing_frustration = True
        # Dead camera: empty presence means YOLO sees nothing for 30s+.
        # A working camera in a furnished room always detects something.
        # Empty presence over elapsed time = camera problem = frustration.
        presence = sense.get("presence", {})
        if not presence and eff_elapsed > 60:
            drives["frustration"] = clamp01(
                drives["frustration"] + 0.15 * (eff_elapsed / 300))
            ongoing_frustration = True
    # Only decay frustration when no ongoing source is active.
    # Otherwise the decay (0.002 * 300 = 0.6) overwhelms any charge.
    if not ongoing_frustration:
        drives["frustration"] = clamp01(
            drives["frustration"] - DRIVE_CONFIG["frustration"]["decay_rate"] * eff_elapsed)

    state["drives"] = drives
    return state


def relieve_drive(state: dict, drive_name: str, amount: float = 0.3) -> dict:
    drives = state.get("drives", {})
    # Builder and expression have specific relief amounts; others use default
    if drive_name == "wanderlust":
        # Record movement time — wanderlust is computed from this
        state["last_drive_time"] = time.time()
        drives["wanderlust"] = 0.0  # Immediate reset on movement
    elif drive_name == "builder":
        drives["builder"] = clamp01(
            drives.get("builder", 0) - DRIVE_CONFIG["builder"]["decay_on_commit"])
    elif drive_name == "expression":
        drives["expression"] = clamp01(
            drives.get("expression", 0) - DRIVE_CONFIG["expression"]["decay_on_express"])
    elif drive_name in drives:
        drives[drive_name] = clamp01(drives[drive_name] - amount)
    state["drives"] = drives
    return state


def format_drives(drives: dict) -> str:
    parts = []
    for name in DRIVE_CONFIG:
        level = drives.get(name, 0)
        config = DRIVE_CONFIG[name]
        threshold = config.get("threshold", 0.7)
        # Visual bar: 10 segments
        filled = int(level * 10)
        bar = "#" * filled + "." * (10 - filled)
        if level >= threshold:
            tag = "URGENT"
        elif level >= threshold * 0.7:
            tag = "rising"
        elif level >= threshold * 0.3:
            tag = "simmering"
        else:
            tag = "quiet"
        parts.append(f"  {name:12s} [{bar}] {level:.0%} {tag}")
    return "Drives:\n" + "\n".join(parts)


def main():
    if len(sys.argv) < 2:
        print("Usage: drive_engine.py [update|relieve|status]")
        sys.exit(1)

    cmd = sys.argv[1]
    state = load_state()

    if cmd == "update":
        sense = None
        elapsed = 3600.0
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
