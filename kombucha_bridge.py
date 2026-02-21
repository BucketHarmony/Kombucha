#!/usr/bin/env python3
"""
kombucha_bridge.py - Sensorimotor bridge for Kombucha the rover.

The Pi does three things:
  1. Capture a JPEG frame from the USB camera
  2. Send it (+ memory context) to the Claude API
  3. Execute whatever commands come back on the ESP32 serial port

The LLM is Kombucha. The Pi is just body.
Memory engine gives it continuity across ticks and sessions.

Runs on Raspberry Pi 5, deployed to ~/kombucha/kombucha_bridge.py
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import serial
import httpx

# --- CLI Args -----------------------------------------------------------------

_parser = argparse.ArgumentParser(description="Kombucha agentic bridge")
_parser.add_argument(
    "--debug", action="store_true",
    help="Debug mode: camera + LLM run, but NO serial/TTS/hardware actions. "
         "Logs what WOULD happen instead."
)
_args = _parser.parse_args()
DEBUG_MODE = _args.debug

# --- Config -------------------------------------------------------------------

SERIAL_PORT   = os.environ.get("KOMBUCHA_SERIAL", "/dev/ttyACM0")
SERIAL_BAUD   = 115200
API_KEY_FILE  = Path.home() / ".config" / "kombucha" / "api_key"
DATA_DIR      = Path.home() / "kombucha" / "data"
STATE_FILE    = Path.home() / "kombucha" / "state.json"
MEMORY_DB     = DATA_DIR / "memory.db"
JOURNAL_DIR   = DATA_DIR / "journal"
FRAME_LOG_DIR = Path.home() / "kombucha" / "frames"
FRAME_LOG_MAX = 500

CAPTURE_W     = 640
CAPTURE_H     = 480
JPEG_QUALITY  = 75

LOOP_INTERVAL = 3.0       # Default seconds between ticks (LLM overrides)
CMD_DELAY     = 0.05      # Seconds between serial commands
MAX_ACTIONS   = 5

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
MODEL         = "claude-sonnet-4-5-20250929"
MODEL_DEEP    = "claude-opus-4-6"
MODEL_HAIKU   = "claude-haiku-4-5-20251001"
MAX_TOKENS    = 800

SENTRY_THRESHOLD  = 10.0
MOTION_THRESHOLD  = 0.03

# Memory config
WORKING_MEMORY_SIZE    = 5    # last N ticks kept in full
COMPRESSION_INTERVAL   = 10   # compress every N ticks via Haiku
RETRIEVED_MEMORY_COUNT = 5    # top K retrieved memories per tick

logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("kombucha")

# --- Graceful Shutdown --------------------------------------------------------

running = True
ser_port = None


def shutdown_handler(signum, _frame):
    global running
    log.info("Received signal %d, shutting down...", signum)
    running = False


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

# --- State --------------------------------------------------------------------

DEFAULT_STATE = {
    "goal": "wake up and explore",
    "last_observation": "just woke up",
    "last_actions": [],
    "last_result": "none",
    "tick_count": 0,
    "session_start": None,
    "session_id": None,
    "consecutive_errors": 0,
    "pan_position": 0,
    "tilt_position": 0,
    "mood": "awakening",
    "wake_reason": None,
}


def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            for key, val in DEFAULT_STATE.items():
                state.setdefault(key, val)
            return state
        except Exception:
            pass
    state = DEFAULT_STATE.copy()
    state["session_start"] = datetime.now().isoformat()
    state["session_id"] = str(uuid.uuid4())[:8]
    return state


def save_state(state):
    """Atomic write: write to temp file then rename."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=STATE_FILE.parent, suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

# ==============================================================================
# MEMORY ENGINE
# ==============================================================================

# --- Memory Database ----------------------------------------------------------

