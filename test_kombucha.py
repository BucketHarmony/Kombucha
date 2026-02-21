#!/usr/bin/env python3
"""
test_kombucha.py — Minimal test suite for Kombucha bridge + memory engine.

Runs without hardware: no serial, no camera, no API calls.

    python -m pytest test_kombucha.py -v
"""

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import bridge module with mocked hardware deps
# ---------------------------------------------------------------------------

# Mock hardware modules so we can import the bridge on any machine
# (cv2, serial, numpy, httpx may not be installed in test env)

import types

MOCK_MODULES = {}

for mod_name in ("cv2", "serial", "numpy", "httpx"):
    if mod_name not in sys.modules:
        MOCK_MODULES[mod_name] = types.ModuleType(mod_name)
        sys.modules[mod_name] = MOCK_MODULES[mod_name]

# numpy needs count_nonzero, GaussianBlur etc — just give it attr access
if "numpy" in MOCK_MODULES:
    MOCK_MODULES["numpy"].count_nonzero = lambda x: 0

# cv2 needs some constants
if "cv2" in MOCK_MODULES:
    cv2_mock = MOCK_MODULES["cv2"]
    cv2_mock.CAP_V4L2 = 200
    cv2_mock.CAP_PROP_FOURCC = 6
    cv2_mock.CAP_PROP_FRAME_WIDTH = 3
    cv2_mock.CAP_PROP_FRAME_HEIGHT = 4
    cv2_mock.IMWRITE_JPEG_QUALITY = 1
    cv2_mock.COLOR_BGR2GRAY = 6
    cv2_mock.THRESH_BINARY = 0
    cv2_mock.VideoWriter_fourcc = lambda *a: 0
    cv2_mock.VideoCapture = lambda *a, **k: None
    cv2_mock.imencode = lambda *a, **k: (True, type("buf", (), {"tobytes": lambda self: b"\xff\xd8"})())
    cv2_mock.cvtColor = lambda *a: a[0]
    cv2_mock.GaussianBlur = lambda *a: a[0]
    cv2_mock.absdiff = lambda *a: a[0]
    cv2_mock.threshold = lambda *a: (0, a[0])

# httpx needs AsyncClient
if "httpx" in MOCK_MODULES:
    httpx_mock = MOCK_MODULES["httpx"]
    httpx_mock.HTTPStatusError = Exception
    httpx_mock.AsyncClient = type("AsyncClient", (), {
        "__aenter__": lambda self: self,
        "__aexit__": lambda self, *a: None,
    })

# serial needs SerialException
if "serial" in MOCK_MODULES:
    serial_mock = MOCK_MODULES["serial"]
    serial_mock.SerialException = Exception
    serial_mock.Serial = lambda *a, **k: None

# Patch sys.argv before importing bridge (it runs argparse at import time)
with patch("sys.argv", ["kombucha_bridge.py", "--debug"]):
    import kombucha_bridge as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temp directory and patch bridge paths to use it."""
    old_db = kb.MEMORY_DB
    old_journal = kb.JOURNAL_DIR
    old_state = kb.STATE_FILE
    old_frames = kb.FRAME_LOG_DIR

    kb.MEMORY_DB = tmp_path / "memory.db"
    kb.JOURNAL_DIR = tmp_path / "journal"
    kb.STATE_FILE = tmp_path / "state.json"
    kb.FRAME_LOG_DIR = tmp_path / "frames"

    yield tmp_path

    kb.MEMORY_DB = old_db
    kb.JOURNAL_DIR = old_journal
    kb.STATE_FILE = old_state
    kb.FRAME_LOG_DIR = old_frames


@pytest.fixture
def db(tmp_dir):
    """Initialize a fresh memory database."""
    conn = kb.init_memory_db()
    yield conn
    conn.close()


@pytest.fixture
def sample_decision():
    """A sample mind output for testing."""
    return {
        "observation": "I see a bright hallway with a doorway at the end",
        "goal": "explore the hallway",
        "reasoning": "The doorway looks interesting, I want to see what's beyond",
        "thought": "Light spills through the doorway like an invitation",
        "mood": "curious",
        "actions": [
            {"type": "drive", "left": 0.3, "right": 0.3},
            {"type": "oled", "line": 0, "text": "curious"},
        ],
        "next_tick_ms": 5000,
        "tags": ["loc:hallway", "obj:doorway", "space:open_area"],
        "outcome": "success",
        "lesson": "Driving straight at 0.3 works well in open hallways",
        "memory_note": "Found a new hallway with natural light",
        "identity_proposal": None,
    }


# ===========================================================================
# Memory Database Tests
# ===========================================================================

class TestMemoryDB:
    def test_init_creates_tables(self, db):
        """DB init creates memories and identity tables."""
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "memories" in tables
        assert "identity" in tables

    def test_init_seeds_identity(self, db):
        """DB init seeds 5 identity statements."""
        count = db.execute("SELECT COUNT(*) FROM identity WHERE active=TRUE").fetchone()[0]
        assert count == 5

    def test_init_idempotent(self, db):
        """Calling init_memory_db again doesn't duplicate seed data."""
        db2 = kb.init_memory_db()
        count = db2.execute("SELECT COUNT(*) FROM identity WHERE active=TRUE").fetchone()[0]
        assert count == 5
        db2.close()

    def test_wal_mode(self, db):
        """DB uses WAL journal mode."""
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"


