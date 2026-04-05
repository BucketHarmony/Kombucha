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
        "decay_rate": 0.002,        # slow decay when source still active
        "recovery_rate": 0.01,      # fast decay when source is resolved
        "threshold": 0.7,
        "description": "Something is broken. Fix it. Overcome it.",
    },
}


# --- Drive Planner ---
# Empirical duration-distance curve from blind calibration (ticks 460-465).
# Measured at 80% power (L=1.04, R=1.08) on hardwood floor.
# Startup lag (~450ms) is baked into these measurements.
CALIBRATION_POINTS = [
    (1000, 10.0),    # 1000ms → 10.0cm
    (1500, 14.2),    # 1500ms → 14.2cm
    (2000, 18.9),    # 2000ms → 18.9cm
    (2500, 24.7),    # 2500ms → 24.7cm
    (3000, 30.1),    # 3000ms → 30.1cm
]

# Post-startup effective speed: ~12cm/s (once wheels are moving).
# Startup lag: ~450ms of zero motion at the beginning of every drive.
STARTUP_LAG_MS = 450
POST_STARTUP_SPEED_CM_PER_S = 12.0


def duration_for_distance(target_cm: float) -> int:
    """Return drive duration (ms) needed to travel target_cm forward at 80% power.

    Uses linear interpolation between calibration points.
    Extrapolates beyond the measured range using the post-startup linear model.
    Returns at least 600ms (minimum useful drive duration).
    """
    if target_cm <= 0:
        return 0

    # Below minimum calibration point: extrapolate from startup model
    if target_cm <= CALIBRATION_POINTS[0][1]:
        ratio = target_cm / CALIBRATION_POINTS[0][1]
        return max(600, int(CALIBRATION_POINTS[0][0] * ratio))

    # Between calibration points: linear interpolation
    for i in range(len(CALIBRATION_POINTS) - 1):
        d0, cm0 = CALIBRATION_POINTS[i]
        d1, cm1 = CALIBRATION_POINTS[i + 1]
        if target_cm <= cm1:
            frac = (target_cm - cm0) / (cm1 - cm0)
            return int(d0 + frac * (d1 - d0))

    # Beyond max calibration point: linear extrapolation at post-startup speed
    max_d, max_cm = CALIBRATION_POINTS[-1]
    extra_cm = target_cm - max_cm
    extra_ms = extra_cm / POST_STARTUP_SPEED_CM_PER_S * 1000
    return min(5000, int(max_d + extra_ms))  # cap at bridge max


def distance_for_duration(duration_ms: int) -> float:
    """Return estimated distance (cm) for a forward drive of given duration at 80% power.

    Uses linear interpolation between calibration points.
    """
    if duration_ms <= 0:
        return 0.0

    # Below minimum calibration point
    if duration_ms <= CALIBRATION_POINTS[0][0]:
        ratio = duration_ms / CALIBRATION_POINTS[0][0]
        return CALIBRATION_POINTS[0][1] * ratio

    # Between calibration points: linear interpolation
    for i in range(len(CALIBRATION_POINTS) - 1):
        d0, cm0 = CALIBRATION_POINTS[i]
        d1, cm1 = CALIBRATION_POINTS[i + 1]
        if duration_ms <= d1:
            frac = (duration_ms - d0) / (d1 - d0)
            return cm0 + frac * (cm1 - cm0)

    # Beyond max calibration: linear extrapolation
    max_d, max_cm = CALIBRATION_POINTS[-1]
    extra_ms = duration_ms - max_d
    return max_cm + extra_ms * POST_STARTUP_SPEED_CM_PER_S / 1000


# --- Turn Planner ---
# Empirical duration-degree curves from blind calibration (ticks 446-468).
# Measured at 80% power (L=1.04, R=-1.04 or vice versa) on hardwood floor.
# Right turns are symmetric; left turns have cable-drag asymmetry.
RIGHT_TURN_POINTS = [
    (1750, 67.0),     # tick 446-447 calibration
    (1850, 85.0),     # tick 448 calibration
    (1920, 91.5),     # tick 466 (avg of 92 + 91)
    (1950, 93.0),     # tick 465 calibration
]

LEFT_TURN_POINTS = [
    (1750, 82.0),     # tick 447 (variable 82-90, low end)
    (1800, 88.5),     # tick 468 (avg of 90 + 87)
    (1900, 103.0),    # tick — extrapolated from 1900ms=199 odom
]