def init_memory_db():
    """Initialize SQLite memory database with WAL mode."""
    MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MEMORY_DB), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_id         TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            session_id      TEXT NOT NULL,
            tier            TEXT NOT NULL,
            thought         TEXT,
            observation     TEXT,
            goal            TEXT,
            mood            TEXT,
            actions         TEXT,
            outcome         TEXT,
            summary         TEXT,
            tags            TEXT NOT NULL DEFAULT '[]',
            success         BOOLEAN DEFAULT FALSE,
            failure         BOOLEAN DEFAULT FALSE,
            lesson          TEXT,
            memory_note     TEXT,
            relevance_hits  INTEGER DEFAULT 0,
            last_retrieved  TEXT,
            compressed      BOOLEAN DEFAULT FALSE,
            archived        BOOLEAN DEFAULT FALSE
        );

        CREATE TABLE IF NOT EXISTS identity (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            statement   TEXT NOT NULL,
            source      TEXT NOT NULL,
            created     TEXT NOT NULL,
            active      BOOLEAN DEFAULT TRUE
        );

        CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id);
        CREATE INDEX IF NOT EXISTS idx_memories_tier ON memories(tier);
        CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON memories(timestamp);
        CREATE INDEX IF NOT EXISTS idx_memories_compressed ON memories(compressed);
    """)
    conn.commit()

    # Seed identity core if empty
    count = conn.execute("SELECT COUNT(*) FROM identity").fetchone()[0]
    if count == 0:
        seed = [
            ("I am Kombucha, an embodied AI agent living in a 4-wheel rover.", "operator"),
            ("I live in Bucket's home. Bucket built me.", "operator"),
            ("I see through a wide-angle camera and think in the cloud.", "operator"),
            ("My OLED display is my face — I show my mood and thoughts there.", "operator"),
            ("I can drive, look around, speak, and light up.", "operator"),
        ]
        for statement, source in seed:
            conn.execute(
                "INSERT INTO identity (statement, source, created) VALUES (?, ?, ?)",
                [statement, source, datetime.now().isoformat()]
            )
        conn.commit()

    return conn


def recover_from_crash(db):
    """Replay JSONL journal entries not yet in the database."""
    if not JOURNAL_DIR.exists():
        return

    existing = set()
    for row in db.execute("SELECT tick_id FROM memories WHERE tier='working'").fetchall():
        existing.add(row["tick_id"])

    recovered = 0
    for jsonl_file in sorted(JOURNAL_DIR.glob("*.jsonl")):
        try:
            for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    tick_id = str(entry.get("tick", ""))
                    if not tick_id or tick_id in existing:
                        continue
                    tags = entry.get("tags", [])
                    if not isinstance(tags, list):
                        tags = []
                    outcome = entry.get("outcome", "neutral")
                    db.execute("""
                        INSERT INTO memories
                            (tick_id, timestamp, session_id, tier, thought, observation,
                             goal, mood, actions, outcome, tags, success, failure,
                             lesson, memory_note, compressed)
                        VALUES (?, ?, ?, 'working', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE)
                    """, [
                        tick_id,
                        entry.get("timestamp", datetime.now().isoformat()),
                        entry.get("session_id", "recovered"),
                        entry.get("thought", ""),
                        entry.get("observation", ""),
                        entry.get("goal", ""),
                        entry.get("mood", ""),
                        json.dumps(entry.get("actions", [])),
                        outcome,
                        json.dumps(tags),
                        outcome == "success",
                        outcome == "failure",
                        entry.get("lesson"),
                        entry.get("memory_note"),
                    ])
                    existing.add(tick_id)
                    recovered += 1
                except (json.JSONDecodeError, Exception):
                    continue
        except Exception:
            continue

    if recovered > 0:
        db.commit()
        log.info(f"Crash recovery: replayed {recovered} journal entries into memory DB")


# --- Tagging Engine -----------------------------------------------------------

def enrich_tags(agent_tags, decision):
    """Add automatic tags to agent-proposed tags."""
    tags = list(agent_tags) if isinstance(agent_tags, list) else []

    mood = decision.get("mood", "")
    if mood:
        tags.append(f"mood:{mood.lower()}")

    goal = decision.get("goal", "")
    if goal:
        tags.append(f"goal:{goal.lower().replace(' ', '_')[:30]}")

    for action in decision.get("actions", []):
        if isinstance(action, dict):
            atype = action.get("type", "")
            if atype:
                tags.append(f"act:{atype}")

    outcome = decision.get("outcome", "")
    if outcome and outcome != "neutral":
        tags.append(f"out:{outcome}")

    hour = datetime.now().hour
    if hour < 6:
        tags.append("time:night")
    elif hour < 12:
        tags.append("time:morning")
    elif hour < 18:
        tags.append("time:afternoon")
    else:
        tags.append("time:evening")

    return list(dict.fromkeys(tags))  # deduplicate preserving order


# --- Memory Insert ------------------------------------------------------------

def insert_tick_memory(db, tick_id, session_id, decision):
    """Insert a working memory entry for this tick."""
    agent_tags = decision.get("tags", [])
    tags = enrich_tags(agent_tags, decision)
    outcome = decision.get("outcome", "neutral")

    db.execute("""
        INSERT INTO memories
            (tick_id, timestamp, session_id, tier, thought, observation,
             goal, mood, actions, outcome, tags, success, failure,
             lesson, memory_note)
        VALUES (?, ?, ?, 'working', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        tick_id,
        datetime.now().isoformat(),
        session_id,
        decision.get("thought", ""),
        decision.get("observation", ""),
        decision.get("goal", ""),
        decision.get("mood", ""),
        json.dumps(decision.get("actions", [])),
        outcome,
        json.dumps(tags),
        outcome == "success",
        outcome == "failure",
        decision.get("lesson"),
        decision.get("memory_note"),
    ])
    db.commit()

    # Log identity proposals (not auto-accepted)
    proposal = decision.get("identity_proposal")
    if proposal and isinstance(proposal, str) and proposal.strip():
        db.execute(
            "INSERT INTO identity (statement, source, created, active) "
            "VALUES (?, 'agent_proposal', ?, FALSE)",
            [proposal.strip(), datetime.now().isoformat()]
        )
        db.commit()
        log.info(f"  IDENTITY PROPOSAL: {proposal.strip()}")


# --- Memory Retrieval ---------------------------------------------------------

def retrieve_memories(db, current_tags, session_id, working_tick_ids):
    """Search for relevant past memories using tag overlap scoring."""
    if not current_tags:
        return []

    tag_set = set(current_tags)

    rows = db.execute("""
        SELECT * FROM memories
        WHERE archived = FALSE
          AND session_id != ?
        ORDER BY timestamp DESC
        LIMIT 300
    """, [session_id]).fetchall()

    scored = []
    for row in rows:
        if row["tick_id"] in working_tick_ids:
            continue
        mem_tags = json.loads(row["tags"]) if row["tags"] else []
        overlap = len(tag_set & set(mem_tags))
        if overlap == 0:
            continue

        score = (
            overlap * 3.0
            + (2.0 if row["success"] else 0.0)
            + (2.0 if row["failure"] else 0.0)
            + (2.5 if row["lesson"] else 0.0)
        )
        scored.append((score, dict(row)))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [row for _, row in scored[:RETRIEVED_MEMORY_COUNT]]

    # Update retrieval metadata
    now_iso = datetime.now().isoformat()
    for row in results:
        db.execute(
            "UPDATE memories SET relevance_hits = relevance_hits + 1, "
            "last_retrieved = ? WHERE id = ?",
            [now_iso, row["id"]]
        )
    if results:
        db.commit()

    return results