# ===========================================================================
# Tagging Engine Tests
# ===========================================================================

class TestTagging:
    def test_enrich_tags_adds_mood(self):
        tags = kb.enrich_tags([], {"mood": "Curious", "actions": []})
        assert "mood:curious" in tags

    def test_enrich_tags_adds_goal(self):
        tags = kb.enrich_tags([], {"goal": "find the door", "actions": []})
        assert any(t.startswith("goal:") for t in tags)

    def test_enrich_tags_adds_action_types(self):
        tags = kb.enrich_tags([], {
            "actions": [{"type": "drive"}, {"type": "look"}]
        })
        assert "act:drive" in tags
        assert "act:look" in tags

    def test_enrich_tags_adds_outcome(self):
        tags = kb.enrich_tags([], {"outcome": "failure", "actions": []})
        assert "out:failure" in tags

    def test_enrich_tags_skips_neutral_outcome(self):
        tags = kb.enrich_tags([], {"outcome": "neutral", "actions": []})
        assert "out:neutral" not in tags

    def test_enrich_tags_adds_time(self):
        tags = kb.enrich_tags([], {"actions": []})
        time_tags = [t for t in tags if t.startswith("time:")]
        assert len(time_tags) == 1

    def test_enrich_tags_preserves_agent_tags(self):
        tags = kb.enrich_tags(["loc:kitchen", "person:bucket"], {"actions": []})
        assert "loc:kitchen" in tags
        assert "person:bucket" in tags

    def test_enrich_tags_deduplicates(self):
        tags = kb.enrich_tags(
            ["mood:curious"],
            {"mood": "curious", "actions": []}
        )
        assert tags.count("mood:curious") == 1


# ===========================================================================
# Memory Insert & Retrieve Tests
# ===========================================================================