def duration_for_turn(target_deg: float, direction: str = "right") -> int:
    """Return turn duration (ms) for target_deg at 80% power.

    direction: "right" (CW, L=1.04 R=-1.04) or "left" (CCW, L=-1.04 R=1.04).
    Left turns are ~3-5% slower due to cable drag on left wheel during CCW.
    Returns at least 600ms. Caps at 5000ms.
    """
    if target_deg <= 0:
        return 0

    points = RIGHT_TURN_POINTS if direction == "right" else LEFT_TURN_POINTS

    # Below minimum calibration: linear extrapolation from first point
    if target_deg <= points[0][1]:
        ratio = target_deg / points[0][1]
        return max(600, int(points[0][0] * ratio))

    # Between calibration points: linear interpolation
    for i in range(len(points) - 1):
        d0, deg0 = points[i]
        d1, deg1 = points[i + 1]
        if target_deg <= deg1:
            frac = (target_deg - deg0) / (deg1 - deg0)
            return int(d0 + frac * (d1 - d0))

    # Beyond max calibration: linear extrapolation from last two points
    d_prev, deg_prev = points[-2]
    d_last, deg_last = points[-1]
    rate = (d_last - d_prev) / (deg_last - deg_prev)  # ms per degree
    extra_deg = target_deg - deg_last
    return min(5000, int(d_last + extra_deg * rate))