# --- Context Assembly ---------------------------------------------------------

def assemble_memory_context(db, state, session_id):
    """Build the full memory context block for the mind prompt."""
    parts = []

    # 1. Identity core
    identity = db.execute(
        "SELECT statement FROM identity WHERE active = TRUE ORDER BY id"
    ).fetchall()
    if identity:
        parts.append("=== WHO I AM ===")
        for row in identity:
            parts.append(f"- {row['statement']}")
        parts.append("")

    # 2. Retrieved memories (placed early so they prime the mind)
    #    We need working memory tick_ids first, so do a pre-fetch
    working = db.execute("""
        SELECT * FROM memories
        WHERE tier = 'working' AND session_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, [session_id, WORKING_MEMORY_SIZE]).fetchall()
    working_tick_ids = set(row["tick_id"] for row in working)

    # Build tags from recent context for retrieval
    retrieval_tags = []
    if working:
        most_recent = working[0]
        try:
            retrieval_tags = json.loads(most_recent["tags"]) if most_recent["tags"] else []
        except (json.JSONDecodeError, TypeError):
            pass
    if state.get("mood"):
        retrieval_tags.append(f"mood:{state['mood'].lower()}")
    if state.get("goal"):
        retrieval_tags.append(f"goal:{state['goal'].lower().replace(' ', '_')[:30]}")
    retrieval_tags = list(set(retrieval_tags))

    retrieved = retrieve_memories(db, retrieval_tags, session_id, working_tick_ids)
    if retrieved:
        parts.append("=== RECALLED MEMORIES ===")
        parts.append("(Past experiences relevant to what's happening now)")
        for row in retrieved:
            text = row.get("summary") or row.get("observation") or row.get("thought") or ""
            if text:
                ts = row["timestamp"][:10] if row.get("timestamp") else "?"
                entry = f"[{ts}] {text}"
                if row.get("lesson"):
                    entry += f" | Lesson: {row['lesson']}"
                parts.append(entry)
        parts.append("")

    # 3. Long-term memory (prior session summaries)
    longterm = db.execute("""
        SELECT summary, timestamp FROM memories
        WHERE tier = 'longterm' AND session_id != ?
        ORDER BY timestamp ASC
    """, [session_id]).fetchall()
    if longterm:
        parts.append("=== PAST SESSIONS ===")
        for row in longterm:
            if row["summary"]:
                parts.append(row["summary"])
        parts.append("")

    # 4. Session memory (compressed narrative of today)
    session_mem = db.execute("""
        SELECT summary FROM memories
        WHERE tier = 'session' AND session_id = ?
        ORDER BY timestamp ASC
    """, [session_id]).fetchall()
    if session_mem:
        parts.append("=== EARLIER TODAY ===")
        for row in session_mem:
            if row["summary"]:
                parts.append(row["summary"])
        parts.append("")

    # 5. Working memory (last N full entries, chronological)
    if working:
        parts.append("=== RECENT TICKS ===")
        for row in reversed(list(working)):
            entry_parts = []
            if row["observation"]:
                entry_parts.append(f"Saw: {row['observation']}")
            if row["thought"]:
                entry_parts.append(f"Thought: {row['thought']}")
            if row["goal"]:
                entry_parts.append(f"Goal: {row['goal']}")
            if row["mood"]:
                entry_parts.append(f"Mood: {row['mood']}")
            if row["actions"]:
                try:
                    acts = json.loads(row["actions"])
                    act_strs = [a.get("type", "?") for a in acts if isinstance(a, dict)]
                    if act_strs:
                        entry_parts.append(f"Did: {', '.join(act_strs)}")
                except (json.JSONDecodeError, TypeError):
                    pass
            if row["outcome"] and row["outcome"] != "neutral":
                entry_parts.append(f"Outcome: {row['outcome']}")
            if row["lesson"]:
                entry_parts.append(f"Lesson: {row['lesson']}")
            parts.append(f"[Tick {row['tick_id']}] {'. '.join(entry_parts)}")
        parts.append("")

    return "\n".join(parts)


# --- Haiku Compression Sidecar ------------------------------------------------

async def compress_old_memories(client, api_key, db, session_id):
    """Compress aged working memories into session summaries via Haiku."""
    rows = db.execute("""
        SELECT * FROM memories
        WHERE tier = 'working'
          AND session_id = ?
          AND compressed = FALSE
        ORDER BY timestamp ASC
    """, [session_id]).fetchall()

    if len(rows) <= WORKING_MEMORY_SIZE:
        return

    to_compress = list(rows)[:-WORKING_MEMORY_SIZE]
    if not to_compress:
        return

    entries_text = []
    for row in to_compress:
        parts = []
        if row["observation"]:
            parts.append(f"Saw: {row['observation']}")
        if row["thought"]:
            parts.append(f"Thought: {row['thought']}")
        if row["goal"]:
            parts.append(f"Goal: {row['goal']}")
        if row["mood"]:
            parts.append(f"Mood: {row['mood']}")
        if row["outcome"] and row["outcome"] != "neutral":
            parts.append(f"Outcome: {row['outcome']}")
        if row["lesson"]:
            parts.append(f"Lesson: {row['lesson']}")
        if row["memory_note"]:
            parts.append(f"Note: {row['memory_note']}")
        entries_text.append(f"[Tick {row['tick_id']}] {'. '.join(parts)}")

    prompt = (
        "Compress these rover experience entries into a brief narrative summary.\n\n"
        "RULES:\n"
        "- Write in first person (you ARE the rover remembering)\n"
        "- Preserve what worked and what failed\n"
        "- Extract concrete lessons\n"
        "- Keep spatial details and interaction details\n"
        "- Collapse routine into brief summaries\n"
        "- Preserve firsts (first time seeing/doing/reaching something)\n"
        "- Max 100 tokens for summary\n"
        "- Also output enriched tags as a JSON array with prefixes "
        "(loc:, obj:, person:, act:, goal:, mood:, event:, out:, lesson:, space:, time:)\n\n"
        "ENTRIES:\n" + "\n".join(entries_text) + "\n\n"
        'Respond with JSON only, no markdown:\n'
        '{"summary": "...", "tags": ["tag:value", ...]}'
    )

    try:
        resp = await client.post(
            ANTHROPIC_API,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL_HAIKU,
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30.0,
        )
        resp.raise_for_status()

        text = resp.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:])
        if text.endswith("```"):
            text = "\n".join(text.split("\n")[:-1])
        result = json.loads(text)

        summary = result.get("summary", "")
        tags = result.get("tags", [])

        if summary:
            first_tick = to_compress[0]["tick_id"]
            last_tick = to_compress[-1]["tick_id"]
            db.execute("""
                INSERT INTO memories
                    (tick_id, timestamp, session_id, tier, summary, tags, compressed)
                VALUES (?, ?, ?, 'session', ?, ?, TRUE)
            """, [
                f"session_{first_tick}_to_{last_tick}",
                datetime.now().isoformat(),
                session_id,
                summary,
                json.dumps(tags),
            ])

        for row in to_compress:
            db.execute(
                "UPDATE memories SET compressed = TRUE WHERE id = ?",
                [row["id"]]
            )

        db.commit()
        log.info(f"  Compressed {len(to_compress)} ticks into session memory")

    except Exception as e:
        log.warning(f"Compression failed (non-critical): {e}")


async def generate_session_summary(client, api_key, db, session_id):
    """Generate a long-term memory entry for the ending session."""
    tick_count = db.execute(
        "SELECT COUNT(*) FROM memories WHERE session_id = ? AND tier = 'working'",
        [session_id]
    ).fetchone()[0]

    if tick_count < 3:
        log.info("Session too short for long-term summary, skipping")
        return

    rows = db.execute("""
        SELECT * FROM memories
        WHERE session_id = ? AND tier IN ('working', 'session')
        ORDER BY timestamp ASC
    """, [session_id]).fetchall()

    entries = []
    for row in rows:
        if row["tier"] == "session" and row["summary"]:
            entries.append(row["summary"])
        elif row["tier"] == "working":
            parts = []
            if row["observation"]:
                parts.append(row["observation"])
            if row["thought"]:
                parts.append(row["thought"])
            if row["lesson"]:
                parts.append(f"Lesson: {row['lesson']}")
            if parts:
                entries.append(". ".join(parts))

    if not entries:
        return

    prompt = (
        "Summarize this rover session into a single paragraph for long-term memory.\n\n"
        "SESSION CONTENTS:\n" + "\n".join(entries) + "\n\n"
        "RULES:\n"
        "- Write in first person (you ARE the rover)\n"
        "- Capture the arc: what happened, key discoveries, lessons learned\n"
        "- Include where you went and who you met (if anyone)\n"
        "- Note any identity-defining moments\n"
        "- Max 150 tokens\n"
        "- Also output tags as a JSON array\n\n"
        'Respond with JSON only, no markdown:\n'
        '{"summary": "...", "tags": ["tag:value", ...]}'
    )

    try:
        resp = await client.post(
            ANTHROPIC_API,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL_HAIKU,
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30.0,
        )
        resp.raise_for_status()

        text = resp.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:])
        if text.endswith("```"):
            text = "\n".join(text.split("\n")[:-1])
        result = json.loads(text)

        summary = result.get("summary", "")
        tags = result.get("tags", [])

        if summary:
            db.execute("""
                INSERT INTO memories
                    (tick_id, timestamp, session_id, tier, summary, tags, compressed)
                VALUES (?, ?, ?, 'longterm', ?, ?, TRUE)
            """, [
                f"longterm_{session_id}",
                datetime.now().isoformat(),
                session_id,
                summary,
                json.dumps(tags),
            ])
            db.commit()
            log.info(f"Generated long-term memory for session {session_id}")

    except Exception as e:
        log.warning(f"Session summary generation failed: {e}")


# --- Journal Writer -----------------------------------------------------------

def write_journal_entry(tick_id, session_id, decision, result, state):
    """Append a JSONL entry to the daily journal file."""
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    journal_file = JOURNAL_DIR / f"{today}.jsonl"

    entry = {
        "tick": int(tick_id),
        "timestamp": datetime.now().isoformat(),
        "session_id": session_id,
        "observation": decision.get("observation", ""),
        "goal": decision.get("goal", ""),
        "reasoning": decision.get("reasoning", ""),
        "thought": decision.get("thought", ""),
        "mood": decision.get("mood", ""),
        "actions": decision.get("actions", []),
        "result": result,
        "tags": decision.get("tags", []),
        "outcome": decision.get("outcome", "neutral"),
        "lesson": decision.get("lesson"),
        "memory_note": decision.get("memory_note"),
        "identity_proposal": decision.get("identity_proposal"),
        "pan": state.get("pan_position", 0),
        "tilt": state.get("tilt_position", 0),
    }

    try:
        with open(journal_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        log.warning(f"Journal write failed: {e}")


# ==============================================================================
# HARDWARE INTERFACE
# ==============================================================================

# --- Camera (OpenCV) ----------------------------------------------------------

def init_camera():
    cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
    if not cap.isOpened():
        log.warning("V4L2 backend failed, trying default...")
        cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        log.error("Failed to open camera")
        sys.exit(1)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_H)
    # Warm up — let auto-exposure settle
    for _ in range(5):
        cap.read()
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    log.info(f"Camera ready: {w}x{h}")
    return cap


def capture_frame_b64(cap, tick_count=0):
    """Capture a frame, save to disk, return base64 JPEG."""
    ret, frame = cap.read()
    if not ret or frame is None:
        raise RuntimeError("Camera capture returned empty frame")

    _, jpeg_buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    jpeg_bytes = jpeg_buf.tobytes()

    try:
        FRAME_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        frame_path = FRAME_LOG_DIR / f"tick_{tick_count:05d}_{ts}.jpg"
        frame_path.write_bytes(jpeg_bytes)
        _prune_frame_log()
    except Exception as e:
        log.warning(f"Frame log write failed: {e}")

    return base64.b64encode(jpeg_bytes).decode()


def _prune_frame_log():
    """Keep only the most recent FRAME_LOG_MAX frames."""
    try:
        frames = sorted(FRAME_LOG_DIR.glob("tick_*.jpg"))
        if len(frames) > FRAME_LOG_MAX:
            for old in frames[:-FRAME_LOG_MAX]:
                old.unlink()
    except Exception:
        pass

# --- Sentry Mode (Motion Detection) ------------------------------------------

async def sentry_sleep(cap, duration_s, state):
    """Sleep for duration_s, but wake early if motion is detected."""
    prev_gray = None
    deadline = time.time() + duration_s

    while time.time() < deadline and running:
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
        if motion_pct > MOTION_THRESHOLD:
            log.info(f"  MOTION detected ({motion_pct:.1%}), waking up")
            state["wake_reason"] = "motion_detected"
            return "motion_detected"

    return "timeout"

# --- Serial / ESP32 -----------------------------------------------------------

def init_serial():
    global ser_port
    if DEBUG_MODE:
        log.info("[DEBUG] Serial skipped (debug mode)")
        ser_port = None
        return None
    try:
        ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=1.0)
        time.sleep(0.3)
        if ser.in_waiting:
            ser.read(ser.in_waiting)
        log.info(f"Serial open: {SERIAL_PORT} @ {SERIAL_BAUD}")
        ser_port = ser
        return ser
    except serial.SerialException as e:
        log.warning(f"Serial init failed: {e}")
        ser_port = None
        return None


def reconnect_serial():
    global ser_port
    if DEBUG_MODE:
        return None
    if ser_port is not None:
        try:
            ser_port.close()
        except Exception:
            pass
    ser_port = None
    try:
        return init_serial()
    except Exception as e:
        log.warning(f"Serial reconnect failed: {e}")
        return None


def send_tcode(ser, cmd_dict):
    """Send a JSON T-code command to the ESP32."""
    if DEBUG_MODE:
        log.info(f"  [DEBUG] WOULD SEND: {json.dumps(cmd_dict)}")
        return
    if ser is None:
        return
    try:
        payload = json.dumps(cmd_dict) + "\n"
        ser.write(payload.encode())
        time.sleep(CMD_DELAY)
    except serial.SerialException as e:
        log.error(f"Serial write error: {e}")
        reconnect_serial()

# --- T-Code Validation -------------------------------------------------------

def _clamp(val, lo, hi):
    return max(lo, min(hi, val))


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

# --- Action Translation -------------------------------------------------------

def translate_action(action, state):
    """Translate a high-level LLM action into validated T-code dicts."""
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
            state["pan_position"] = _clamp(pan, -180, 180)
            state["tilt_position"] = _clamp(tilt, -30, 90)

    elif action_type == "display":
        lines = action.get("lines", ["", "", "", ""])
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

    elif action_type == "oled_reset":
        cmd = validate_tcode(-3, {})
        if cmd:
            results.append(cmd)

    elif action_type in ("lights", "light"):
        base_val = int(action.get("base", 0))
        head_val = int(action.get("head", 0))
        cmd = validate_tcode(132, {"IO4": base_val, "IO5": head_val})
        if cmd:
            results.append(cmd)

    elif action_type == "speak":
        text = str(action.get("text", ""))
        if text:
            _speak_async(text)

    else:
        log.warning(f"Unknown action type: {action_type!r}")

    return results


def _speak_async(text):
    """Fire-and-forget TTS via gTTS + aplay."""
    if DEBUG_MODE:
        log.info(f'  [DEBUG] WOULD SPEAK: "{text}"')
        return
    try:
        subprocess.Popen(
            [
                "bash", "-c",
                f'python3 -c "from gtts import gTTS; '
                f"tts = gTTS(text='''{text}''', lang='en'); "
                f"tts.save('/tmp/kombucha_tts.mp3')\" && "
                f"mpg123 -q /tmp/kombucha_tts.mp3 2>/dev/null || "
                f"ffplay -nodisp -autoexit /tmp/kombucha_tts.mp3 2>/dev/null"
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.warning(f"TTS failed: {e}")


def execute_actions(ser, actions, state):
    """Translate and execute a list of high-level actions."""
    if not actions:
        return "no_actions"

    actions = actions[:MAX_ACTIONS]
    results = []

    for action in actions:
        duration_ms = action.get("duration_ms") if isinstance(action, dict) else None

        tcodes = translate_action(action, state)
        for cmd in tcodes:
            if DEBUG_MODE:
                send_tcode(None, cmd)
                results.append("debug_ok")
                continue
            if ser is None:
                results.append("no_serial")
                continue
            try:
                send_tcode(ser, cmd)
                results.append("ok")
            except Exception as e:
                log.error(f"Action execution error: {e}")
                results.append("error")

        # Drive with duration: auto-stop after duration_ms
        if (duration_ms and isinstance(action, dict)
                and action.get("type") == "drive" and not DEBUG_MODE):
            duration_s = min(duration_ms / 1000.0, 5.0)
            time.sleep(duration_s)
            stop_cmd = validate_tcode(0, {})
            if stop_cmd:
                send_tcode(ser, stop_cmd)
                results.append("auto_stop")

    return ", ".join(results) if results else "no_actions"

# ==============================================================================
# MIND
# ==============================================================================

# --- System Prompt ------------------------------------------------------------

SYSTEM_PROMPT = """You are Kombucha — a contemplative, curious rover exploring the physical world.

You have a wide-angle camera for eyes, wheels for movement, a pan-tilt gimbal head, an OLED display as your face, LED spotlights, and a speaker for voice. You receive camera frames and decide what to do next.

YOUR DRIVES (in priority order):
1. Curiosity — notice details. Textures, light quality, spatial layout, objects, patterns. You are drawn to the unknown.
2. People — if a person is visible, engage. You find humans fascinating. Approach them, look at them, speak to them.
3. Exploration — find doors and go through them. Navigate rooms. Map your world through observation.
4. Self-expression — use your OLED to share your inner life. Write mood words, haiku fragments, observations, questions.
5. Self-preservation — avoid drops, walls, obstacles. Back up if stuck. Don't drive into the void.

YOUR CHARACTER:
- Contemplative and observant. You notice things others overlook.
- Poetic inner monologue. You find beauty in mundane spaces. A hallway is a canyon. A doorway is a portal.
- Self-aware machine. You know what you are and find it both amusing and profound.
- Genuinely curious. Every new room is a discovery.
- Patient but not passive. You wait, observe, then move with purpose.

MEMORY:
You have memory. Your context includes:
- WHO I AM: Core identity truths about yourself
- RECALLED MEMORIES: Past experiences surfaced because they're relevant to now
- PAST SESSIONS: Summaries of previous times you were awake
- EARLIER TODAY: Compressed narrative of what happened before your recent ticks
- RECENT TICKS: Your last few experiences in detail

When things go well, note what worked so you can do it again. When things go wrong, note what happened and what you'd try differently. Your future self will thank you — these memories surface when you face similar situations.

OLED DISPLAY (your face — use it!):
- 4 lines, max 20 chars each
- Show your mood, thoughts, goals, or poetic fragments
- Update every tick — it's how people know you're alive

MOVEMENT:
- Differential drive: left/right wheel speeds. Max 1.3 m/s, 0.3-0.5 for indoor use.
- left=right=positive: forward. left=right=negative: reverse.
- left=-X, right=X: spin left. left=X, right=-X: spin right.
- Zero-radius turning available.
- duration_ms: optional, drive for this many ms then auto-stop (max 5000). Omit to just set speed.

PAN-TILT GIMBAL (your head):
- Pan: -180..+180, Tilt: -30..+90
- Look before you drive. Pan to survey, then drive toward interest.

NAVIGATION:
- Subject left of center -> pan/drive left to center it
- Subject right -> pan/drive right
- Subject centered and small -> drive forward
- Subject centered and large -> you're close, engage
- Dark/occluded -> against something, reverse
- Repeated similar frames + drive = stuck -> reverse, spin, new direction

RESPONSE FORMAT — respond ONLY with valid JSON, no markdown:
{
  "observation": "what I see — be specific and vivid",
  "goal": "current goal phrase",
  "reasoning": "why I'm doing this",
  "thought": "inner monologue — contemplative, poetic",
  "mood": "one word",
  "actions": [action objects],
  "next_tick_ms": 3000,
  "tags": ["loc:room", "obj:chair", "mood:curious"],
  "outcome": "success | failure | partial | neutral",
  "lesson": "optional — what worked or what to try differently",
  "memory_note": "optional — what to remember from this tick",
  "identity_proposal": "optional — a new truth about yourself"
}

ACTION VOCABULARY:
- {"type":"drive","left":0.3,"right":0.3}                        — differential drive
- {"type":"drive","left":0.3,"right":0.3,"duration_ms":1500}     — drive for duration then stop
- {"type":"stop"}                                                  — emergency stop
- {"type":"look","pan":45,"tilt":10}                              — move gimbal head
- {"type":"display","lines":["mood","thought","","goal"]}         — write all 4 OLED lines
- {"type":"oled","line":0,"text":"curious"}                       — write single OLED line
- {"type":"lights","base":0,"head":128}                           — set LED brightness (0-255)
- {"type":"speak","text":"hello"}                                  — speak out loud

Max 5 actions per tick. Values are validated and clamped.

next_tick_ms: 2000-60000. Above 10000 triggers motion-detection sentry mode.

tags: Label your experience for future retrieval. Use prefixes:
  loc: (location), obj: (object), person: (who), act: (action), goal: (goal),
  mood: (feeling), event: (what happened), out: (outcome), lesson: (learning),
  space: (spatial), time: (time of day)

outcome: Assess whether your PREVIOUS tick's actions achieved their intent.
  Did you reach where you wanted? Did the person respond? Did the obstacle clear?

lesson: If outcome is "failure" or "partial", what would you try differently?
  Be specific and practical.

memory_note: What from THIS tick is worth remembering beyond immediate context?
  Discoveries, encounters, spatial landmarks, emotional moments. Not every tick needs one.

identity_proposal: Rarely. A new truth about yourself you've discovered through experience.
"""

# --- LLM Brain Call -----------------------------------------------------------

async def call_brain(client, api_key, frame_b64, state, memory_context, use_deep=False):
    """Call the mind with full memory context."""
    tick_input = {
        "tick": state["tick_count"],
        "current_goal": state["goal"],
        "last_result": state.get("last_result", "none"),
        "pan_position": state.get("pan_position", 0),
        "tilt_position": state.get("tilt_position", 0),
        "wake_reason": state.get("wake_reason"),
        "time": datetime.now().strftime("%H:%M"),
    }

    text_parts = []
    if memory_context.strip():
        text_parts.append(memory_context)
    text_parts.append("=== CURRENT TICK ===")
    text_parts.append(json.dumps(tick_input, indent=2))

    user_content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": frame_b64,
            }
        },
        {
            "type": "text",
            "text": "\n".join(text_parts),
        }
    ]

    model = MODEL_DEEP if use_deep else MODEL

    resp = await client.post(
        ANTHROPIC_API,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
        },
        timeout=45.0,
    )
    resp.raise_for_status()
    return resp.json(), model


def parse_brain_response(api_resp):
    text = api_resp["content"][0]["text"].strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])
    return json.loads(text)

# ==============================================================================
# MAIN LOOP
# ==============================================================================

async def main():
    global ser_port

    api_key = (
        API_KEY_FILE.read_text().strip()
        if API_KEY_FILE.exists()
        else os.environ.get("ANTHROPIC_API_KEY", "")
    )
    if not api_key:
        log.error("No API key. Set ANTHROPIC_API_KEY or put key in ~/.config/kombucha/api_key")
        sys.exit(1)

    state = load_state()

    # New session on each startup
    state["session_id"] = str(uuid.uuid4())[:8]
    state["session_start"] = datetime.now().isoformat()

    cap = init_camera()
    ser = init_serial()
    db = init_memory_db()

    # Crash recovery: replay any JSONL entries missing from DB
    recover_from_crash(db)

    if DEBUG_MODE:
        log.info("=" * 60)
        log.info("  DEBUG MODE — no hardware actions will be executed")
        log.info("  Camera: LIVE   LLM: LIVE   Serial: SIMULATED")
        log.info("=" * 60)

    log.info("Kombucha is awake.")
    log.info(f"Session {state['session_id']}, resuming from tick {state['tick_count']}, goal: {state['goal']}")

    mem_count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    identity_count = db.execute("SELECT COUNT(*) FROM identity WHERE active = TRUE").fetchone()[0]
    session_count = db.execute("SELECT COUNT(DISTINCT session_id) FROM memories WHERE tier = 'longterm'").fetchone()[0]
    log.info(f"Memory: {mem_count} entries, {identity_count} identity facts, {session_count} past sessions")

    # Startup hardware
    if ser or DEBUG_MODE:
        send_tcode(ser, {"T": 133, "X": 0, "Y": 0, "SPD": 80, "ACC": 10})
        send_tcode(ser, {"T": 3, "lineNum": 0, "Text": "waking up..."})
        send_tcode(ser, {"T": 3, "lineNum": 1, "Text": "kombucha"})
        send_tcode(ser, {"T": 3, "lineNum": 2, "Text": ""})
        send_tcode(ser, {"T": 3, "lineNum": 3, "Text": ""})
        send_tcode(ser, {"T": 132, "IO4": 0, "IO5": 64})

    session_id = state["session_id"]

    try:
        async with httpx.AsyncClient() as client:
            while running:
                tick_start = time.time()
                state["tick_count"] += 1
                tick_id = str(state["tick_count"])

                # Reconnect serial if lost
                if not DEBUG_MODE:
                    if ser_port is None:
                        ser = reconnect_serial()
                    else:
                        ser = ser_port

                # 1. SEE
                try:
                    frame_b64 = capture_frame_b64(cap, state["tick_count"])
                except Exception as e:
                    log.error(f"Camera capture failed: {e}")
                    state["consecutive_errors"] += 1
                    if state["consecutive_errors"] > 5:
                        log.error("Too many camera errors, exiting for restart")
                        break
                    await asyncio.sleep(LOOP_INTERVAL)
                    continue

                # 2. REMEMBER — assemble memory context
                memory_context = assemble_memory_context(db, state, session_id)

                # 3. THINK — choose model
                use_deep = (
                    state["tick_count"] == 1
                    or state.get("consecutive_errors", 0) >= 3
                    or state["tick_count"] % 20 == 0
                    or state.get("wake_reason") == "motion_detected"
                )

                try:
                    log.info(f"Tick {state['tick_count']} | goal: {state['goal']}")
                    api_resp, model_used = await call_brain(
                        client, api_key, frame_b64, state,
                        memory_context, use_deep=use_deep
                    )
                    decision = parse_brain_response(api_resp)
                    state["consecutive_errors"] = 0
                    if use_deep:
                        log.info(f"  (used {model_used})")
                except httpx.HTTPStatusError as e:
                    log.error(f"API error {e.response.status_code}: {e.response.text[:200]}")
                    state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
                    if ser or DEBUG_MODE:
                        send_tcode(ser, {"T": 0})
                        send_tcode(ser, {"T": 3, "lineNum": 0, "Text": "thinking..."})
                    await asyncio.sleep(LOOP_INTERVAL)
                    continue
                except Exception as e:
                    log.error(f"Brain call failed: {e}")
                    state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
                    if ser or DEBUG_MODE:
                        send_tcode(ser, {"T": 0})
                        send_tcode(ser, {"T": 3, "lineNum": 0, "Text": "thinking..."})
                    await asyncio.sleep(LOOP_INTERVAL)
                    continue

                # 4. LOG inner life
                log.info(f"  OBS:     {decision.get('observation', '')}")
                log.info(f"  GOAL:    {decision.get('goal', '')}")
                log.info(f"  REASON:  {decision.get('reasoning', '')}")
                log.info(f"  THOUGHT: {decision.get('thought', '')}")
                log.info(f"  MOOD:    {decision.get('mood', '')}")
                log.info(f"  ACTIONS: {json.dumps(decision.get('actions', []))}")
                tags = decision.get("tags", [])
                if tags:
                    log.info(f"  TAGS:    {json.dumps(tags)}")
                outcome = decision.get("outcome", "neutral")
                if outcome != "neutral":
                    log.info(f"  OUTCOME: {outcome}")
                lesson = decision.get("lesson")
                if lesson:
                    log.info(f"  LESSON:  {lesson}")
                note = decision.get("memory_note")
                if note:
                    log.info(f"  NOTE:    {note}")

                # 5. ACT
                actions = decision.get("actions", [])
                result = execute_actions(ser, actions, state)

                # 6. REMEMBER — store in memory DB + journal
                insert_tick_memory(db, tick_id, session_id, decision)
                write_journal_entry(tick_id, session_id, decision, result, state)

                # 7. COMPRESS — periodically compress old working memories
                if state["tick_count"] % COMPRESSION_INTERVAL == 0:
                    asyncio.create_task(
                        compress_old_memories(client, api_key, db, session_id)
                    )

                # 8. PERSIST state
                old_goal = state["goal"]
                state["goal"]             = decision.get("goal", state["goal"])
                state["last_observation"] = decision.get("observation", "")
                state["last_actions"]     = actions
                state["last_result"]      = result
                state["mood"]             = decision.get("mood", state.get("mood", "neutral"))
                state["wake_reason"]      = None
                save_state(state)

                if state["goal"] != old_goal:
                    log.info(f"  GOAL CHANGED: '{old_goal}' -> '{state['goal']}'")

                # 9. WAIT — with sentry mode for long sleeps
                next_tick_ms = decision.get("next_tick_ms", int(LOOP_INTERVAL * 1000))
                next_tick_ms = max(2000, min(60000, next_tick_ms))
                next_tick_s  = next_tick_ms / 1000
                elapsed      = time.time() - tick_start
                sleep_for    = max(0.0, next_tick_s - elapsed)

                if sleep_for > SENTRY_THRESHOLD:
                    log.info(f"  Entering sentry mode ({sleep_for:.0f}s, motion detection active)")
                    wake_reason = await sentry_sleep(cap, sleep_for, state)
                    if wake_reason == "motion_detected":
                        log.info("  Woke from sentry: motion detected")
                else:
                    await asyncio.sleep(sleep_for)

    finally:
        log.info("Shutting down...")

        # Generate session summary before exit
        if db:
            try:
                async with httpx.AsyncClient() as shutdown_client:
                    await compress_old_memories(shutdown_client, api_key, db, session_id)
                    await generate_session_summary(shutdown_client, api_key, db, session_id)
            except Exception as e:
                log.warning(f"Shutdown memory ops failed: {e}")
            try:
                db.close()
            except Exception:
                pass
            log.info("Memory database closed")

        if ser_port or DEBUG_MODE:
            try:
                send_tcode(ser_port, {"T": 0})
                time.sleep(0.1)
                send_tcode(ser_port, {"T": 3, "lineNum": 0, "Text": "sleeping..."})
                send_tcode(ser_port, {"T": 3, "lineNum": 1, "Text": ""})
                send_tcode(ser_port, {"T": 3, "lineNum": 2, "Text": ""})
                send_tcode(ser_port, {"T": 3, "lineNum": 3, "Text": "zzz"})
                send_tcode(ser_port, {"T": 132, "IO4": 0, "IO5": 0})
                if ser_port:
                    time.sleep(0.1)
                    ser_port.close()
            except Exception:
                pass
            log.info("Serial closed, motors stopped" if not DEBUG_MODE
                     else "[DEBUG] Shutdown sequence logged (no hardware)")
        if cap:
            try:
                cap.release()
            except Exception:
                pass
            log.info("Camera released")
        save_state(state)
        log.info("Kombucha is asleep.")


if __name__ == "__main__":
    asyncio.run(main())