class TestMemoryInsertRetrieve:
    def test_insert_tick_memory(self, db, sample_decision):
        kb.insert_tick_memory(db, "1", "sess_abc", sample_decision)
        row = db.execute("SELECT * FROM memories WHERE tick_id='1'").fetchone()
        assert row is not None
        assert row["tier"] == "working"
        assert row["mood"] == "curious"
        assert row["success"] == 1
        assert row["lesson"] == "Driving straight at 0.3 works well in open hallways"

    def test_insert_stores_tags(self, db, sample_decision):
        kb.insert_tick_memory(db, "1", "sess_abc", sample_decision)
        row = db.execute("SELECT tags FROM memories WHERE tick_id='1'").fetchone()
        tags = json.loads(row["tags"])
        assert "loc:hallway" in tags
        assert "obj:doorway" in tags
        # Auto-enriched tags should also be present
        assert any(t.startswith("mood:") for t in tags)

    def test_insert_logs_identity_proposal(self, db):
        decision = {
            "observation": "test",
            "goal": "test",
            "mood": "curious",
            "actions": [],
            "tags": [],
            "outcome": "neutral",
            "identity_proposal": "I am drawn to natural light",
        }
        kb.insert_tick_memory(db, "1", "sess_abc", decision)
        proposals = db.execute(
            "SELECT * FROM identity WHERE source='agent_proposal'"
        ).fetchall()
        assert len(proposals) == 1
        assert proposals[0]["statement"] == "I am drawn to natural light"
        assert proposals[0]["active"] == 0  # not auto-accepted

    def test_retrieve_from_other_sessions(self, db, sample_decision):
        # Insert a memory in session A
        kb.insert_tick_memory(db, "1", "sess_A", sample_decision)

        # Retrieve from session B with matching tags
        results = kb.retrieve_memories(
            db,
            current_tags=["loc:hallway", "mood:curious"],
            session_id="sess_B",
            working_tick_ids=set(),
        )
        assert len(results) >= 1
        assert results[0]["tick_id"] == "1"

    def test_retrieve_excludes_current_session(self, db, sample_decision):
        kb.insert_tick_memory(db, "1", "sess_A", sample_decision)

        results = kb.retrieve_memories(
            db,
            current_tags=["loc:hallway"],
            session_id="sess_A",  # same session
            working_tick_ids=set(),
        )
        assert len(results) == 0

    def test_retrieve_scores_lessons_higher(self, db):
        # Memory with lesson
        decision_with_lesson = {
            "observation": "wall",
            "goal": "navigate",
            "mood": "cautious",
            "actions": [{"type": "drive"}],
            "tags": ["loc:hallway"],
            "outcome": "failure",
            "lesson": "Reverse before turning in narrow spaces",
        }
        kb.insert_tick_memory(db, "1", "sess_old", decision_with_lesson)

        # Memory without lesson
        decision_no_lesson = {
            "observation": "hallway",
            "goal": "explore",
            "mood": "curious",
            "actions": [{"type": "drive"}],
            "tags": ["loc:hallway"],
            "outcome": "neutral",
        }
        kb.insert_tick_memory(db, "2", "sess_old", decision_no_lesson)

        results = kb.retrieve_memories(
            db,
            current_tags=["loc:hallway"],
            session_id="sess_new",
            working_tick_ids=set(),
        )
        # The one with lesson should score higher
        assert len(results) >= 2
        assert results[0]["lesson"] is not None

    def test_retrieve_empty_tags_returns_nothing(self, db, sample_decision):
        kb.insert_tick_memory(db, "1", "sess_A", sample_decision)
        results = kb.retrieve_memories(db, [], "sess_B", set())
        assert results == []


# ===========================================================================
# Context Assembly Tests
# ===========================================================================

class TestContextAssembly:
    def test_context_includes_identity(self, db):
        state = {"mood": "curious", "goal": "explore"}
        ctx = kb.assemble_memory_context(db, state, "sess_test")
        assert "WHO I AM" in ctx
        assert "Kombucha" in ctx

    def test_context_includes_working_memory(self, db, sample_decision):
        kb.insert_tick_memory(db, "1", "sess_test", sample_decision)
        state = {"mood": "curious", "goal": "explore"}
        ctx = kb.assemble_memory_context(db, state, "sess_test")
        assert "RECENT TICKS" in ctx
        assert "hallway" in ctx

    def test_context_empty_session(self, db):
        state = {"mood": "awakening", "goal": "wake up"}
        ctx = kb.assemble_memory_context(db, state, "sess_new")
        # Should still have identity, no crash
        assert "WHO I AM" in ctx
        assert "RECENT TICKS" not in ctx


# ===========================================================================
# T-Code Validation Tests
# ===========================================================================