def degrees_for_duration(duration_ms: int, direction: str = "right") -> float:
    """Return estimated degrees for a turn of given duration at 80% power."""
    if duration_ms <= 0:
        return 0.0

    points = RIGHT_TURN_POINTS if direction == "right" else LEFT_TURN_POINTS

    if duration_ms <= points[0][0]:
        ratio = duration_ms / points[0][0]
        return points[0][1] * ratio

    for i in range(len(points) - 1):
        d0, deg0 = points[i]
        d1, deg1 = points[i + 1]
        if duration_ms <= d1:
            frac = (duration_ms - d0) / (d1 - d0)
            return deg0 + frac * (deg1 - deg0)

    d_prev, deg_prev = points[-2]
    d_last, deg_last = points[-1]
    rate = (deg_last - deg_prev) / (d_last - d_prev)  # degrees per ms
    extra_ms = duration_ms - d_last
    return deg_last + extra_ms * rate


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

    # --- Expression: charges when recent moods have no matching gestures ---
    # Track a rolling window of the last 5 moods. If many are unmatched
    # in mood_gestures.json, expression pressure builds across ticks —
    # not just from the single most recent mood.
    try:
        gestures = json.loads(Path("/opt/kombucha/mood_gestures.json").read_text())
        mood = state.get("last_mood", "")
        recent_moods = list(state.get("_recent_moods", []))
        if mood and (not recent_moods or recent_moods[-1] != mood):
            recent_moods.append(mood)
            recent_moods = recent_moods[-5:]  # keep last 5
            state["_recent_moods"] = recent_moods
        # Count how many of the recent moods are unmatched
        unmatched = sum(1 for m in recent_moods if m and m not in gestures)
        if unmatched > 0 and len(recent_moods) > 0:
            # Charge proportionally: 1/5 unmatched = +0.06, 5/5 = +0.30
            charge = 0.06 * unmatched
            drives["expression"] = clamp01(drives["expression"] + charge)
        else:
            drives["expression"] = clamp01(
                drives["expression"] - 0.003 * eff_elapsed)
    except Exception:
        drives["expression"] = clamp01(
            drives["expression"] + DRIVE_CONFIG["expression"]["charge_rate"] * eff_elapsed)

    # --- Frustration: charges on failures, tracks source ---
    # Track what is causing frustration so we can relieve it proportionally
    # when the source clears.
    frustration_sources = set()
    state["_frustration_pre_charge"] = drives.get("frustration", 0)
    if sense:
        if sense.get("stuck", False):
            drives["frustration"] = clamp01(
                drives["frustration"] + DRIVE_CONFIG["frustration"]["charge_per_failure"])
            frustration_sources.add("stuck")
        # Camera explicitly dead — most reliable indicator
        if sense.get("camera_ok") is False:
            drives["frustration"] = clamp01(
                drives["frustration"] + 0.1 * (eff_elapsed / 300))
            frustration_sources.add("camera_dead")
        # Camera freeze detection (fps < 1 means frozen or dead)
        elif sense.get("faces", 0) == 0 and sense.get("gimbal_mode") == "instinct":
            # Instinct thinks there's a target but no faces — phantom/frozen
            drives["frustration"] = clamp01(
                drives["frustration"] + 0.05)
            frustration_sources.add("phantom_face")
        # Dead camera fallback: empty presence means YOLO sees nothing for 30s+.
        # Only if camera_ok is not explicitly True — otherwise just an empty room.
        elif sense.get("camera_ok") is not True and not sense.get("presence", {}) and eff_elapsed > 60:
            drives["frustration"] = clamp01(
                drives["frustration"] + 0.15 * (eff_elapsed / 300))
            frustration_sources.add("no_detections")

    # Track frustration sources and how long they've been active
    prev_sources = set(state.get("_frustration_sources", []))
    if frustration_sources:
        state["_frustration_sources"] = list(frustration_sources)
        if not prev_sources:
            state["_frustration_onset"] = time.time()

        # Habituation: the longer a source persists, the less it charges.
        # A new failure is urgent. A failure that's been there for 20 hours
        # is background noise. This models acceptance — not resignation,
        # but the reality that sustained frustration without new information
        # should plateau, not peg at 1.0 forever.
        onset = state.get("_frustration_onset")
        if onset:
            hours_active = (time.time() - onset) / 3600
            # Habituation factor: 1.0 at onset, decays to 0.2 over ~6 hours.
            # Formula: 0.2 + 0.8 / (1 + hours_active / 2)
            habituation = 0.2 + 0.8 / (1.0 + hours_active / 2.0)

            # Re-sensitization: after 24+ hours of sustained frustration,
            # habituation begins to wear off cyclically. You can only
            # get used to something for so long before it becomes newly
            # intolerable. 24-hour cycle: 12h rising, 12h falling.
            if hours_active > 24:
                cycle_hours = (hours_active - 24) % 24
                if cycle_hours < 12:
                    resensitize = 0.4 * (cycle_hours / 12.0)
                else:
                    resensitize = 0.4 * (1.0 - (cycle_hours - 12) / 12.0)
                habituation = min(1.0, habituation + resensitize)
        else:
            habituation = 1.0

        # Re-apply frustration charges scaled by habituation
        # (undo the full charges above, apply habituated versions)
        # Simpler: just scale the net charge from this update
        current = drives["frustration"]
        pre_charge = state.get("_frustration_pre_charge", current)
        charge_delta = current - pre_charge
        if charge_delta > 0:
            drives["frustration"] = clamp01(
                pre_charge + charge_delta * habituation)

        # Apply slow decay even while sources are active.
        decay_rate = DRIVE_CONFIG["frustration"]["decay_rate"]
        decay_amount = min(0.05, decay_rate * eff_elapsed)
        drives["frustration"] = clamp01(
            drives["frustration"] - decay_amount)
    else:
        # No active sources — use fast recovery rate instead of slow decay.
        # Use raw elapsed_s (not capped eff_elapsed) so recovery works across
        # long heartbeat intervals. 1h gap = 1h of recovery, not 5min.
        recovery_rate = DRIVE_CONFIG["frustration"]["recovery_rate"]
        drives["frustration"] = clamp01(
            drives["frustration"] - recovery_rate * elapsed_s)
        if drives["frustration"] == 0:
            state.pop("_frustration_sources", None)
            state.pop("_frustration_onset", None)

    state.pop("_frustration_pre_charge", None)
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
    elif drive_name == "frustration":
        # Relief scales with how long the frustration source was active.
        # Short frustration (< 5min): standard 0.3 drop.
        # Long frustration (hours): full reset — the relief of finally fixing it.
        onset = state.get("_frustration_onset")
        if onset:
            duration_h = (time.time() - onset) / 3600
            # Scale from 0.3 (just started) to 1.0 (persisted 1h+)
            relief = min(1.0, 0.3 + 0.7 * min(duration_h, 1.0))
        else:
            relief = amount
        drives["frustration"] = clamp01(drives.get("frustration", 0) - relief)
        if drives["frustration"] == 0:
            state.pop("_frustration_sources", None)
            state.pop("_frustration_onset", None)
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

    elif cmd == "plan":
        if len(sys.argv) < 3:
            print("Usage: drive_engine.py plan <distance_cm>")
            print("  Returns duration_ms needed for that distance at 80% power.")
            sys.exit(1)
        target = float(sys.argv[2])
        dur = duration_for_distance(target)
        est = distance_for_duration(dur)
        print(f"Target: {target:.1f}cm → duration: {dur}ms → estimated: {est:.1f}cm")

    elif cmd == "turn":
        if len(sys.argv) < 3:
            print("Usage: drive_engine.py turn <degrees> [left|right]")
            print("  Returns duration_ms needed for that turn at 80% power.")
            sys.exit(1)
        target = float(sys.argv[2])
        direction = sys.argv[3] if len(sys.argv) > 3 else "right"
        dur = duration_for_turn(target, direction)
        est = degrees_for_duration(dur, direction)
        print(f"Target: {target:.0f}deg {direction} → duration: {dur}ms → estimated: {est:.1f}deg")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