class TestTCodeValidation:
    def test_emergency_stop(self):
        result = kb.validate_tcode(0, {})
        assert result == {"T": 0}

    def test_drive_clamped(self):
        result = kb.validate_tcode(1, {"L": 5.0, "R": -5.0})
        assert result["L"] == 1.3
        assert result["R"] == -1.3

    def test_oled_text_truncated(self):
        result = kb.validate_tcode(3, {
            "lineNum": 0,
            "Text": "a" * 50
        })
        assert len(result["Text"]) == 20

    def test_oled_line_clamped(self):
        result = kb.validate_tcode(3, {"lineNum": 10, "Text": "hi"})
        assert result["lineNum"] == 3

    def test_led_clamped(self):
        result = kb.validate_tcode(132, {"IO4": 999, "IO5": -10})
        assert result["IO4"] == 255
        assert result["IO5"] == 0

    def test_gimbal_clamped(self):
        result = kb.validate_tcode(133, {
            "X": 999, "Y": 999, "SPD": 999, "ACC": 999
        })
        assert result["X"] == 180
        assert result["Y"] == 90
        assert result["SPD"] == 200
        assert result["ACC"] == 50

    def test_unknown_tcode_blocked(self):
        result = kb.validate_tcode(999, {})
        assert result is None

    def test_invalid_params_returns_none(self):
        result = kb.validate_tcode(1, {"L": "not_a_number"})
        assert result is None


# ===========================================================================
# Action Translation Tests
# ===========================================================================

class TestActionTranslation:
    def test_drive_action(self):
        state = {}
        cmds = kb.translate_action({"type": "drive", "left": 0.5, "right": 0.5}, state)
        assert len(cmds) == 1
        assert cmds[0]["T"] == 1
        assert cmds[0]["L"] == 0.5

    def test_stop_action(self):
        cmds = kb.translate_action({"type": "stop"}, {})
        assert len(cmds) == 1
        assert cmds[0]["T"] == 0

    def test_look_updates_state(self):
        state = {"pan_position": 0, "tilt_position": 0}
        cmds = kb.translate_action({"type": "look", "pan": 45, "tilt": 10}, state)
        assert len(cmds) == 1
        assert cmds[0]["X"] == 45
        assert state["pan_position"] == 45
        assert state["tilt_position"] == 10

    def test_display_produces_four_commands(self):
        cmds = kb.translate_action({
            "type": "display",
            "lines": ["curious", "exploring", "the hall", "onward"]
        }, {})
        assert len(cmds) == 4
        assert all(c["T"] == 3 for c in cmds)
        assert cmds[0]["Text"] == "curious"
        assert cmds[3]["Text"] == "onward"

    def test_oled_single_line(self):
        cmds = kb.translate_action({"type": "oled", "line": 2, "text": "hello"}, {})
        assert len(cmds) == 1
        assert cmds[0]["lineNum"] == 2

    def test_lights_action(self):
        cmds = kb.translate_action({"type": "lights", "base": 0, "head": 128}, {})
        assert len(cmds) == 1
        assert cmds[0]["IO5"] == 128

    def test_light_alias(self):
        cmds = kb.translate_action({"type": "light", "base": 50, "head": 200}, {})
        assert len(cmds) == 1

    def test_unknown_action_returns_empty(self):
        cmds = kb.translate_action({"type": "dance"}, {})
        assert cmds == []

    def test_non_dict_action_returns_empty(self):
        cmds = kb.translate_action("drive forward", {})
        assert cmds == []


# ===========================================================================
# Journal Writer Tests
# ===========================================================================

class TestJournalWriter:
    def test_writes_jsonl_file(self, tmp_dir, sample_decision):
        state = {"pan_position": 0, "tilt_position": 0}
        kb.write_journal_entry("42", "sess_abc", sample_decision, "ok", state)

        today = datetime.now().strftime("%Y-%m-%d")
        journal_file = kb.JOURNAL_DIR / f"{today}.jsonl"
        assert journal_file.exists()

        lines = journal_file.read_text().strip().splitlines()
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["tick"] == 42
        assert entry["session_id"] == "sess_abc"
        assert entry["mood"] == "curious"
        assert entry["observation"] == "I see a bright hallway with a doorway at the end"

    def test_appends_multiple_entries(self, tmp_dir, sample_decision):
        state = {"pan_position": 0, "tilt_position": 0}
        kb.write_journal_entry("1", "sess", sample_decision, "ok", state)
        kb.write_journal_entry("2", "sess", sample_decision, "ok", state)

        today = datetime.now().strftime("%Y-%m-%d")
        journal_file = kb.JOURNAL_DIR / f"{today}.jsonl"
        lines = journal_file.read_text().strip().splitlines()
        assert len(lines) == 2


# ===========================================================================
# State Persistence Tests
# ===========================================================================

class TestState:
    def test_save_and_load(self, tmp_dir):
        state = kb.DEFAULT_STATE.copy()
        state["goal"] = "test goal"
        state["tick_count"] = 42
        state["session_id"] = "test_sess"
        kb.save_state(state)

        loaded = kb.load_state()
        assert loaded["goal"] == "test goal"
        assert loaded["tick_count"] == 42

    def test_load_defaults_on_missing(self, tmp_dir):
        # STATE_FILE doesn't exist
        state = kb.load_state()
        assert state["goal"] == "wake up and explore"
        assert state["tick_count"] == 0
        assert state["session_id"] is not None

    def test_load_migrates_missing_keys(self, tmp_dir):
        # Write a state file missing some keys
        kb.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        kb.STATE_FILE.write_text(json.dumps({"goal": "old goal"}))

        state = kb.load_state()
        assert state["goal"] == "old goal"
        assert state["tick_count"] == 0  # default filled in
        assert "mood" in state


# ===========================================================================
# Brain Response Parser Tests
# ===========================================================================

class TestBrainParser:
    def test_parse_clean_json(self):
        api_resp = {
            "content": [{
                "text": '{"observation":"test","goal":"test","mood":"curious","actions":[],"next_tick_ms":3000}'
            }]
        }
        result = kb.parse_brain_response(api_resp)
        assert result["observation"] == "test"
        assert result["mood"] == "curious"

    def test_parse_strips_markdown_fences(self):
        api_resp = {
            "content": [{
                "text": '```json\n{"observation":"test","goal":"test"}\n```'
            }]
        }
        result = kb.parse_brain_response(api_resp)
        assert result["observation"] == "test"

    def test_parse_invalid_json_raises(self):
        api_resp = {"content": [{"text": "this is not json"}]}
        with pytest.raises(json.JSONDecodeError):
            kb.parse_brain_response(api_resp)


# ===========================================================================
# Crash Recovery Tests
# ===========================================================================

class TestCrashRecovery:
    def test_recover_replays_missing_entries(self, db, tmp_dir):
        # Write a JSONL entry that's not in the DB
        kb.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        journal_file = kb.JOURNAL_DIR / "2025-01-01.jsonl"
        entry = {
            "tick": 99,
            "timestamp": "2025-01-01T12:00:00",
            "session_id": "old_sess",
            "observation": "recovered observation",
            "goal": "recovered goal",
            "thought": "recovered thought",
            "mood": "recovered",
            "actions": [],
            "tags": ["loc:recovered"],
            "outcome": "neutral",
        }
        journal_file.write_text(json.dumps(entry) + "\n")

        kb.recover_from_crash(db)

        row = db.execute("SELECT * FROM memories WHERE tick_id='99'").fetchone()
        assert row is not None
        assert row["observation"] == "recovered observation"
        assert row["compressed"] == 1  # recovered entries marked compressed

    def test_recover_skips_existing(self, db, tmp_dir, sample_decision):
        # Insert tick 1 into DB
        kb.insert_tick_memory(db, "1", "sess", sample_decision)

        # Also write tick 1 to JSONL
        kb.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        journal_file = kb.JOURNAL_DIR / "2025-01-01.jsonl"
        entry = {"tick": 1, "session_id": "sess", "tags": []}
        journal_file.write_text(json.dumps(entry) + "\n")

        kb.recover_from_crash(db)

        # Should still have only 1 entry for tick 1
        count = db.execute("SELECT COUNT(*) FROM memories WHERE tick_id='1'").fetchone()[0]
        assert count == 1


# ===========================================================================
# Clamp Utility Tests
# ===========================================================================

class TestClamp:
    def test_clamp_within_range(self):
        assert kb._clamp(5, 0, 10) == 5

    def test_clamp_below(self):
        assert kb._clamp(-5, 0, 10) == 0

    def test_clamp_above(self):
        assert kb._clamp(15, 0, 10) == 10

    def test_clamp_float(self):
        assert kb._clamp(1.5, -1.3, 1.3) == 1.3
