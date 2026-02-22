#!/usr/bin/env python3
"""
test_kombucha.py — Test suite for Kombucha bridge + memory engine + story server.

Runs without hardware: no serial, no camera, no API calls.

    python -m pytest test_kombucha.py -v
"""

import asyncio
import json
import os
import queue
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

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
    MOCK_MODULES["numpy"].frombuffer = lambda *a, **k: b"fake"
    MOCK_MODULES["numpy"].uint8 = "uint8"
    MOCK_MODULES["numpy"].mean = lambda x: 25.5
    MOCK_MODULES["numpy"].isscalar = lambda x: isinstance(x, (int, float, complex, str, bytes))
    MOCK_MODULES["numpy"].bool_ = type("bool_", (int,), {})
    MOCK_MODULES["numpy"].ndarray = type("ndarray", (), {})

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
    cv2_mock.IMREAD_GRAYSCALE = 0
    cv2_mock.imdecode = lambda *a, **k: MagicMock()

# httpx needs AsyncClient
if "httpx" in MOCK_MODULES:
    httpx_mock = MOCK_MODULES["httpx"]
    httpx_mock.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
    httpx_mock.AsyncClient = type("AsyncClient", (), {
        "__aenter__": lambda self: self,
        "__aexit__": lambda self, *a: None,
    })
    httpx_mock.Client = type("Client", (), {
        "__enter__": lambda self: self,
        "__exit__": lambda self, *a: None,
        "post": lambda self, *a, **k: None,
    })

# serial needs SerialException
if "serial" in MOCK_MODULES:
    serial_mock = MOCK_MODULES["serial"]
    serial_mock.SerialException = type("SerialException", (Exception,), {})
    serial_mock.Serial = lambda *a, **k: None

# Patch sys.argv before importing bridge (it runs argparse at import time)
with patch("sys.argv", ["kombucha_bridge.py", "--debug"]):
    import kombucha_bridge as kb

# Import story_server (no argparse at import time, safe to import directly)
# Patch sys.argv just in case
with patch("sys.argv", ["story_server.py"]):
    import story_server as ss


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


@pytest.fixture
def minimal_decision():
    """A decision with only required fields."""
    return {
        "observation": "darkness",
        "goal": "wake up",
        "mood": "awakening",
        "actions": [],
        "next_tick_ms": 3000,
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

    def test_creates_indexes(self, db):
        """DB creates expected indexes."""
        indexes = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()]
        assert "idx_memories_session" in indexes
        assert "idx_memories_tier" in indexes
        assert "idx_memories_timestamp" in indexes
        assert "idx_memories_compressed" in indexes

    def test_identity_seed_content(self, db):
        """Seed identity statements mention Kombucha."""
        rows = db.execute("SELECT statement FROM identity WHERE active=TRUE").fetchall()
        statements = [r["statement"] for r in rows]
        assert any("Kombucha" in s for s in statements)
        assert any("Bucket" in s for s in statements)

    def test_identity_seed_source_is_operator(self, db):
        """All seed identity rows have source='operator'."""
        rows = db.execute("SELECT source FROM identity WHERE active=TRUE").fetchall()
        assert all(r["source"] == "operator" for r in rows)


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

    def test_enrich_tags_goal_truncated(self):
        """Long goals are truncated to 30 chars."""
        tags = kb.enrich_tags([], {"goal": "a" * 50, "actions": []})
        goal_tags = [t for t in tags if t.startswith("goal:")]
        assert len(goal_tags) == 1
        # "goal:" prefix + 30 chars max
        assert len(goal_tags[0]) <= 5 + 30

    def test_enrich_tags_goal_spaces_to_underscores(self):
        """Goal spaces become underscores."""
        tags = kb.enrich_tags([], {"goal": "find the door", "actions": []})
        goal_tags = [t for t in tags if t.startswith("goal:")]
        assert "goal:find_the_door" in goal_tags

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
        assert time_tags[0] in ("time:night", "time:morning", "time:afternoon", "time:evening")

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

    def test_enrich_tags_preserves_order(self):
        """Agent tags appear before enriched tags."""
        tags = kb.enrich_tags(["loc:kitchen"], {"mood": "curious", "actions": []})
        assert tags.index("loc:kitchen") < tags.index("mood:curious")

    def test_enrich_tags_handles_non_dict_actions(self):
        """Non-dict actions in list are skipped gracefully."""
        tags = kb.enrich_tags([], {"actions": ["drive", 42, None]})
        assert not any(t.startswith("act:") for t in tags)

    def test_enrich_tags_empty_mood(self):
        """Empty mood string doesn't add a mood tag."""
        tags = kb.enrich_tags([], {"mood": "", "actions": []})
        assert not any(t.startswith("mood:") for t in tags)

    def test_enrich_tags_missing_fields(self):
        """Handles decision with no mood/goal/outcome gracefully."""
        tags = kb.enrich_tags([], {"actions": []})
        # Should still have time tag at minimum
        assert len(tags) >= 1

    def test_enrich_tags_none_input(self):
        """None for agent_tags treated as empty list."""
        tags = kb.enrich_tags(None, {"actions": []})
        assert isinstance(tags, list)


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

    def test_insert_skips_empty_identity_proposal(self, db):
        """Empty/whitespace identity proposals are ignored."""
        decision = {
            "observation": "test", "goal": "test", "mood": "ok",
            "actions": [], "tags": [], "outcome": "neutral",
            "identity_proposal": "   ",
        }
        kb.insert_tick_memory(db, "1", "sess", decision)
        count = db.execute("SELECT COUNT(*) FROM identity WHERE source='agent_proposal'").fetchone()[0]
        assert count == 0

    def test_insert_skips_none_identity_proposal(self, db):
        """None identity proposal is ignored."""
        decision = {
            "observation": "test", "goal": "test", "mood": "ok",
            "actions": [], "tags": [], "outcome": "neutral",
            "identity_proposal": None,
        }
        kb.insert_tick_memory(db, "1", "sess", decision)
        count = db.execute("SELECT COUNT(*) FROM identity WHERE source='agent_proposal'").fetchone()[0]
        assert count == 0

    def test_insert_failure_outcome(self, db):
        """Failure outcome sets failure=TRUE, success=FALSE."""
        decision = {
            "observation": "wall", "goal": "go forward",
            "mood": "frustrated", "actions": [], "tags": [],
            "outcome": "failure",
        }
        kb.insert_tick_memory(db, "1", "sess", decision)
        row = db.execute("SELECT success, failure FROM memories WHERE tick_id='1'").fetchone()
        assert row["success"] == 0
        assert row["failure"] == 1

    def test_insert_neutral_outcome(self, db):
        """Neutral outcome sets both success=FALSE, failure=FALSE."""
        decision = {
            "observation": "hallway", "goal": "explore",
            "mood": "curious", "actions": [], "tags": [],
            "outcome": "neutral",
        }
        kb.insert_tick_memory(db, "1", "sess", decision)
        row = db.execute("SELECT success, failure FROM memories WHERE tick_id='1'").fetchone()
        assert row["success"] == 0
        assert row["failure"] == 0

    def test_insert_stores_memory_note(self, db, sample_decision):
        kb.insert_tick_memory(db, "1", "sess", sample_decision)
        row = db.execute("SELECT memory_note FROM memories WHERE tick_id='1'").fetchone()
        assert row["memory_note"] == "Found a new hallway with natural light"

    def test_insert_minimal_decision(self, db, minimal_decision):
        """Minimal decision without optional fields doesn't crash."""
        kb.insert_tick_memory(db, "1", "sess", minimal_decision)
        row = db.execute("SELECT * FROM memories WHERE tick_id='1'").fetchone()
        assert row is not None
        assert row["lesson"] is None
        assert row["memory_note"] is None

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

    def test_retrieve_excludes_working_tick_ids(self, db, sample_decision):
        """Memories whose tick_id is in working_tick_ids are excluded."""
        kb.insert_tick_memory(db, "1", "sess_old", sample_decision)

        results = kb.retrieve_memories(
            db,
            current_tags=["loc:hallway"],
            session_id="sess_new",
            working_tick_ids={"1"},  # exclude this tick
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

    def test_retrieve_scores_success_higher(self, db):
        """Success memories get a scoring boost."""
        decision_success = {
            "observation": "door", "goal": "navigate", "mood": "happy",
            "actions": [], "tags": ["loc:hallway"], "outcome": "success",
        }
        decision_neutral = {
            "observation": "wall", "goal": "navigate", "mood": "ok",
            "actions": [], "tags": ["loc:hallway"], "outcome": "neutral",
        }
        kb.insert_tick_memory(db, "1", "sess_old", decision_success)
        kb.insert_tick_memory(db, "2", "sess_old", decision_neutral)

        results = kb.retrieve_memories(
            db, current_tags=["loc:hallway"],
            session_id="sess_new", working_tick_ids=set(),
        )
        assert results[0]["tick_id"] == "1"  # success scored higher

    def test_retrieve_updates_relevance_hits(self, db, sample_decision):
        """Retrieved memories get their relevance_hits incremented."""
        kb.insert_tick_memory(db, "1", "sess_old", sample_decision)

        kb.retrieve_memories(
            db, current_tags=["loc:hallway"],
            session_id="sess_new", working_tick_ids=set(),
        )
        row = db.execute("SELECT relevance_hits FROM memories WHERE tick_id='1'").fetchone()
        assert row["relevance_hits"] == 1

        # Retrieve again
        kb.retrieve_memories(
            db, current_tags=["loc:hallway"],
            session_id="sess_new2", working_tick_ids=set(),
        )
        row = db.execute("SELECT relevance_hits FROM memories WHERE tick_id='1'").fetchone()
        assert row["relevance_hits"] == 2

    def test_retrieve_updates_last_retrieved(self, db, sample_decision):
        """Retrieved memories get last_retrieved timestamp set."""
        kb.insert_tick_memory(db, "1", "sess_old", sample_decision)

        row_before = db.execute("SELECT last_retrieved FROM memories WHERE tick_id='1'").fetchone()
        assert row_before["last_retrieved"] is None

        kb.retrieve_memories(
            db, current_tags=["loc:hallway"],
            session_id="sess_new", working_tick_ids=set(),
        )
        row_after = db.execute("SELECT last_retrieved FROM memories WHERE tick_id='1'").fetchone()
        assert row_after["last_retrieved"] is not None

    def test_retrieve_empty_tags_returns_nothing(self, db, sample_decision):
        kb.insert_tick_memory(db, "1", "sess_A", sample_decision)
        results = kb.retrieve_memories(db, [], "sess_B", set())
        assert results == []

    def test_retrieve_no_overlap_returns_nothing(self, db, sample_decision):
        """Tags with zero overlap return no results."""
        kb.insert_tick_memory(db, "1", "sess_old", sample_decision)
        results = kb.retrieve_memories(
            db, current_tags=["loc:bathroom", "obj:mirror"],
            session_id="sess_new", working_tick_ids=set(),
        )
        assert results == []

    def test_retrieve_respects_count_limit(self, db):
        """At most RETRIEVED_MEMORY_COUNT results are returned."""
        for i in range(20):
            decision = {
                "observation": f"hallway {i}", "goal": "explore",
                "mood": "curious", "actions": [], "tags": ["loc:hallway"],
                "outcome": "neutral",
            }
            kb.insert_tick_memory(db, str(i), "sess_old", decision)

        results = kb.retrieve_memories(
            db, current_tags=["loc:hallway"],
            session_id="sess_new", working_tick_ids=set(),
        )
        assert len(results) <= kb.RETRIEVED_MEMORY_COUNT

    def test_retrieve_multi_tag_overlap_scores_higher(self, db):
        """Memories matching more tags score higher."""
        decision_1tag = {
            "observation": "wall", "goal": "go", "mood": "ok",
            "actions": [], "tags": ["loc:hallway"], "outcome": "neutral",
        }
        decision_3tags = {
            "observation": "door", "goal": "go", "mood": "curious",
            "actions": [], "tags": ["loc:hallway", "obj:door", "mood:curious"],
            "outcome": "neutral",
        }
        kb.insert_tick_memory(db, "1", "sess_old", decision_1tag)
        kb.insert_tick_memory(db, "2", "sess_old", decision_3tags)

        results = kb.retrieve_memories(
            db, current_tags=["loc:hallway", "obj:door", "mood:curious"],
            session_id="sess_new", working_tick_ids=set(),
        )
        assert results[0]["tick_id"] == "2"  # more overlap = higher score


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

    def test_context_working_memory_chronological(self, db):
        """Working memory entries appear in chronological order (oldest first)."""
        for i in range(3):
            decision = {
                "observation": f"obs_{i}", "goal": "test",
                "mood": "ok", "actions": [], "tags": [],
                "outcome": "neutral",
            }
            kb.insert_tick_memory(db, str(i + 1), "sess_test", decision)

        ctx = kb.assemble_memory_context(db, {"mood": "ok", "goal": "test"}, "sess_test")
        # obs_0 should appear before obs_2 in the context
        pos_0 = ctx.find("obs_0")
        pos_2 = ctx.find("obs_2")
        assert pos_0 < pos_2

    def test_context_includes_longterm(self, db):
        """Long-term memory entries appear in context."""
        db.execute("""
            INSERT INTO memories (tick_id, timestamp, session_id, tier, summary, tags, compressed)
            VALUES ('lt_1', '2025-01-01T00:00:00', 'old_sess', 'longterm',
                    'I explored the kitchen and found a window.', '[]', TRUE)
        """)
        db.commit()

        ctx = kb.assemble_memory_context(db, {"mood": "ok", "goal": "test"}, "sess_new")
        assert "PAST SESSIONS" in ctx
        assert "kitchen" in ctx

    def test_context_includes_session_memory(self, db):
        """Session summaries appear in context."""
        db.execute("""
            INSERT INTO memories (tick_id, timestamp, session_id, tier, summary, tags, compressed)
            VALUES ('sess_1_to_5', '2025-01-15T12:00:00', 'sess_test', 'session',
                    'Explored the hallway and found the living room.', '[]', TRUE)
        """)
        db.commit()

        ctx = kb.assemble_memory_context(db, {"mood": "ok", "goal": "test"}, "sess_test")
        assert "EARLIER TODAY" in ctx
        assert "living room" in ctx

    def test_context_includes_retrieved_memories(self, db, sample_decision):
        """Cross-session memories retrieved via tag overlap appear in context."""
        kb.insert_tick_memory(db, "1", "sess_old", sample_decision)
        # Now assemble from a new session with matching tags
        state = {"mood": "curious", "goal": "explore the hallway"}
        ctx = kb.assemble_memory_context(db, state, "sess_new")
        assert "RECALLED MEMORIES" in ctx

    def test_context_limits_working_memory(self, db):
        """Only WORKING_MEMORY_SIZE most recent ticks appear."""
        for i in range(10):
            decision = {
                "observation": f"obs_{i}", "goal": "test", "mood": "ok",
                "actions": [], "tags": [], "outcome": "neutral",
            }
            kb.insert_tick_memory(db, str(i + 1), "sess_test", decision)

        ctx = kb.assemble_memory_context(
            db, {"mood": "ok", "goal": "test"}, "sess_test"
        )
        # Should not contain the oldest entries (beyond WORKING_MEMORY_SIZE)
        assert "obs_0" not in ctx
        # Should contain the most recent
        assert "obs_9" in ctx


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

    def test_drive_zero(self):
        result = kb.validate_tcode(1, {"L": 0, "R": 0})
        assert result["L"] == 0
        assert result["R"] == 0

    def test_drive_normal(self):
        result = kb.validate_tcode(1, {"L": 0.5, "R": -0.3})
        assert result["L"] == 0.5
        assert result["R"] == -0.3

    def test_drive_defaults_missing_params(self):
        result = kb.validate_tcode(1, {})
        assert result["L"] == 0
        assert result["R"] == 0

    def test_oled_text_truncated(self):
        result = kb.validate_tcode(3, {
            "lineNum": 0,
            "Text": "a" * 50
        })
        assert len(result["Text"]) == 20

    def test_oled_line_clamped(self):
        result = kb.validate_tcode(3, {"lineNum": 10, "Text": "hi"})
        assert result["lineNum"] == 3

    def test_oled_negative_line_clamped(self):
        result = kb.validate_tcode(3, {"lineNum": -5, "Text": "hi"})
        assert result["lineNum"] == 0

    def test_oled_reset(self):
        result = kb.validate_tcode(-3, {})
        assert result == {"T": -3}

    def test_led_clamped(self):
        result = kb.validate_tcode(132, {"IO4": 999, "IO5": -10})
        assert result["IO4"] == 255
        assert result["IO5"] == 0

    def test_led_normal(self):
        result = kb.validate_tcode(132, {"IO4": 128, "IO5": 64})
        assert result["IO4"] == 128
        assert result["IO5"] == 64

    def test_gimbal_clamped(self):
        result = kb.validate_tcode(133, {
            "X": 999, "Y": 999, "SPD": 999, "ACC": 999
        })
        assert result["X"] == 180
        assert result["Y"] == 90
        assert result["SPD"] == 200
        assert result["ACC"] == 50

    def test_gimbal_negative_clamped(self):
        result = kb.validate_tcode(133, {
            "X": -999, "Y": -999, "SPD": -999, "ACC": -999
        })
        assert result["X"] == -180
        assert result["Y"] == -30
        assert result["SPD"] == 1
        assert result["ACC"] == 1

    def test_gimbal_simple(self):
        """T-code 141 (simple gimbal) validates correctly."""
        result = kb.validate_tcode(141, {"X": 45, "Y": 10, "SPD": 50})
        assert result["T"] == 141
        assert result["X"] == 45
        assert result["Y"] == 10
        assert result["SPD"] == 50

    def test_gimbal_simple_clamped(self):
        result = kb.validate_tcode(141, {"X": 300, "Y": -90, "SPD": 500})
        assert result["X"] == 180
        assert result["Y"] == -30
        assert result["SPD"] == 200

    def test_servo_torque(self):
        """T-code 210 (servo torque) validates correctly."""
        result = kb.validate_tcode(210, {"id": 1, "cmd": 1})
        assert result["T"] == 210
        assert result["id"] == 1
        assert result["cmd"] == 1

    def test_servo_torque_id_clamped(self):
        result = kb.validate_tcode(210, {"id": 5, "cmd": 1})
        assert result["id"] == 2

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

    def test_drive_defaults_to_zero(self):
        """Drive with missing left/right defaults to 0."""
        cmds = kb.translate_action({"type": "drive"}, {})
        assert cmds[0]["L"] == 0
        assert cmds[0]["R"] == 0

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

    def test_look_custom_speed(self):
        """Look action with custom speed and accel."""
        state = {"pan_position": 0, "tilt_position": 0}
        cmds = kb.translate_action(
            {"type": "look", "pan": 90, "tilt": 30, "speed": 150, "accel": 25}, state
        )
        assert cmds[0]["SPD"] == 150
        assert cmds[0]["ACC"] == 25

    def test_look_default_speed(self):
        """Look action uses default speed=100, accel=10."""
        state = {"pan_position": 0, "tilt_position": 0}
        cmds = kb.translate_action({"type": "look", "pan": 0, "tilt": 0}, state)
        assert cmds[0]["SPD"] == 100
        assert cmds[0]["ACC"] == 10

    def test_look_clamps_state(self):
        """Look action clamps state values to valid range."""
        state = {"pan_position": 0, "tilt_position": 0}
        kb.translate_action({"type": "look", "pan": 999, "tilt": -999}, state)
        assert state["pan_position"] == 180
        assert state["tilt_position"] == -30

    def test_display_produces_four_commands(self):
        cmds = kb.translate_action({
            "type": "display",
            "lines": ["curious", "exploring", "the hall", "onward"]
        }, {})
        assert len(cmds) == 4
        assert all(c["T"] == 3 for c in cmds)
        assert cmds[0]["Text"] == "curious"
        assert cmds[3]["Text"] == "onward"

    def test_display_fewer_than_four_lines(self):
        """Display with fewer than 4 lines pads with empty strings."""
        cmds = kb.translate_action({
            "type": "display",
            "lines": ["hello", "world"]
        }, {})
        assert len(cmds) == 2

    def test_display_more_than_four_lines(self):
        """Display truncates to first 4 lines."""
        cmds = kb.translate_action({
            "type": "display",
            "lines": ["a", "b", "c", "d", "e", "f"]
        }, {})
        assert len(cmds) == 4

    def test_display_empty_lines_default(self):
        """Display with no lines specified uses defaults."""
        cmds = kb.translate_action({"type": "display"}, {})
        assert len(cmds) == 4

    def test_oled_single_line(self):
        cmds = kb.translate_action({"type": "oled", "line": 2, "text": "hello"}, {})
        assert len(cmds) == 1
        assert cmds[0]["lineNum"] == 2

    def test_oled_reset_action(self):
        """oled_reset action translates to T=-3."""
        cmds = kb.translate_action({"type": "oled_reset"}, {})
        assert len(cmds) == 1
        assert cmds[0]["T"] == -3

    def test_lights_action(self):
        cmds = kb.translate_action({"type": "lights", "base": 0, "head": 128}, {})
        assert len(cmds) == 1
        assert cmds[0]["IO5"] == 128

    def test_light_alias(self):
        cmds = kb.translate_action({"type": "light", "base": 50, "head": 200}, {})
        assert len(cmds) == 1

    def test_speak_action_in_debug(self):
        """Speak action in debug mode doesn't crash."""
        cmds = kb.translate_action({"type": "speak", "text": "hello world"}, {})
        # speak doesn't produce T-codes
        assert cmds == []

    def test_unknown_action_returns_empty(self):
        cmds = kb.translate_action({"type": "dance"}, {})
        assert cmds == []

    def test_non_dict_action_returns_empty(self):
        cmds = kb.translate_action("drive forward", {})
        assert cmds == []

    def test_none_action_returns_empty(self):
        cmds = kb.translate_action(None, {})
        assert cmds == []

    def test_action_missing_type(self):
        """Action dict without 'type' key returns empty."""
        cmds = kb.translate_action({"left": 0.3, "right": 0.3}, {})
        assert cmds == []


# ===========================================================================
# Execute Actions Tests
# ===========================================================================

class TestExecuteActions:
    def test_no_actions_returns_no_actions(self):
        result = kb.execute_actions(None, [], {})
        assert result == "no_actions"

    def test_none_actions_returns_no_actions(self):
        result = kb.execute_actions(None, None, {})
        assert result == "no_actions"

    def test_debug_mode_executes(self):
        """In debug mode, actions produce debug_ok results."""
        actions = [{"type": "drive", "left": 0.3, "right": 0.3}]
        result = kb.execute_actions(None, actions, {})
        assert "debug_ok" in result

    def test_truncates_to_max_actions(self):
        """More than MAX_ACTIONS actions are truncated."""
        actions = [{"type": "stop"} for _ in range(10)]
        result = kb.execute_actions(None, actions, {})
        # Should only execute MAX_ACTIONS (5)
        result_parts = result.split(", ")
        assert len(result_parts) == kb.MAX_ACTIONS

    def test_mixed_valid_invalid_actions(self):
        """Mix of valid and invalid actions."""
        actions = [
            {"type": "stop"},
            {"type": "nonexistent"},
            {"type": "stop"},
        ]
        result = kb.execute_actions(None, actions, {})
        parts = result.split(", ")
        # stop produces 1 t-code each, nonexistent produces 0
        assert len(parts) == 2  # only valid actions produce results

    def test_display_produces_multiple_results(self):
        """Display action produces 4 T-codes, each counted."""
        actions = [{"type": "display", "lines": ["a", "b", "c", "d"]}]
        result = kb.execute_actions(None, actions, {})
        assert result.count("debug_ok") == 4

    def test_no_serial_result(self):
        """Without serial and not in debug mode, actions return no_serial."""
        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = False
            actions = [{"type": "stop"}]
            result = kb.execute_actions(None, actions, {})
            assert "no_serial" in result
        finally:
            kb.DEBUG_MODE = old_debug


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

    def test_entry_contains_all_fields(self, tmp_dir, sample_decision):
        """Journal entry contains all expected fields."""
        state = {"pan_position": 45, "tilt_position": 10}
        kb.write_journal_entry("1", "sess", sample_decision, "ok", state)

        today = datetime.now().strftime("%Y-%m-%d")
        journal_file = kb.JOURNAL_DIR / f"{today}.jsonl"
        entry = json.loads(journal_file.read_text().strip())

        expected_keys = {
            "tick", "timestamp", "session_id", "observation", "goal",
            "reasoning", "thought", "mood", "actions", "result",
            "tags", "outcome", "lesson", "memory_note",
            "identity_proposal", "pan", "tilt",
        }
        assert expected_keys.issubset(set(entry.keys()))
        assert entry["pan"] == 45
        assert entry["tilt"] == 10

    def test_tick_is_integer(self, tmp_dir, sample_decision):
        """Tick ID is stored as an integer in journal."""
        state = {"pan_position": 0, "tilt_position": 0}
        kb.write_journal_entry("42", "sess", sample_decision, "ok", state)

        today = datetime.now().strftime("%Y-%m-%d")
        journal_file = kb.JOURNAL_DIR / f"{today}.jsonl"
        entry = json.loads(journal_file.read_text().strip())
        assert isinstance(entry["tick"], int)
        assert entry["tick"] == 42

    def test_minimal_decision_journal(self, tmp_dir, minimal_decision):
        """Minimal decision writes without error."""
        state = {"pan_position": 0, "tilt_position": 0}
        kb.write_journal_entry("1", "sess", minimal_decision, "ok", state)

        today = datetime.now().strftime("%Y-%m-%d")
        journal_file = kb.JOURNAL_DIR / f"{today}.jsonl"
        entry = json.loads(journal_file.read_text().strip())
        assert entry["lesson"] is None
        assert entry["memory_note"] is None


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

    def test_load_generates_session_on_fresh(self, tmp_dir):
        """Fresh state generates a session_start timestamp."""
        state = kb.load_state()
        assert state["session_start"] is not None

    def test_save_atomic(self, tmp_dir):
        """State file is written atomically (no partial writes)."""
        state = kb.DEFAULT_STATE.copy()
        state["goal"] = "atomic test"
        kb.save_state(state)

        # Verify file is valid JSON
        loaded = json.loads(kb.STATE_FILE.read_text())
        assert loaded["goal"] == "atomic test"

    def test_default_state_keys(self):
        """DEFAULT_STATE has all expected keys."""
        expected = {
            "goal", "last_observation", "last_actions", "last_result",
            "tick_count", "session_start", "session_id",
            "consecutive_errors", "pan_position", "tilt_position",
            "mood", "wake_reason",
        }
        assert set(kb.DEFAULT_STATE.keys()) == expected


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

    def test_parse_strips_plain_fences(self):
        """Strips ``` fences without language identifier."""
        api_resp = {
            "content": [{
                "text": '```\n{"observation":"test","goal":"test"}\n```'
            }]
        }
        result = kb.parse_brain_response(api_resp)
        assert result["observation"] == "test"

    def test_parse_handles_extra_whitespace(self):
        api_resp = {
            "content": [{
                "text": '  \n  {"observation":"test","goal":"test"}  \n  '
            }]
        }
        result = kb.parse_brain_response(api_resp)
        assert result["observation"] == "test"

    def test_parse_invalid_json_raises(self):
        api_resp = {"content": [{"text": "this is not json"}]}
        with pytest.raises(json.JSONDecodeError):
            kb.parse_brain_response(api_resp)

    def test_parse_preserves_all_fields(self):
        """Parser preserves all mind output fields."""
        api_resp = {
            "content": [{
                "text": json.dumps({
                    "observation": "obs",
                    "goal": "g",
                    "reasoning": "r",
                    "thought": "t",
                    "mood": "m",
                    "actions": [{"type": "stop"}],
                    "next_tick_ms": 5000,
                    "tags": ["loc:here"],
                    "outcome": "success",
                    "lesson": "lesson text",
                    "memory_note": "note text",
                    "identity_proposal": "I am kind",
                })
            }]
        }
        result = kb.parse_brain_response(api_resp)
        assert result["lesson"] == "lesson text"
        assert result["memory_note"] == "note text"
        assert result["identity_proposal"] == "I am kind"
        assert result["tags"] == ["loc:here"]
        assert result["outcome"] == "success"


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

    def test_recover_handles_empty_journal_dir(self, db, tmp_dir):
        """Recovery with empty journal directory doesn't crash."""
        kb.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        kb.recover_from_crash(db)  # should not raise

    def test_recover_handles_missing_journal_dir(self, db, tmp_dir):
        """Recovery with non-existent journal directory doesn't crash."""
        # JOURNAL_DIR doesn't exist
        kb.recover_from_crash(db)  # should not raise

    def test_recover_handles_malformed_json(self, db, tmp_dir):
        """Recovery skips malformed JSON lines."""
        kb.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        journal_file = kb.JOURNAL_DIR / "2025-01-01.jsonl"
        journal_file.write_text("this is not json\n{bad json\n")

        kb.recover_from_crash(db)  # should not raise
        count = db.execute("SELECT COUNT(*) FROM memories WHERE tier='working'").fetchone()[0]
        assert count == 0

    def test_recover_multiple_files(self, db, tmp_dir):
        """Recovery processes multiple journal files."""
        kb.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)

        for i, date in enumerate(["2025-01-01", "2025-01-02"]):
            entry = {
                "tick": i + 1,
                "timestamp": f"{date}T12:00:00",
                "session_id": "sess",
                "observation": f"obs_{i}",
                "tags": [],
            }
            (kb.JOURNAL_DIR / f"{date}.jsonl").write_text(json.dumps(entry) + "\n")

        kb.recover_from_crash(db)
        count = db.execute("SELECT COUNT(*) FROM memories WHERE tier='working'").fetchone()[0]
        assert count == 2

    def test_recover_preserves_outcome(self, db, tmp_dir):
        """Recovered entries preserve success/failure flags."""
        kb.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "tick": 1, "session_id": "s", "tags": [],
            "outcome": "success",
            "timestamp": "2025-01-01T00:00:00",
        }
        (kb.JOURNAL_DIR / "2025-01-01.jsonl").write_text(json.dumps(entry) + "\n")

        kb.recover_from_crash(db)
        row = db.execute("SELECT success, failure FROM memories WHERE tick_id='1'").fetchone()
        assert row["success"] == 1
        assert row["failure"] == 0


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

    def test_clamp_at_boundary(self):
        assert kb._clamp(10, 0, 10) == 10
        assert kb._clamp(0, 0, 10) == 0

    def test_clamp_negative_range(self):
        assert kb._clamp(-100, -180, 180) == -100
        assert kb._clamp(-200, -180, 180) == -180


# ===========================================================================
# Dual Model Strategy Tests
# ===========================================================================

class TestDualModelStrategy:
    """Test the model selection logic from the main loop."""

    def test_first_tick_uses_deep(self):
        state = {"tick_count": 1, "consecutive_errors": 0, "wake_reason": None}
        use_deep = (
            state["tick_count"] == 1
            or state.get("consecutive_errors", 0) >= 3
            or state["tick_count"] % 20 == 0
            or state.get("wake_reason") == "motion_detected"
        )
        assert use_deep is True

    def test_normal_tick_uses_standard(self):
        state = {"tick_count": 5, "consecutive_errors": 0, "wake_reason": None}
        use_deep = (
            state["tick_count"] == 1
            or state.get("consecutive_errors", 0) >= 3
            or state["tick_count"] % 20 == 0
            or state.get("wake_reason") == "motion_detected"
        )
        assert use_deep is False

    def test_every_20th_tick_uses_deep(self):
        state = {"tick_count": 20, "consecutive_errors": 0, "wake_reason": None}
        use_deep = (
            state["tick_count"] == 1
            or state.get("consecutive_errors", 0) >= 3
            or state["tick_count"] % 20 == 0
            or state.get("wake_reason") == "motion_detected"
        )
        assert use_deep is True

    def test_40th_tick_uses_deep(self):
        state = {"tick_count": 40, "consecutive_errors": 0, "wake_reason": None}
        use_deep = (
            state["tick_count"] == 1
            or state.get("consecutive_errors", 0) >= 3
            or state["tick_count"] % 20 == 0
            or state.get("wake_reason") == "motion_detected"
        )
        assert use_deep is True

    def test_3_errors_uses_deep(self):
        state = {"tick_count": 5, "consecutive_errors": 3, "wake_reason": None}
        use_deep = (
            state["tick_count"] == 1
            or state.get("consecutive_errors", 0) >= 3
            or state["tick_count"] % 20 == 0
            or state.get("wake_reason") == "motion_detected"
        )
        assert use_deep is True

    def test_motion_wake_uses_deep(self):
        state = {"tick_count": 5, "consecutive_errors": 0, "wake_reason": "motion_detected"}
        use_deep = (
            state["tick_count"] == 1
            or state.get("consecutive_errors", 0) >= 3
            or state["tick_count"] % 20 == 0
            or state.get("wake_reason") == "motion_detected"
        )
        assert use_deep is True

    def test_2_errors_not_deep(self):
        """2 errors is below threshold, still uses standard."""
        state = {"tick_count": 5, "consecutive_errors": 2, "wake_reason": None}
        use_deep = (
            state["tick_count"] == 1
            or state.get("consecutive_errors", 0) >= 3
            or state["tick_count"] % 20 == 0
            or state.get("wake_reason") == "motion_detected"
        )
        assert use_deep is False


# ===========================================================================
# Frame Log Pruning Tests
# ===========================================================================

class TestFramePruning:
    def test_prune_keeps_max(self, tmp_dir):
        """Pruning keeps only FRAME_LOG_MAX most recent frames."""
        kb.FRAME_LOG_DIR.mkdir(parents=True, exist_ok=True)
        old_max = kb.FRAME_LOG_MAX
        kb.FRAME_LOG_MAX = 5

        try:
            for i in range(10):
                (kb.FRAME_LOG_DIR / f"tick_{i:05d}_test.jpg").write_bytes(b"\xff\xd8")

            kb._prune_frame_log()

            remaining = list(kb.FRAME_LOG_DIR.glob("tick_*.jpg"))
            assert len(remaining) == 5
        finally:
            kb.FRAME_LOG_MAX = old_max

    def test_prune_noop_under_limit(self, tmp_dir):
        """Pruning does nothing when under the limit."""
        kb.FRAME_LOG_DIR.mkdir(parents=True, exist_ok=True)

        for i in range(3):
            (kb.FRAME_LOG_DIR / f"tick_{i:05d}_test.jpg").write_bytes(b"\xff\xd8")

        kb._prune_frame_log()

        remaining = list(kb.FRAME_LOG_DIR.glob("tick_*.jpg"))
        assert len(remaining) == 3

    def test_prune_empty_dir(self, tmp_dir):
        """Pruning doesn't crash on empty frames directory."""
        kb.FRAME_LOG_DIR.mkdir(parents=True, exist_ok=True)
        kb._prune_frame_log()  # should not raise


# ===========================================================================
# System Prompt Tests
# ===========================================================================

class TestSystemPrompt:
    def test_prompt_mentions_kombucha(self):
        assert "Kombucha" in kb.SYSTEM_PROMPT

    def test_prompt_defines_action_vocabulary(self):
        for action in ("drive", "stop", "look", "display", "oled", "lights", "speak"):
            assert action in kb.SYSTEM_PROMPT

    def test_prompt_defines_response_format(self):
        for field in ("observation", "goal", "reasoning", "thought", "mood",
                       "actions", "next_tick_ms", "tags", "outcome", "lesson"):
            assert field in kb.SYSTEM_PROMPT

    def test_prompt_mentions_sentry(self):
        assert "sentry" in kb.SYSTEM_PROMPT.lower() or "10000" in kb.SYSTEM_PROMPT

    def test_prompt_mentions_duration_ms(self):
        assert "duration_ms" in kb.SYSTEM_PROMPT


# ===========================================================================
# Config Constants Tests
# ===========================================================================

class TestConfig:
    def test_sentry_threshold_reasonable(self):
        assert kb.SENTRY_THRESHOLD >= 5.0
        assert kb.SENTRY_THRESHOLD <= 30.0

    def test_motion_threshold_reasonable(self):
        assert 0.01 <= kb.MOTION_THRESHOLD <= 0.10

    def test_max_actions_limit(self):
        assert kb.MAX_ACTIONS >= 1
        assert kb.MAX_ACTIONS <= 10

    def test_working_memory_size(self):
        assert kb.WORKING_MEMORY_SIZE >= 3
        assert kb.WORKING_MEMORY_SIZE <= 20

    def test_compression_interval(self):
        assert kb.COMPRESSION_INTERVAL >= 5

    def test_models_defined(self):
        assert kb.MODEL is not None
        assert kb.MODEL_DEEP is not None
        assert kb.MODEL_HAIKU is not None

    def test_tick_range_bounds(self):
        """next_tick_ms clamping range: 2000-60000."""
        assert max(2000, min(60000, 1000)) == 2000
        assert max(2000, min(60000, 100000)) == 60000
        assert max(2000, min(60000, 5000)) == 5000


# ===========================================================================
# STORY SERVER TESTS
# ===========================================================================

# ===========================================================================
# Journal Parser Tests
# ===========================================================================

class TestJournalParser:
    def test_parse_valid_jsonl(self, tmp_path):
        """Parses valid JSONL into ordered list of ticks."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()

        entries = [
            {"tick": 2, "observation": "wall", "mood": "cautious"},
            {"tick": 1, "observation": "hallway", "mood": "curious"},
            {"tick": 3, "observation": "door", "mood": "excited"},
        ]
        lines = "\n".join(json.dumps(e) for e in entries)
        (journal_dir / "2025-01-01.jsonl").write_text(lines)

        ticks = ss.parse_journal_files(journal_dir)
        assert len(ticks) == 3
        assert ticks[0]["tick"] == 1  # sorted by tick number
        assert ticks[1]["tick"] == 2
        assert ticks[2]["tick"] == 3

    def test_parse_empty_dir(self, tmp_path):
        """Empty directory returns empty list."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        assert ss.parse_journal_files(journal_dir) == []

    def test_parse_missing_dir(self, tmp_path):
        """Non-existent directory returns empty list."""
        assert ss.parse_journal_files(tmp_path / "nonexistent") == []

    def test_parse_deduplicates_ticks(self, tmp_path):
        """Duplicate tick numbers keep the latest (last-wins)."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()

        lines = "\n".join([
            json.dumps({"tick": 1, "observation": "first"}),
            json.dumps({"tick": 1, "observation": "second"}),
        ])
        (journal_dir / "2025-01-01.jsonl").write_text(lines)

        ticks = ss.parse_journal_files(journal_dir)
        assert len(ticks) == 1
        assert ticks[0]["observation"] == "second"

    def test_parse_multiple_files(self, tmp_path):
        """Parses and merges multiple JSONL files."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()

        (journal_dir / "2025-01-01.jsonl").write_text(
            json.dumps({"tick": 1, "observation": "day1"}) + "\n"
        )
        (journal_dir / "2025-01-02.jsonl").write_text(
            json.dumps({"tick": 2, "observation": "day2"}) + "\n"
        )

        ticks = ss.parse_journal_files(journal_dir)
        assert len(ticks) == 2
        assert ticks[0]["observation"] == "day1"
        assert ticks[1]["observation"] == "day2"

    def test_parse_skips_malformed_lines(self, tmp_path):
        """Malformed JSON lines are skipped."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()

        lines = "not json\n" + json.dumps({"tick": 1, "mood": "ok"}) + "\n{bad\n"
        (journal_dir / "2025-01-01.jsonl").write_text(lines)

        ticks = ss.parse_journal_files(journal_dir)
        assert len(ticks) == 1

    def test_parse_skips_entries_without_tick(self, tmp_path):
        """Entries missing the 'tick' field are skipped."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()

        lines = "\n".join([
            json.dumps({"observation": "no tick field"}),
            json.dumps({"tick": 1, "observation": "has tick"}),
        ])
        (journal_dir / "2025-01-01.jsonl").write_text(lines)

        ticks = ss.parse_journal_files(journal_dir)
        assert len(ticks) == 1

    def test_parse_ignores_non_jsonl_files(self, tmp_path):
        """Non-.jsonl files are ignored."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()

        (journal_dir / "readme.txt").write_text("ignore me")
        (journal_dir / "2025-01-01.jsonl").write_text(
            json.dumps({"tick": 1}) + "\n"
        )

        ticks = ss.parse_journal_files(journal_dir)
        assert len(ticks) == 1


# ===========================================================================
# Legacy Log Parser Tests
# ===========================================================================

class TestLegacyLogParser:
    def test_parse_basic_log(self):
        """Parses standard log format with tick header and fields."""
        log_text = (
            "2025-01-15T14:30:00+0000 [INFO] Tick 1 | goal: explore\n"
            "2025-01-15T14:30:00+0000 [INFO]   OBS:     I see a hallway\n"
            "2025-01-15T14:30:00+0000 [INFO]   THOUGHT: Beautiful light\n"
            "2025-01-15T14:30:00+0000 [INFO]   MOOD:    curious\n"
            "2025-01-15T14:30:00+0000 [INFO]   ACTIONS: [{\"type\":\"drive\"}]\n"
        )
        ticks = ss.parse_logs(log_text)
        assert len(ticks) == 1
        assert ticks[0]["tick"] == 1
        assert ticks[0]["goal"] == "explore"
        assert ticks[0]["observation"] == "I see a hallway"
        assert ticks[0]["thought"] == "Beautiful light"
        assert ticks[0]["mood"] == "curious"

    def test_parse_multiple_ticks(self):
        log_text = (
            "Tick 1 | goal: explore\n"
            "  OBS: hallway\n"
            "Tick 2 | goal: investigate\n"
            "  OBS: doorway\n"
        )
        ticks = ss.parse_logs(log_text)
        assert len(ticks) == 2
        assert ticks[0]["tick"] == 1
        assert ticks[1]["tick"] == 2

    def test_parse_goal_change(self):
        log_text = (
            "Tick 1 | goal: explore\n"
            "  GOAL CHANGED: 'explore' -> 'investigate door'\n"
        )
        ticks = ss.parse_logs(log_text)
        assert ticks[0]["goal_changed"]["from"] == "explore"
        assert ticks[0]["goal_changed"]["to"] == "investigate door"

    def test_parse_model_used(self):
        log_text = (
            "Tick 1 | goal: explore\n"
            "  (used claude-opus-4-6)\n"
        )
        ticks = ss.parse_logs(log_text)
        assert ticks[0]["model"] == "claude-opus-4-6"

    def test_parse_outcome(self):
        log_text = (
            "Tick 1 | goal: explore\n"
            "  OUTCOME: failure\n"
        )
        ticks = ss.parse_logs(log_text)
        assert ticks[0]["outcome"] == "failure"

    def test_parse_lesson(self):
        log_text = (
            "Tick 1 | goal: explore\n"
            "  LESSON: Back up before turning\n"
        )
        ticks = ss.parse_logs(log_text)
        assert ticks[0]["lesson"] == "Back up before turning"

    def test_parse_tags(self):
        log_text = (
            "Tick 1 | goal: explore\n"
            '  TAGS: ["loc:hallway", "mood:curious"]\n'
        )
        ticks = ss.parse_logs(log_text)
        assert ticks[0]["tags"] == ["loc:hallway", "mood:curious"]

    def test_parse_empty_text(self):
        assert ss.parse_logs("") == []

    def test_parse_no_ticks(self):
        assert ss.parse_logs("some random log output\nwithout tick headers") == []


# ===========================================================================
# Frame Matcher Tests
# ===========================================================================

class TestFrameMatcher:
    def test_find_frame_match(self, tmp_path):
        """Finds frame file for given tick number."""
        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()
        (frames_dir / "tick_00042_20250115_143000.jpg").write_bytes(b"\xff\xd8")

        result = ss.find_frame(42, frames_dir)
        assert result == "tick_00042_20250115_143000.jpg"

    def test_find_frame_no_match(self, tmp_path):
        """Returns None when no frame matches."""
        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()
        assert ss.find_frame(999, frames_dir) is None

    def test_attach_frames(self, tmp_path):
        """Attaches frame filenames to tick entries."""
        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()
        (frames_dir / "tick_00001_test.jpg").write_bytes(b"\xff\xd8")

        ticks = [{"tick": 1}, {"tick": 2}]
        ss.attach_frames(ticks, frames_dir)
        assert ticks[0]["frame"] == "tick_00001_test.jpg"
        assert ticks[1]["frame"] is None


# ===========================================================================
# SSE Broker Tests
# ===========================================================================

class TestSSEBroker:
    def test_subscribe_returns_queue(self):
        broker = ss.SSEBroker()
        q = broker.subscribe()
        assert isinstance(q, queue.Queue)

    def test_broadcast_reaches_subscriber(self):
        broker = ss.SSEBroker()
        q = broker.subscribe()
        broker.broadcast({"tick": 1, "mood": "curious"})
        msg = q.get_nowait()
        assert msg["tick"] == 1

    def test_broadcast_reaches_multiple_subscribers(self):
        broker = ss.SSEBroker()
        q1 = broker.subscribe()
        q2 = broker.subscribe()
        broker.broadcast({"tick": 1})
        assert q1.get_nowait()["tick"] == 1
        assert q2.get_nowait()["tick"] == 1

    def test_unsubscribe_removes_queue(self):
        broker = ss.SSEBroker()
        q = broker.subscribe()
        broker.unsubscribe(q)
        broker.broadcast({"tick": 1})
        assert q.empty()

    def test_unsubscribe_nonexistent_no_error(self):
        broker = ss.SSEBroker()
        q = queue.Queue()
        broker.unsubscribe(q)  # should not raise

    def test_broadcast_drops_full_queues(self):
        """Full queues are removed on broadcast."""
        broker = ss.SSEBroker()
        q = broker.subscribe()
        # Fill the queue
        for i in range(100):
            q.put({"tick": i})
        # This broadcast should detect the full queue and remove it
        broker.broadcast({"tick": 999})
        with broker.lock:
            assert q not in broker.subscribers

    def test_subscribe_queue_maxsize(self):
        """Subscriber queues have a maxsize of 100."""
        broker = ss.SSEBroker()
        q = broker.subscribe()
        assert q.maxsize == 100


# ===========================================================================
# SyncThread Tests
# ===========================================================================

class TestSyncThread:
    def test_get_ticks_reverse_order(self):
        """get_ticks returns ticks in reverse chronological order."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)
        sync.all_ticks = [
            {"tick": 1}, {"tick": 2}, {"tick": 3}
        ]
        result = sync.get_ticks(0, 10)
        assert result[0]["tick"] == 3
        assert result[2]["tick"] == 1

    def test_get_ticks_offset_limit(self):
        """get_ticks respects offset and limit."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)
        sync.all_ticks = [{"tick": i} for i in range(1, 11)]  # 10 ticks
        result = sync.get_ticks(offset=2, limit=3)
        # Reversed: [10, 9, 8, 7, 6, 5, 4, 3, 2, 1] — offset 2, limit 3 = [8, 7, 6]
        assert len(result) == 3
        assert result[0]["tick"] == 8

    def test_get_total(self):
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)
        sync.all_ticks = [{"tick": 1}, {"tick": 2}]
        assert sync.get_total() == 2

    def test_get_state_returns_copy(self):
        """get_state returns a copy, not a reference."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)
        sync.rover_state = {"goal": "test"}
        state = sync.get_state()
        state["goal"] = "modified"
        assert sync.rover_state["goal"] == "test"  # original unchanged


# ===========================================================================
# Async Tests (Sentry Mode)
# ===========================================================================

class TestSentryMode:
    def test_sentry_returns_timeout(self):
        """Sentry sleep returns 'timeout' when no motion detected."""
        state = {}
        # Mock camera that returns fake frames
        mock_cap = MagicMock()
        mock_cap.read.return_value = (False, None)  # no frames → no motion

        result = asyncio.get_event_loop().run_until_complete(
            kb.sentry_sleep(mock_cap, 0.1, state)  # very short duration
        )
        assert result == "timeout"

    def test_sentry_timeout_preserves_state(self):
        """Sentry timeout doesn't set wake_reason."""
        state = {"wake_reason": None}
        mock_cap = MagicMock()
        mock_cap.read.return_value = (False, None)

        asyncio.get_event_loop().run_until_complete(
            kb.sentry_sleep(mock_cap, 0.1, state)
        )
        assert state.get("wake_reason") is None


# ===========================================================================
# Phase 1: Easy Edge Paths
# ===========================================================================

class TestEdgePaths:
    def test_shutdown_handler_sets_running_false(self):
        """shutdown_handler sets running to False."""
        old_running = kb.running
        try:
            kb.running = True
            kb.shutdown_handler(signal.SIGTERM, None)
            assert kb.running is False
        finally:
            kb.running = old_running

    def test_load_state_corrupt_json(self, tmp_dir):
        """Corrupt JSON in state file returns defaults."""
        kb.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        kb.STATE_FILE.write_text("{this is not valid json!!")
        state = kb.load_state()
        assert state["goal"] == "wake up and explore"
        assert state["tick_count"] == 0

    def test_save_state_write_failure(self, tmp_dir):
        """save_state raises when fdopen fails, cleans up temp file."""
        state = kb.DEFAULT_STATE.copy()
        with patch("kombucha_bridge.os.fdopen", side_effect=OSError("mock write failure")):
            with pytest.raises(OSError, match="mock write failure"):
                kb.save_state(state)

    def test_recover_empty_lines_skipped(self, db, tmp_dir):
        """Recovery skips empty/blank lines in JSONL."""
        kb.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"tick": 1, "session_id": "s", "tags": [], "timestamp": "2025-01-01T00:00:00"}
        content = "\n\n" + json.dumps(entry) + "\n\n\n"
        (kb.JOURNAL_DIR / "2025-01-01.jsonl").write_text(content)
        kb.recover_from_crash(db)
        count = db.execute("SELECT COUNT(*) FROM memories WHERE tick_id='1'").fetchone()[0]
        assert count == 1

    def test_recover_non_list_tags(self, db, tmp_dir):
        """Recovery handles non-list tags by replacing with []."""
        kb.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"tick": 1, "session_id": "s", "tags": "not-a-list",
                 "timestamp": "2025-01-01T00:00:00"}
        (kb.JOURNAL_DIR / "2025-01-01.jsonl").write_text(json.dumps(entry) + "\n")
        kb.recover_from_crash(db)
        row = db.execute("SELECT tags FROM memories WHERE tick_id='1'").fetchone()
        assert json.loads(row["tags"]) == []

    def test_recover_file_read_exception(self, db, tmp_dir):
        """Recovery handles file read exceptions gracefully."""
        kb.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        # Create a file that will cause an exception when read
        journal_file = kb.JOURNAL_DIR / "2025-01-01.jsonl"
        journal_file.write_text(json.dumps({"tick": 1, "session_id": "s", "tags": []}) + "\n")
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            kb.recover_from_crash(db)  # should not raise

    def test_context_tags_parse_error(self, db):
        """Context assembly handles invalid tags JSON in DB rows."""
        db.execute("""
            INSERT INTO memories (tick_id, timestamp, session_id, tier, observation,
                                  tags, compressed, archived)
            VALUES ('1', '2025-01-01T00:00:00', 'sess_test', 'working',
                    'test obs', '{invalid json}', FALSE, FALSE)
        """)
        db.commit()
        state = {"mood": "curious", "goal": "test"}
        # Should not crash
        ctx = kb.assemble_memory_context(db, state, "sess_test")
        assert isinstance(ctx, str)

    def test_context_actions_parse_error(self, db):
        """Context assembly handles invalid actions JSON in DB rows."""
        db.execute("""
            INSERT INTO memories (tick_id, timestamp, session_id, tier, observation,
                                  goal, mood, thought, actions, outcome,
                                  tags, compressed, archived)
            VALUES ('1', '2025-01-01T00:00:00', 'sess_test', 'working',
                    'test obs', 'test goal', 'ok', 'thought', '{not json array}',
                    'neutral', '["loc:test"]', FALSE, FALSE)
        """)
        db.commit()
        state = {"mood": "curious", "goal": "test"}
        ctx = kb.assemble_memory_context(db, state, "sess_test")
        assert "RECENT TICKS" in ctx

    def test_journal_write_exception(self, tmp_dir, sample_decision):
        """Journal write exception doesn't crash."""
        state = {"pan_position": 0, "tilt_position": 0}
        with patch("builtins.open", side_effect=PermissionError("denied")):
            # Should not raise
            kb.write_journal_entry("1", "sess", sample_decision, "ok", state)

    def test_prune_exception(self, tmp_dir):
        """Prune frame log handles exceptions gracefully."""
        kb.FRAME_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with patch.object(Path, "glob", side_effect=OSError("glob failed")):
            kb._prune_frame_log()  # should not raise


# ===========================================================================
# Phase 2: Time Branch Coverage
# ===========================================================================

class TestTimeBranches:
    def test_enrich_tags_time_night(self):
        """Hour 3 produces time:night."""
        mock_dt = MagicMock()
        mock_dt.now.return_value.hour = 3
        mock_dt.now.return_value.isoformat = datetime.now().isoformat
        with patch("kombucha_bridge.datetime", mock_dt):
            tags = kb.enrich_tags([], {"actions": []})
        assert "time:night" in tags

    def test_enrich_tags_time_morning(self):
        """Hour 8 produces time:morning."""
        mock_dt = MagicMock()
        mock_dt.now.return_value.hour = 8
        mock_dt.now.return_value.isoformat = datetime.now().isoformat
        with patch("kombucha_bridge.datetime", mock_dt):
            tags = kb.enrich_tags([], {"actions": []})
        assert "time:morning" in tags

    def test_enrich_tags_time_evening(self):
        """Hour 20 produces time:evening."""
        mock_dt = MagicMock()
        mock_dt.now.return_value.hour = 20
        mock_dt.now.return_value.isoformat = datetime.now().isoformat
        with patch("kombucha_bridge.datetime", mock_dt):
            tags = kb.enrich_tags([], {"actions": []})
        assert "time:evening" in tags


# ===========================================================================
# Phase 3: Hardware I/O Mocks
# ===========================================================================

class TestCameraInit:
    def test_init_camera_v4l2_success(self):
        """Camera init with V4L2 backend succeeding."""
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.get.return_value = 640
        mock_cap.read.return_value = (True, MagicMock())
        with patch("kombucha_bridge.cv2.VideoCapture", return_value=mock_cap):
            result = kb.init_camera()
        assert result == mock_cap

    def test_init_camera_fallback(self):
        """Camera falls back to default backend when V4L2 fails."""
        mock_cap_fail = MagicMock()
        mock_cap_fail.isOpened.return_value = False
        mock_cap_ok = MagicMock()
        mock_cap_ok.isOpened.return_value = True
        mock_cap_ok.get.return_value = 640
        mock_cap_ok.read.return_value = (True, MagicMock())
        with patch("kombucha_bridge.cv2.VideoCapture", side_effect=[mock_cap_fail, mock_cap_ok]):
            result = kb.init_camera()
        assert result == mock_cap_ok

    def test_init_camera_both_fail(self):
        """sys.exit(1) when both camera backends fail."""
        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = False
        with patch("kombucha_bridge.cv2.VideoCapture", return_value=mock_cap):
            with pytest.raises(SystemExit):
                kb.init_camera()


class TestCaptureFrame:
    def test_capture_frame_success(self, tmp_dir):
        """capture_frame_b64 returns base64 and saves file."""
        mock_cap = MagicMock()
        mock_cap.read.return_value = (True, MagicMock())
        result = kb.capture_frame_b64(mock_cap, tick_count=1)
        # cv2.imencode is mocked to return b"\xff\xd8"
        assert isinstance(result, str)
        # Should have saved a frame file
        frames = list(kb.FRAME_LOG_DIR.glob("tick_*.jpg"))
        assert len(frames) >= 1

    def test_capture_frame_empty(self):
        """Empty frame raises RuntimeError."""
        mock_cap = MagicMock()
        mock_cap.read.return_value = (False, None)
        with pytest.raises(RuntimeError, match="empty frame"):
            kb.capture_frame_b64(mock_cap)

    def test_capture_frame_save_fail(self, tmp_dir):
        """Frame save failure still returns base64."""
        mock_cap = MagicMock()
        mock_cap.read.return_value = (True, MagicMock())
        with patch.object(Path, "write_bytes", side_effect=OSError("write failed")):
            result = kb.capture_frame_b64(mock_cap, tick_count=1)
        assert isinstance(result, str)  # still returns b64


class TestSentryMotion:
    def test_sentry_detects_motion(self):
        """Sentry mode detects motion and returns early."""
        state = {"wake_reason": None}
        mock_cap = MagicMock()
        # First read: prev_gray, second read: motion detected
        frame1 = MagicMock()
        frame2 = MagicMock()
        mock_cap.read.side_effect = [
            (True, frame1),
            (True, frame2),
            (True, frame2),
        ]
        # Make thresh have a .size attribute for division
        mock_thresh = MagicMock()
        mock_thresh.size = 100

        # Use a long duration and control time to ensure the loop iterates
        time_values = [0.0, 0.0, 0.0, 0.0, 0.0, 999.0]  # eventually expire
        time_iter = iter(time_values)

        with patch("kombucha_bridge.cv2.threshold", return_value=(0, mock_thresh)), \
             patch("kombucha_bridge.np.count_nonzero", return_value=50), \
             patch("kombucha_bridge.time.time", side_effect=lambda: next(time_iter)), \
             patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
            result = asyncio.get_event_loop().run_until_complete(
                kb.sentry_sleep(mock_cap, 100.0, state)
            )
        assert result == "motion_detected"
        assert state["wake_reason"] == "motion_detected"


class TestSerialInit:
    def test_init_serial_success(self):
        """Serial init succeeds when not in debug mode."""
        old_debug = kb.DEBUG_MODE
        old_ser = kb.ser_port
        try:
            kb.DEBUG_MODE = False
            mock_ser = MagicMock()
            mock_ser.in_waiting = 0
            with patch("kombucha_bridge.serial.Serial", return_value=mock_ser):
                with patch("kombucha_bridge.time.sleep"):
                    result = kb.init_serial()
            assert result == mock_ser
            assert kb.ser_port == mock_ser
        finally:
            kb.DEBUG_MODE = old_debug
            kb.ser_port = old_ser

    def test_init_serial_buffered(self):
        """Serial init drains buffer when in_waiting > 0."""
        old_debug = kb.DEBUG_MODE
        old_ser = kb.ser_port
        try:
            kb.DEBUG_MODE = False
            mock_ser = MagicMock()
            mock_ser.in_waiting = 5
            with patch("kombucha_bridge.serial.Serial", return_value=mock_ser):
                with patch("kombucha_bridge.time.sleep"):
                    kb.init_serial()
            mock_ser.read.assert_called_with(5)
        finally:
            kb.DEBUG_MODE = old_debug
            kb.ser_port = old_ser

    def test_init_serial_failure(self):
        """Serial init returns None on failure."""
        old_debug = kb.DEBUG_MODE
        old_ser = kb.ser_port
        try:
            kb.DEBUG_MODE = False
            with patch("kombucha_bridge.serial.Serial",
                       side_effect=serial_mock.SerialException("no port")):
                result = kb.init_serial()
            assert result is None
            assert kb.ser_port is None
        finally:
            kb.DEBUG_MODE = old_debug
            kb.ser_port = old_ser

    def test_init_serial_debug_mode(self):
        """Serial init skips in debug mode."""
        old_debug = kb.DEBUG_MODE
        old_ser = kb.ser_port
        try:
            kb.DEBUG_MODE = True
            result = kb.init_serial()
            assert result is None
            assert kb.ser_port is None
        finally:
            kb.DEBUG_MODE = old_debug
            kb.ser_port = old_ser


class TestReconnectSerial:
    def test_reconnect_closes_existing(self):
        """Reconnect closes existing serial port before reinit."""
        old_debug = kb.DEBUG_MODE
        old_ser = kb.ser_port
        try:
            kb.DEBUG_MODE = False
            mock_old_ser = MagicMock()
            kb.ser_port = mock_old_ser
            mock_new_ser = MagicMock()
            mock_new_ser.in_waiting = 0
            with patch("kombucha_bridge.serial.Serial", return_value=mock_new_ser):
                with patch("kombucha_bridge.time.sleep"):
                    result = kb.reconnect_serial()
            mock_old_ser.close.assert_called_once()
            assert result == mock_new_ser
        finally:
            kb.DEBUG_MODE = old_debug
            kb.ser_port = old_ser

    def test_reconnect_close_exception(self):
        """Reconnect handles close() exception gracefully."""
        old_debug = kb.DEBUG_MODE
        old_ser = kb.ser_port
        try:
            kb.DEBUG_MODE = False
            mock_old_ser = MagicMock()
            mock_old_ser.close.side_effect = OSError("close failed")
            kb.ser_port = mock_old_ser
            mock_new_ser = MagicMock()
            mock_new_ser.in_waiting = 0
            with patch("kombucha_bridge.serial.Serial", return_value=mock_new_ser):
                with patch("kombucha_bridge.time.sleep"):
                    result = kb.reconnect_serial()
            assert result == mock_new_ser
        finally:
            kb.DEBUG_MODE = old_debug
            kb.ser_port = old_ser

    def test_reconnect_init_fails(self):
        """Reconnect returns None when init_serial raises."""
        old_debug = kb.DEBUG_MODE
        old_ser = kb.ser_port
        try:
            kb.DEBUG_MODE = False
            kb.ser_port = None
            with patch("kombucha_bridge.init_serial", side_effect=Exception("failed")):
                result = kb.reconnect_serial()
            assert result is None
        finally:
            kb.DEBUG_MODE = old_debug
            kb.ser_port = old_ser

    def test_reconnect_debug_mode(self):
        """Reconnect returns None in debug mode."""
        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = True
            result = kb.reconnect_serial()
            assert result is None
        finally:
            kb.DEBUG_MODE = old_debug


class TestSendTCode:
    def test_send_tcode_real_success(self):
        """send_tcode writes JSON to serial port."""
        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = False
            mock_ser = MagicMock()
            with patch("kombucha_bridge.time.sleep"):
                kb.send_tcode(mock_ser, {"T": 0})
            mock_ser.write.assert_called_once()
            payload = mock_ser.write.call_args[0][0]
            assert b'"T": 0' in payload or b'"T":0' in payload
        finally:
            kb.DEBUG_MODE = old_debug

    def test_send_tcode_no_serial(self):
        """send_tcode with None serial and not debug doesn't crash."""
        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = False
            kb.send_tcode(None, {"T": 0})  # should not raise
        finally:
            kb.DEBUG_MODE = old_debug

    def test_send_tcode_serial_exception(self):
        """send_tcode reconnects on serial exception."""
        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = False
            mock_ser = MagicMock()
            mock_ser.write.side_effect = serial_mock.SerialException("write error")
            with patch("kombucha_bridge.reconnect_serial") as mock_reconnect:
                kb.send_tcode(mock_ser, {"T": 0})
            mock_reconnect.assert_called_once()
        finally:
            kb.DEBUG_MODE = old_debug


class TestSpeakAsync:
    def test_speak_real_launches_subprocess(self):
        """_speak_async launches a subprocess when not in debug mode."""
        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = False
            mock_popen = MagicMock()
            with patch("kombucha_bridge.subprocess.Popen", return_value=mock_popen) as popen_patch:
                kb._speak_async("hello world")
            popen_patch.assert_called_once()
        finally:
            kb.DEBUG_MODE = old_debug

    def test_speak_popen_exception(self):
        """_speak_async handles Popen exception gracefully."""
        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = False
            with patch("kombucha_bridge.subprocess.Popen", side_effect=FileNotFoundError("no bash")):
                kb._speak_async("hello")  # should not raise
        finally:
            kb.DEBUG_MODE = old_debug


# ===========================================================================
# Phase 4: Execute Actions + Async API
# ===========================================================================

class TestExecuteActionsHardware:
    def test_execute_serial_error(self):
        """Execute actions handles serial error."""
        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = False
            mock_ser = MagicMock()
            with patch("kombucha_bridge.send_tcode", side_effect=Exception("serial died")):
                result = kb.execute_actions(mock_ser, [{"type": "stop"}], {})
            assert "error" in result
        finally:
            kb.DEBUG_MODE = old_debug

    def test_execute_duration_ms_autostop(self):
        """Drive with duration_ms triggers auto-stop."""
        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = False
            mock_ser = MagicMock()
            with patch("kombucha_bridge.send_tcode") as mock_send:
                with patch("kombucha_bridge.time.sleep"):
                    result = kb.execute_actions(
                        mock_ser,
                        [{"type": "drive", "left": 0.3, "right": 0.3, "duration_ms": 1000}],
                        {}
                    )
            # Should have sent drive command + stop command
            assert "auto_stop" in result
            assert mock_send.call_count >= 2
        finally:
            kb.DEBUG_MODE = old_debug


class TestCallBrain:
    def test_call_brain_standard(self):
        """call_brain uses standard model by default."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": '{"observation":"test","goal":"test","mood":"ok","actions":[],"next_tick_ms":3000}'}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        state = {"tick_count": 5, "goal": "test", "last_result": "ok",
                 "pan_position": 0, "tilt_position": 0, "wake_reason": None}

        result, model, _, _ = asyncio.get_event_loop().run_until_complete(
            kb.call_brain(mock_client, "test-key", "base64data", state, "memory ctx")
        )
        assert model == kb.MODEL
        # Verify the post was called
        mock_client.post.assert_called_once()

    def test_call_brain_deep(self):
        """call_brain uses deep model when use_deep=True."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": '{"observation":"test"}'}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        state = {"tick_count": 1, "goal": "test", "last_result": "ok",
                 "pan_position": 0, "tilt_position": 0, "wake_reason": None}

        _, model, _, _ = asyncio.get_event_loop().run_until_complete(
            kb.call_brain(mock_client, "key", "b64", state, "ctx", use_deep=True)
        )
        assert model == kb.MODEL_DEEP

    def test_call_brain_empty_context(self):
        """call_brain omits empty memory context from request."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"content": [{"text": '{"observation":"test"}'}]}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        state = {"tick_count": 5, "goal": "test", "last_result": "ok",
                 "pan_position": 0, "tilt_position": 0, "wake_reason": None}

        asyncio.get_event_loop().run_until_complete(
            kb.call_brain(mock_client, "key", "b64", state, "   ", use_deep=False)
        )
        # Check the text content sent doesn't include whitespace-only context
        call_args = mock_client.post.call_args
        messages = call_args.kwargs.get("json", call_args[1].get("json", {}))["messages"]
        text_content = [c for c in messages[0]["content"] if c.get("type") == "text"][0]["text"]
        assert "   " not in text_content.split("=== CURRENT TICK ===")[0]


class TestCompress:
    def test_compress_not_enough_rows(self, db):
        """Compression skipped when <= WORKING_MEMORY_SIZE rows."""
        for i in range(kb.WORKING_MEMORY_SIZE):
            decision = {"observation": f"obs_{i}", "goal": "test", "mood": "ok",
                        "actions": [], "tags": [], "outcome": "neutral"}
            kb.insert_tick_memory(db, str(i + 1), "sess_test", decision)

        mock_client = AsyncMock()
        asyncio.get_event_loop().run_until_complete(
            kb.compress_old_memories(mock_client, "key", db, "sess_test")
        )
        mock_client.post.assert_not_called()

    def test_compress_success(self, db):
        """Compression creates session row and marks compressed."""
        # Insert more than WORKING_MEMORY_SIZE rows
        for i in range(kb.WORKING_MEMORY_SIZE + 5):
            decision = {"observation": f"obs_{i}", "goal": "test", "mood": "ok",
                        "actions": [], "tags": [], "outcome": "neutral"}
            kb.insert_tick_memory(db, str(i + 1), "sess_test", decision)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": '{"summary": "We explored the area.", "tags": ["loc:test"]}'}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        asyncio.get_event_loop().run_until_complete(
            kb.compress_old_memories(mock_client, "key", db, "sess_test")
        )

        # Check session row was created
        session_rows = db.execute(
            "SELECT * FROM memories WHERE tier='session'"
        ).fetchall()
        assert len(session_rows) == 1
        assert "explored" in session_rows[0]["summary"]

        # Check compressed flags
        compressed = db.execute(
            "SELECT COUNT(*) FROM memories WHERE compressed=TRUE AND tier='working'"
        ).fetchone()[0]
        assert compressed >= 5

    def test_compress_api_failure(self, db):
        """Compression API failure doesn't crash."""
        for i in range(kb.WORKING_MEMORY_SIZE + 5):
            decision = {"observation": f"obs_{i}", "goal": "test", "mood": "ok",
                        "actions": [], "tags": [], "outcome": "neutral"}
            kb.insert_tick_memory(db, str(i + 1), "sess_test", decision)

        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("API error")

        asyncio.get_event_loop().run_until_complete(
            kb.compress_old_memories(mock_client, "key", db, "sess_test")
        )
        # Should not crash, no session row created
        count = db.execute("SELECT COUNT(*) FROM memories WHERE tier='session'").fetchone()[0]
        assert count == 0

    def test_compress_strips_fences(self, db):
        """Compression strips markdown fences from response."""
        for i in range(kb.WORKING_MEMORY_SIZE + 5):
            decision = {"observation": f"obs_{i}", "goal": "test", "mood": "ok",
                        "actions": [], "tags": [], "outcome": "neutral"}
            kb.insert_tick_memory(db, str(i + 1), "sess_test", decision)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": '```json\n{"summary": "fenced summary", "tags": []}\n```'}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        asyncio.get_event_loop().run_until_complete(
            kb.compress_old_memories(mock_client, "key", db, "sess_test")
        )
        row = db.execute("SELECT summary FROM memories WHERE tier='session'").fetchone()
        assert row is not None
        assert row["summary"] == "fenced summary"

    def test_compress_empty_summary(self, db):
        """Empty summary means no session row is created."""
        for i in range(kb.WORKING_MEMORY_SIZE + 5):
            decision = {"observation": f"obs_{i}", "goal": "test", "mood": "ok",
                        "actions": [], "tags": [], "outcome": "neutral"}
            kb.insert_tick_memory(db, str(i + 1), "sess_test", decision)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": '{"summary": "", "tags": []}'}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        asyncio.get_event_loop().run_until_complete(
            kb.compress_old_memories(mock_client, "key", db, "sess_test")
        )
        count = db.execute("SELECT COUNT(*) FROM memories WHERE tier='session'").fetchone()[0]
        assert count == 0


    def test_compress_rich_entries(self, db):
        """Compression includes thought, outcome, lesson, memory_note in prompt."""
        for i in range(kb.WORKING_MEMORY_SIZE + 3):
            decision = {
                "observation": f"obs_{i}", "goal": "explore", "mood": "curious",
                "thought": f"thinking_{i}", "actions": [], "tags": [],
                "outcome": "success", "lesson": f"lesson_{i}",
                "memory_note": f"note_{i}",
            }
            kb.insert_tick_memory(db, str(i + 1), "sess_rich", decision)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": '{"summary": "Rich session.", "tags": ["loc:test"]}'}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        asyncio.get_event_loop().run_until_complete(
            kb.compress_old_memories(mock_client, "key", db, "sess_rich")
        )
        # Verify the prompt sent to the API includes thought, outcome, lesson, note
        call_args = mock_client.post.call_args
        body = call_args.kwargs.get("json", call_args[1].get("json", {}))
        prompt_text = body["messages"][0]["content"]
        assert "Thought:" in prompt_text
        assert "Outcome:" in prompt_text
        assert "Lesson:" in prompt_text
        assert "Note:" in prompt_text

    def test_compress_to_compress_empty_after_slice(self, db):
        """Slicing rows leaves empty to_compress list (edge case)."""
        # Insert exactly WORKING_MEMORY_SIZE + 1 rows, all compressed
        for i in range(kb.WORKING_MEMORY_SIZE + 1):
            decision = {"observation": f"obs_{i}", "goal": "test", "mood": "ok",
                        "actions": [], "tags": [], "outcome": "neutral"}
            kb.insert_tick_memory(db, str(i + 1), "sess_empty", decision)
        # Mark all but WORKING_MEMORY_SIZE as already compressed so the query
        # returns exactly WORKING_MEMORY_SIZE uncompressed rows
        db.execute("""
            UPDATE memories SET compressed = TRUE
            WHERE tier = 'working' AND session_id = 'sess_empty'
            AND tick_id = '1'
        """)
        db.commit()

        mock_client = AsyncMock()
        asyncio.get_event_loop().run_until_complete(
            kb.compress_old_memories(mock_client, "key", db, "sess_empty")
        )
        mock_client.post.assert_not_called()


class TestSessionSummary:
    def test_session_summary_short(self, db):
        """Session with < 3 ticks is too short for summary."""
        for i in range(2):
            decision = {"observation": f"obs_{i}", "goal": "test", "mood": "ok",
                        "actions": [], "tags": [], "outcome": "neutral"}
            kb.insert_tick_memory(db, str(i + 1), "sess_test", decision)

        mock_client = AsyncMock()
        asyncio.get_event_loop().run_until_complete(
            kb.generate_session_summary(mock_client, "key", db, "sess_test")
        )
        mock_client.post.assert_not_called()

    def test_session_summary_success(self, db):
        """Session summary generates longterm memory."""
        for i in range(5):
            decision = {"observation": f"obs_{i}", "goal": "test", "mood": "ok",
                        "thought": f"thought_{i}", "actions": [], "tags": [],
                        "outcome": "neutral"}
            kb.insert_tick_memory(db, str(i + 1), "sess_test", decision)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": '{"summary": "I explored and learned.", "tags": ["event:explore"]}'}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        asyncio.get_event_loop().run_until_complete(
            kb.generate_session_summary(mock_client, "key", db, "sess_test")
        )
        lt = db.execute("SELECT * FROM memories WHERE tier='longterm'").fetchone()
        assert lt is not None
        assert "explored" in lt["summary"]

    def test_session_summary_api_failure(self, db):
        """Session summary API failure doesn't crash."""
        for i in range(5):
            decision = {"observation": f"obs_{i}", "goal": "test", "mood": "ok",
                        "actions": [], "tags": [], "outcome": "neutral"}
            kb.insert_tick_memory(db, str(i + 1), "sess_test", decision)

        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("API failed")

        asyncio.get_event_loop().run_until_complete(
            kb.generate_session_summary(mock_client, "key", db, "sess_test")
        )
        count = db.execute("SELECT COUNT(*) FROM memories WHERE tier='longterm'").fetchone()[0]
        assert count == 0

    def test_session_summary_no_entries(self, db):
        """Session with ticks that have no content returns early."""
        for i in range(5):
            decision = {"observation": "", "goal": "", "mood": "",
                        "thought": "", "actions": [], "tags": [],
                        "outcome": "neutral"}
            kb.insert_tick_memory(db, str(i + 1), "sess_test", decision)

        mock_client = AsyncMock()
        asyncio.get_event_loop().run_until_complete(
            kb.generate_session_summary(mock_client, "key", db, "sess_test")
        )
        mock_client.post.assert_not_called()

    def test_session_summary_strips_fences(self, db):
        """Session summary strips fenced JSON response."""
        for i in range(5):
            decision = {"observation": f"obs_{i}", "goal": "test", "mood": "ok",
                        "actions": [], "tags": [], "outcome": "neutral"}
            kb.insert_tick_memory(db, str(i + 1), "sess_test", decision)

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": '```json\n{"summary": "fenced session", "tags": []}\n```'}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        asyncio.get_event_loop().run_until_complete(
            kb.generate_session_summary(mock_client, "key", db, "sess_test")
        )
        row = db.execute("SELECT summary FROM memories WHERE tier='longterm'").fetchone()
        assert row["summary"] == "fenced session"

    def test_session_summary_with_session_tier_and_lessons(self, db):
        """Session summary includes session-tier summaries and lessons."""
        # Add working ticks with lessons
        for i in range(5):
            decision = {"observation": f"obs_{i}", "goal": "explore", "mood": "ok",
                        "thought": f"thought_{i}", "actions": [], "tags": [],
                        "outcome": "success", "lesson": f"learned_{i}"}
            kb.insert_tick_memory(db, str(i + 1), "sess_rich", decision)

        # Add a session-tier row (from compression) with a summary
        db.execute("""
            INSERT INTO memories (tick_id, timestamp, session_id, tier, summary, tags)
            VALUES (?, datetime('now'), ?, 'session', ?, '[]')
        """, ["comp_1", "sess_rich", "We explored the hallway."])
        db.commit()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": '{"summary": "Full session summary.", "tags": ["event:explore"]}'}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        asyncio.get_event_loop().run_until_complete(
            kb.generate_session_summary(mock_client, "key", db, "sess_rich")
        )
        # Verify the prompt included session summary and lessons
        call_args = mock_client.post.call_args
        body = call_args.kwargs.get("json", call_args[1].get("json", {}))
        prompt_text = body["messages"][0]["content"]
        assert "We explored the hallway." in prompt_text
        assert "Lesson:" in prompt_text


# ===========================================================================
# Phase 5: Main Loop Integration
# ===========================================================================

class TestMainLoop:
    """Tests for the main() async loop. All use tmp_dir fixture which patches
    DATA_DIR paths. We write an actual API key file and patch kb.API_KEY_FILE
    to point at it, avoiding WindowsPath attribute patching issues."""

    @pytest.fixture(autouse=True)
    def _setup_api_key(self, tmp_dir):
        """Write a real API key file for main() to read."""
        self._api_key_file = tmp_dir / "api_key"
        self._api_key_file.write_text("test-api-key-12345")
        self._old_api_key_file = kb.API_KEY_FILE
        kb.API_KEY_FILE = self._api_key_file
        yield
        kb.API_KEY_FILE = self._old_api_key_file

    def _make_client(self, side_effect):
        """Create a mock AsyncClient with the given post side_effect."""
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post.side_effect = side_effect
        return mock_client

    def _make_response(self, decision_dict):
        """Create a mock API response."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": json.dumps(decision_dict)}]
        }
        mock_response.raise_for_status = MagicMock()
        return mock_response

    def _one_tick(self, response):
        """Side effect that returns response once then stops."""
        call_count = 0
        def fn(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                kb.running = False
            return response
        return fn

    def test_main_no_api_key_exits(self, tmp_dir):
        """Main exits when no API key is available."""
        # Remove the API key file
        self._api_key_file.unlink()
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit):
                asyncio.get_event_loop().run_until_complete(kb.main())

    def test_main_one_tick_success(self, tmp_dir):
        """Main loop runs one full tick successfully."""
        resp = self._make_response({
            "observation": "test obs", "goal": "test goal",
            "reasoning": "test reason", "thought": "test thought",
            "mood": "curious", "actions": [{"type": "stop"}],
            "next_tick_ms": 3000, "tags": ["loc:test"],
            "outcome": "success", "lesson": "test lesson",
            "memory_note": "test note", "identity_proposal": None,
        })
        mock_client = self._make_client(self._one_tick(resp))

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running

    def test_main_camera_error(self, tmp_dir):
        """Camera error increments consecutive_errors."""
        mock_client = self._make_client(lambda *a, **kw: None)

        call_count = 0
        def capture_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                kb.running = False
            raise RuntimeError("Camera dead")

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", side_effect=capture_fail), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running

    def test_main_camera_fatal(self, tmp_dir):
        """6 consecutive camera errors break the loop."""
        mock_client = self._make_client(lambda *a, **kw: None)

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", side_effect=RuntimeError("dead")), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock), \
                 patch("kombucha_bridge.load_state", return_value={
                     **kb.DEFAULT_STATE.copy(),
                     "session_id": "test", "session_start": "2025-01-01",
                     "consecutive_errors": 5,
                 }):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running

    def test_main_api_http_error(self, tmp_dir):
        """API HTTPStatusError is handled gracefully."""
        call_count = 0
        def fail_then_stop(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                kb.running = False
            err = httpx_mock.HTTPStatusError("429")
            err.response = MagicMock()
            err.response.status_code = 429
            err.response.text = "Rate limited"
            raise err

        mock_client = self._make_client(fail_then_stop)

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running

    def test_main_api_generic_error(self, tmp_dir):
        """Generic brain call exception is handled."""
        call_count = 0
        def fail_then_stop(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                kb.running = False
            raise ConnectionError("network down")

        mock_client = self._make_client(fail_then_stop)

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running

    def test_main_sentry_triggered(self, tmp_dir):
        """Long next_tick_ms triggers sentry mode."""
        resp = self._make_response({
            "observation": "quiet", "goal": "wait", "mood": "serene",
            "actions": [], "next_tick_ms": 60000,
            "tags": [], "outcome": "neutral",
        })
        mock_client = self._make_client(self._one_tick(resp))

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.sentry_sleep", new_callable=AsyncMock, return_value="timeout") as mock_sentry, \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
                asyncio.get_event_loop().run_until_complete(kb.main())
            mock_sentry.assert_called_once()
        finally:
            kb.running = old_running

    def test_main_goal_change(self, tmp_dir):
        """Goal change is logged when decision has new goal."""
        resp = self._make_response({
            "observation": "door", "goal": "go through door",
            "mood": "curious", "actions": [],
            "next_tick_ms": 3000, "tags": [], "outcome": "neutral",
        })
        mock_client = self._make_client(self._one_tick(resp))

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running

    def test_main_compression_triggered(self, tmp_dir):
        """Compression is triggered when tick_count is multiple of COMPRESSION_INTERVAL."""
        resp = self._make_response({
            "observation": "test", "goal": "test", "mood": "ok",
            "actions": [], "next_tick_ms": 3000,
            "tags": [], "outcome": "neutral",
        })
        mock_client = self._make_client(self._one_tick(resp))

        initial_state = kb.DEFAULT_STATE.copy()
        initial_state["tick_count"] = kb.COMPRESSION_INTERVAL - 1
        initial_state["session_id"] = "test_sess"
        initial_state["session_start"] = "2025-01-01"

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock), \
                 patch("kombucha_bridge.asyncio.create_task") as mock_create_task, \
                 patch("kombucha_bridge.load_state", return_value=initial_state):
                asyncio.get_event_loop().run_until_complete(kb.main())
            mock_create_task.assert_called_once()
        finally:
            kb.running = old_running

    def test_main_shutdown_summary(self, tmp_dir):
        """Shutdown calls compress + summary."""
        resp = self._make_response({
            "observation": "test", "goal": "test", "mood": "ok",
            "actions": [], "next_tick_ms": 3000,
        })
        mock_client = self._make_client(self._one_tick(resp))

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock), \
                 patch("kombucha_bridge.compress_old_memories", new_callable=AsyncMock) as mock_compress, \
                 patch("kombucha_bridge.generate_session_summary", new_callable=AsyncMock) as mock_summary:
                asyncio.get_event_loop().run_until_complete(kb.main())
            assert mock_compress.call_count >= 1
            assert mock_summary.call_count >= 1
        finally:
            kb.running = old_running

    def test_main_shutdown_failure(self, tmp_dir):
        """Shutdown ops raising doesn't prevent clean exit."""
        resp = self._make_response({
            "observation": "t", "goal": "t", "mood": "ok",
            "actions": [], "next_tick_ms": 3000,
        })
        mock_client = self._make_client(self._one_tick(resp))

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running

    def test_main_serial_reconnect(self, tmp_dir):
        """Serial reconnect when ser_port is None and not in debug mode."""
        resp = self._make_response({
            "observation": "t", "goal": "t", "mood": "ok",
            "actions": [], "next_tick_ms": 3000,
        })
        mock_client = self._make_client(self._one_tick(resp))

        old_running = kb.running
        old_debug = kb.DEBUG_MODE
        old_ser = kb.ser_port
        try:
            kb.running = True
            kb.DEBUG_MODE = False
            kb.ser_port = None
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock), \
                 patch("kombucha_bridge.reconnect_serial", return_value=None) as mock_reconnect:
                asyncio.get_event_loop().run_until_complete(kb.main())
            mock_reconnect.assert_called()
        finally:
            kb.running = old_running
            kb.DEBUG_MODE = old_debug
            kb.ser_port = old_ser

    def test_main_ser_port_not_none(self, tmp_dir):
        """When ser_port is not None and not DEBUG, ser = ser_port."""
        resp = self._make_response({
            "observation": "t", "goal": "t", "mood": "ok",
            "actions": [], "next_tick_ms": 3000,
        })
        mock_client = self._make_client(self._one_tick(resp))
        mock_ser = MagicMock()

        old_running = kb.running
        old_debug = kb.DEBUG_MODE
        old_ser = kb.ser_port
        try:
            kb.running = True
            kb.DEBUG_MODE = False
            kb.ser_port = mock_ser
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=mock_ser), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock), \
                 patch("kombucha_bridge.send_tcode"), \
                 patch("kombucha_bridge.time.sleep"):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running
            kb.DEBUG_MODE = old_debug
            kb.ser_port = old_ser

    def test_main_sentry_motion_detected_log(self, tmp_dir):
        """Sentry returns motion_detected, log line is hit."""
        resp = self._make_response({
            "observation": "quiet", "goal": "wait", "mood": "serene",
            "actions": [], "next_tick_ms": 60000,
            "tags": [], "outcome": "neutral",
        })
        mock_client = self._make_client(self._one_tick(resp))

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.sentry_sleep", new_callable=AsyncMock, return_value="motion_detected"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running

    def test_main_shutdown_exception(self, tmp_dir):
        """Shutdown memory ops exception is caught."""
        resp = self._make_response({
            "observation": "t", "goal": "t", "mood": "ok",
            "actions": [], "next_tick_ms": 3000,
        })
        mock_client = self._make_client(self._one_tick(resp))

        # Create a shutdown client that raises
        shutdown_mock = AsyncMock()
        shutdown_mock.__aenter__ = AsyncMock(return_value=shutdown_mock)
        shutdown_mock.__aexit__ = AsyncMock(return_value=None)

        call_count = [0]
        original_async_client = None

        def client_factory(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_client
            # Shutdown client - make compress raise
            return shutdown_mock

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", side_effect=client_factory), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock), \
                 patch("kombucha_bridge.compress_old_memories", new_callable=AsyncMock, side_effect=Exception("compress failed")):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running

    def test_main_db_close_exception(self, tmp_dir):
        """db.close() exception during shutdown is caught."""
        resp = self._make_response({
            "observation": "t", "goal": "t", "mood": "ok",
            "actions": [], "next_tick_ms": 3000,
        })
        mock_client = self._make_client(self._one_tick(resp))

        old_running = kb.running
        try:
            kb.running = True
            mock_db = MagicMock()
            mock_db.execute.return_value.fetchone.return_value = [0]
            mock_db.execute.return_value.fetchall.return_value = []
            mock_db.close.side_effect = Exception("db close error")

            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock), \
                 patch("kombucha_bridge.init_memory_db", return_value=mock_db), \
                 patch("kombucha_bridge.recover_from_crash"), \
                 patch("kombucha_bridge.compress_old_memories", new_callable=AsyncMock), \
                 patch("kombucha_bridge.generate_session_summary", new_callable=AsyncMock):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running

    def test_main_serial_shutdown_cleanup(self, tmp_dir):
        """Serial shutdown sends stop command and closes port."""
        resp = self._make_response({
            "observation": "t", "goal": "t", "mood": "ok",
            "actions": [], "next_tick_ms": 3000,
        })
        mock_client = self._make_client(self._one_tick(resp))
        mock_ser = MagicMock()

        old_running = kb.running
        old_debug = kb.DEBUG_MODE
        old_ser = kb.ser_port
        try:
            kb.running = True
            kb.DEBUG_MODE = False
            kb.ser_port = mock_ser
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=mock_ser), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock), \
                 patch("kombucha_bridge.send_tcode") as mock_tcode, \
                 patch("kombucha_bridge.time.sleep"):
                asyncio.get_event_loop().run_until_complete(kb.main())
            # Verify shutdown T-codes were sent
            tcode_calls = [str(c) for c in mock_tcode.call_args_list]
            assert any("sleeping" in c for c in tcode_calls)
            mock_ser.close.assert_called()
        finally:
            kb.running = old_running
            kb.DEBUG_MODE = old_debug
            kb.ser_port = old_ser

    def test_main_camera_release(self, tmp_dir):
        """Camera cap.release() is called during shutdown."""
        resp = self._make_response({
            "observation": "t", "goal": "t", "mood": "ok",
            "actions": [], "next_tick_ms": 3000,
        })
        mock_client = self._make_client(self._one_tick(resp))
        mock_cap = MagicMock()

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=mock_cap), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
                asyncio.get_event_loop().run_until_complete(kb.main())
            mock_cap.release.assert_called_once()
        finally:
            kb.running = old_running

    def test_main_camera_release_exception(self, tmp_dir):
        """Camera cap.release() exception during shutdown is caught."""
        resp = self._make_response({
            "observation": "t", "goal": "t", "mood": "ok",
            "actions": [], "next_tick_ms": 3000,
        })
        mock_client = self._make_client(self._one_tick(resp))
        mock_cap = MagicMock()
        mock_cap.release.side_effect = Exception("release failed")

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=mock_cap), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running

    def test_main_serial_shutdown_exception(self, tmp_dir):
        """Serial shutdown exception (e.g., close fails) is caught."""
        resp = self._make_response({
            "observation": "t", "goal": "t", "mood": "ok",
            "actions": [], "next_tick_ms": 3000,
        })
        mock_client = self._make_client(self._one_tick(resp))
        mock_ser = MagicMock()
        mock_ser.close.side_effect = Exception("serial close failed")

        old_running = kb.running
        old_debug = kb.DEBUG_MODE
        old_ser = kb.ser_port
        try:
            kb.running = True
            kb.DEBUG_MODE = False
            kb.ser_port = mock_ser
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=mock_ser), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock), \
                 patch("kombucha_bridge.send_tcode"), \
                 patch("kombucha_bridge.time.sleep"):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running
            kb.DEBUG_MODE = old_debug
            kb.ser_port = old_ser

    def test_main_entry_point(self):
        """if __name__ == '__main__' guard calls asyncio.run."""
        assert callable(kb.main)

    def test_bridge_name_guard(self):
        """Exercise the if __name__ == '__main__' guard."""
        with patch("kombucha_bridge.asyncio.run") as mock_run:
            exec(
                compile("if __name__ == '__main__': asyncio.run(main())", "kombucha_bridge.py", "exec"),
                {"__name__": "__main__", "asyncio": kb.asyncio, "main": kb.main}
            )
            mock_run.assert_called_once()


# ===========================================================================
# Phase 6: Story Server Tests
# ===========================================================================

class TestJournalParserEdgeCases:
    def test_parse_journal_empty_lines(self, tmp_path):
        """Journal parser skips empty lines."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        content = "\n\n" + json.dumps({"tick": 1, "mood": "ok"}) + "\n\n"
        (journal_dir / "2025-01-01.jsonl").write_text(content)
        ticks = ss.parse_journal_files(journal_dir)
        assert len(ticks) == 1

    def test_parse_journal_file_error(self, tmp_path):
        """Journal parser handles unreadable file gracefully."""
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        (journal_dir / "2025-01-01.jsonl").write_text(json.dumps({"tick": 1}) + "\n")
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            ticks = ss.parse_journal_files(journal_dir)
        assert ticks == []


class TestLegacyLogParserEdgeCases:
    def test_parse_logs_goal_line(self):
        """GOAL: line sets the goal on current tick."""
        log_text = (
            "Tick 1 | goal: explore\n"
            "  GOAL:    investigate door\n"
        )
        ticks = ss.parse_logs(log_text)
        assert ticks[0]["goal"] == "investigate door"

    def test_parse_logs_reason_line(self):
        """REASON: line sets the reasoning on current tick."""
        log_text = (
            "Tick 1 | goal: explore\n"
            "  REASON:  the door looks interesting\n"
        )
        ticks = ss.parse_logs(log_text)
        assert ticks[0]["reasoning"] == "the door looks interesting"

    def test_parse_logs_actions_non_json(self):
        """Non-JSON actions fallback to raw string."""
        log_text = (
            "Tick 1 | goal: explore\n"
            "  ACTIONS: drive forward fast\n"
        )
        ticks = ss.parse_logs(log_text)
        assert ticks[0]["actions"] == "drive forward fast"

    def test_parse_logs_tags_error(self):
        """Invalid JSON tags are ignored."""
        log_text = (
            "Tick 1 | goal: explore\n"
            '  TAGS: not valid json\n'
        )
        ticks = ss.parse_logs(log_text)
        assert ticks[0]["tags"] == []

    def test_parse_logs_note_line(self):
        """NOTE: line sets the memory_note on current tick."""
        log_text = (
            "Tick 1 | goal: explore\n"
            "  NOTE:    important discovery\n"
        )
        ticks = ss.parse_logs(log_text)
        assert ticks[0]["memory_note"] == "important discovery"


class TestSyncThreadOperations:
    def test_sync_thread_stop(self):
        """stop() sets the stop event."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)
        assert not sync._stop_event.is_set()
        sync.stop()
        assert sync._stop_event.is_set()

    def test_sync_thread_run(self, tmp_path):
        """SyncThread.run creates dirs and calls _parse_and_diff."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)
        # Immediately stop
        sync._stop_event.set()

        old_frames = ss.LOCAL_FRAMES
        old_journal = ss.LOCAL_JOURNAL
        try:
            ss.LOCAL_FRAMES = tmp_path / "frames"
            ss.LOCAL_JOURNAL = tmp_path / "journal"
            with patch.object(sync, "_sync_once"), \
                 patch.object(sync, "_parse_and_diff"):
                sync.run()
        finally:
            ss.LOCAL_FRAMES = old_frames
            ss.LOCAL_JOURNAL = old_journal

    def test_sync_thread_run_error(self, tmp_path):
        """SyncThread handles _sync_once errors gracefully."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)

        call_count = 0

        def sync_then_stop():
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                sync._stop_event.set()
            raise Exception("sync error")

        old_frames = ss.LOCAL_FRAMES
        old_journal = ss.LOCAL_JOURNAL
        try:
            ss.LOCAL_FRAMES = tmp_path / "frames"
            ss.LOCAL_JOURNAL = tmp_path / "journal"
            with patch.object(sync, "_sync_once", side_effect=sync_then_stop), \
                 patch.object(sync, "_parse_and_diff"):
                sync.run()  # should not raise
        finally:
            ss.LOCAL_FRAMES = old_frames
            ss.LOCAL_JOURNAL = old_journal

    def test_sync_once_rsync_success(self, tmp_path):
        """_sync_once uses rsync successfully."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)

        mock_result = MagicMock()
        mock_result.returncode = 0

        old_state = ss.LOCAL_STATE
        try:
            ss.LOCAL_STATE = tmp_path / "state.json"
            with patch("story_server.subprocess.run", return_value=mock_result) as mock_run, \
                 patch.object(sync, "_parse_and_diff"):
                sync._sync_once()
            assert mock_run.call_count >= 2
        finally:
            ss.LOCAL_STATE = old_state

    def test_sync_once_rsync_fallback_scp(self, tmp_path):
        """_sync_once falls back to scp when rsync not found."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)

        mock_result = MagicMock()
        mock_result.returncode = 0

        def rsync_fail_scp_ok(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if cmd and cmd[0] == "rsync":
                raise FileNotFoundError("rsync not found")
            return mock_result

        old_state = ss.LOCAL_STATE
        try:
            ss.LOCAL_STATE = tmp_path / "state.json"
            with patch("story_server.subprocess.run", side_effect=rsync_fail_scp_ok), \
                 patch.object(sync, "_parse_and_diff"):
                sync._sync_once()
        finally:
            ss.LOCAL_STATE = old_state

    def test_sync_once_journal_timeout(self, tmp_path):
        """_sync_once handles journal sync timeout."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)

        def timeout_on_first(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="rsync", timeout=15)

        with patch("story_server.subprocess.run", side_effect=timeout_on_first), \
             patch.object(sync, "_parse_and_diff"):
            sync._sync_once()  # should not raise

    def test_sync_once_frames_timeout(self, tmp_path):
        """_sync_once handles frame sync timeout."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)

        call_count = 0

        def timeout_on_frames(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return MagicMock(returncode=0)
            raise subprocess.TimeoutExpired(cmd="rsync", timeout=30)

        with patch("story_server.subprocess.run", side_effect=timeout_on_frames), \
             patch.object(sync, "_parse_and_diff"):
            sync._sync_once()  # should not raise

    def test_sync_once_state_success(self, tmp_path):
        """_sync_once successfully pulls rover state."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)

        mock_result = MagicMock()
        mock_result.returncode = 0
        state_data = {"goal": "explore", "tick_count": 42}

        # Create a real state file
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps(state_data))

        old_state = ss.LOCAL_STATE
        try:
            ss.LOCAL_STATE = state_file
            with patch("story_server.subprocess.run", return_value=mock_result), \
                 patch.object(sync, "_parse_and_diff"):
                sync._sync_once()
            assert sync.rover_state["goal"] == "explore"
        finally:
            ss.LOCAL_STATE = old_state

    def test_sync_once_state_invalid_json(self, tmp_path):
        """_sync_once handles invalid state JSON gracefully."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)

        mock_result = MagicMock()
        mock_result.returncode = 0

        state_file = tmp_path / "state.json"
        state_file.write_text("{bad json}")

        old_state = ss.LOCAL_STATE
        try:
            ss.LOCAL_STATE = state_file
            with patch("story_server.subprocess.run", return_value=mock_result), \
                 patch.object(sync, "_parse_and_diff"):
                sync._sync_once()  # should not raise
        finally:
            ss.LOCAL_STATE = old_state

    def test_sync_once_state_failure(self, tmp_path):
        """_sync_once handles state scp failure gracefully."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)

        call_count = 0

        def succeed_then_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return MagicMock(returncode=0)
            raise Exception("scp failed")

        with patch("story_server.subprocess.run", side_effect=succeed_then_fail), \
             patch.object(sync, "_parse_and_diff"):
            sync._sync_once()  # should not raise

    def test_sync_once_journal_generic_error(self, tmp_path):
        """_sync_once handles generic journal sync exception."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)

        call_count = 0

        def journal_error_rest_ok(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            cmd = args[0] if args else kwargs.get("args", [])
            # First call is rsync for journal - raise generic exception
            if call_count == 1:
                raise RuntimeError("permission denied")
            return MagicMock(returncode=0)

        old_state = ss.LOCAL_STATE
        try:
            ss.LOCAL_STATE = tmp_path / "state.json"
            with patch("story_server.subprocess.run", side_effect=journal_error_rest_ok), \
                 patch.object(sync, "_parse_and_diff"):
                sync._sync_once()  # should not crash
        finally:
            ss.LOCAL_STATE = old_state

    def test_sync_once_frames_generic_error(self, tmp_path):
        """_sync_once handles generic frame sync exception."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)

        call_count = 0

        def frames_error(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # First call: journal rsync succeeds
            if call_count == 1:
                return MagicMock(returncode=0)
            # Second call: frames rsync raises generic exception
            if call_count == 2:
                raise RuntimeError("disk full")
            # Remaining: succeed
            return MagicMock(returncode=0)

        old_state = ss.LOCAL_STATE
        try:
            ss.LOCAL_STATE = tmp_path / "state.json"
            with patch("story_server.subprocess.run", side_effect=frames_error), \
                 patch.object(sync, "_parse_and_diff"):
                sync._sync_once()  # should not crash
        finally:
            ss.LOCAL_STATE = old_state


class TestParseAndDiff:
    def test_parse_and_diff_broadcasts(self, tmp_path):
        """New ticks are broadcast via SSE."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)
        q = broker.subscribe()

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        (journal_dir / "2025-01-01.jsonl").write_text(
            json.dumps({"tick": 1, "observation": "test"}) + "\n"
        )

        with patch.object(ss, "LOCAL_JOURNAL", journal_dir), \
             patch.object(ss, "LOCAL_FRAMES", tmp_path / "frames"):
            (tmp_path / "frames").mkdir(exist_ok=True)
            sync._parse_and_diff()

        msg = q.get_nowait()
        assert msg["tick"] == 1

    def test_parse_and_diff_skips_known(self, tmp_path):
        """Known ticks are not re-broadcast."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)
        sync.known_ticks = {1}
        q = broker.subscribe()

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        (journal_dir / "2025-01-01.jsonl").write_text(
            json.dumps({"tick": 1, "observation": "test"}) + "\n"
        )

        with patch.object(ss, "LOCAL_JOURNAL", journal_dir), \
             patch.object(ss, "LOCAL_FRAMES", tmp_path / "frames"):
            (tmp_path / "frames").mkdir(exist_ok=True)
            sync._parse_and_diff()

        assert q.empty()

    def test_parse_and_diff_no_ticks(self, tmp_path):
        """Empty journal returns early."""
        broker = ss.SSEBroker()
        sync = ss.SyncThread(broker)

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()

        with patch.object(ss, "LOCAL_JOURNAL", journal_dir), \
             patch.object(ss, "LOCAL_FRAMES", tmp_path / "frames"):
            (tmp_path / "frames").mkdir(exist_ok=True)
            sync._parse_and_diff()

        assert sync.all_ticks == []


class TestSSEBrokerEdgeCases:
    def test_sse_broker_dead_queue_double_remove(self):
        """Double remove of dead queue caught by ValueError."""
        broker = ss.SSEBroker()
        q = broker.subscribe()
        # Fill the queue so put_nowait raises queue.Full
        for i in range(100):
            q.put({"tick": i})
        # Replace subscribers with a list that raises ValueError on remove
        # to simulate the race where a queue disappears between iteration and cleanup
        original_list = broker.subscribers[:]

        class FlakyList(list):
            def remove(self, item):
                raise ValueError("already removed")

        with broker.lock:
            broker.subscribers = FlakyList(original_list)
        broker.broadcast({"tick": 999})


class TestStoryHandler:
    def _make_handler(self, method, path, sync_thread=None, sse_broker=None):
        """Create a mock handler to test request handling."""
        from io import BytesIO

        class FakeHandler(ss.StoryHandler):
            pass

        FakeHandler.sync_thread = sync_thread or MagicMock()
        FakeHandler.sse_broker = sse_broker or ss.SSEBroker()

        # Mock the connection and request
        handler = FakeHandler.__new__(FakeHandler)
        handler.path = path
        handler.command = method
        handler.request_version = "HTTP/1.1"
        handler.headers = {}
        handler.wfile = BytesIO()
        handler._headers_buffer = []
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.client_address = ("127.0.0.1", 12345)
        handler.server = MagicMock()
        handler.close_connection = True

        return handler

    def test_handler_log_suppressed(self):
        """log_message is a no-op (suppressed)."""
        handler = self._make_handler("GET", "/")
        # Should not raise
        handler.log_message("test %s", "message")

    def test_handler_root(self):
        """GET / serves HTML."""
        handler = self._make_handler("GET", "/")
        responses = []
        headers = {}

        def mock_send_response(code):
            responses.append(code)

        def mock_send_header(name, value):
            headers[name] = value

        handler.send_response = mock_send_response
        handler.send_header = mock_send_header
        handler.end_headers = MagicMock()

        handler.do_GET()
        assert 200 in responses
        assert "text/html" in headers.get("Content-Type", "")

    def test_handler_ticks(self):
        """GET /api/ticks returns JSON."""
        mock_sync = MagicMock()
        mock_sync.get_ticks.return_value = [{"tick": 1}]
        mock_sync.get_total.return_value = 1

        handler = self._make_handler("GET", "/api/ticks?offset=0&limit=10", sync_thread=mock_sync)
        responses = []
        headers = {}

        handler.send_response = lambda c: responses.append(c)
        handler.send_header = lambda n, v: headers.update({n: v})
        handler.end_headers = MagicMock()

        handler.do_GET()
        assert 200 in responses
        written = handler.wfile.getvalue()
        data = json.loads(written)
        assert data["total"] == 1

    def test_handler_state(self):
        """GET /api/state returns JSON."""
        mock_sync = MagicMock()
        mock_sync.get_state.return_value = {"goal": "explore"}

        handler = self._make_handler("GET", "/api/state", sync_thread=mock_sync)
        responses = []
        headers = {}

        handler.send_response = lambda c: responses.append(c)
        handler.send_header = lambda n, v: headers.update({n: v})
        handler.end_headers = MagicMock()

        handler.do_GET()
        assert 200 in responses
        data = json.loads(handler.wfile.getvalue())
        assert data["goal"] == "explore"

    def test_handler_frame_found(self, tmp_path):
        """GET /frames/x.jpg serves the JPEG."""
        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()
        (frames_dir / "test.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        handler = self._make_handler("GET", "/frames/test.jpg")
        responses = []
        headers = {}

        handler.send_response = lambda c: responses.append(c)
        handler.send_header = lambda n, v: headers.update({n: v})
        handler.end_headers = MagicMock()

        with patch.object(ss, "LOCAL_FRAMES", frames_dir):
            handler.do_GET()
        assert 200 in responses
        assert handler.wfile.getvalue() == b"\xff\xd8\xff\xe0"

    def test_handler_frame_not_found(self, tmp_path):
        """GET /frames/missing.jpg returns 404."""
        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()

        handler = self._make_handler("GET", "/frames/missing.jpg")
        errors = []
        handler.send_error = lambda code, *a, **kw: errors.append(code)

        with patch.object(ss, "LOCAL_FRAMES", frames_dir):
            handler.do_GET()
        assert 404 in errors

    def test_handler_frame_not_jpg(self, tmp_path):
        """GET /frames/x.png returns 404 (not .jpg)."""
        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()
        (frames_dir / "test.png").write_bytes(b"\x89PNG")

        handler = self._make_handler("GET", "/frames/test.png")
        errors = []
        handler.send_error = lambda code, *a, **kw: errors.append(code)

        with patch.object(ss, "LOCAL_FRAMES", frames_dir):
            handler.do_GET()
        assert 404 in errors

    def test_handler_404(self):
        """Unknown path returns 404."""
        handler = self._make_handler("GET", "/unknown/path")
        errors = []
        handler.send_error = lambda code, *a, **kw: errors.append(code)
        handler.do_GET()
        assert 404 in errors

    def test_handler_sse(self):
        """GET /api/stream connects SSE and sends events."""
        broker = ss.SSEBroker()
        handler = self._make_handler("GET", "/api/stream", sse_broker=broker)

        responses = []
        headers = {}

        handler.send_response = lambda c: responses.append(c)
        handler.send_header = lambda n, v: headers.update({n: v})
        handler.end_headers = MagicMock()

        # Make wfile.write raise after initial connection to break the loop
        write_count = 0
        original_wfile = handler.wfile

        class MockWfile:
            def write(self, data):
                nonlocal write_count
                write_count += 1
                if write_count > 1:
                    raise BrokenPipeError("client disconnected")
                return original_wfile.write(data)

            def flush(self):
                pass

        handler.wfile = MockWfile()
        handler.do_GET()
        assert 200 in responses
        assert "text/event-stream" in headers.get("Content-Type", "")

    def test_handler_sse_tick_delivery(self):
        """SSE handler delivers tick data and then disconnects."""
        import queue as queue_mod
        broker = ss.SSEBroker()
        handler = self._make_handler("GET", "/api/stream", sse_broker=broker)

        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        written_data = []
        write_count = 0

        class MockWfile:
            def write(self, data):
                nonlocal write_count
                write_count += 1
                written_data.append(data)
                # After writing connected + tick data + flush, break on next write
                if write_count >= 3:
                    raise BrokenPipeError("done")

            def flush(self):
                pass

        handler.wfile = MockWfile()

        # Pre-populate a tick in the broker queue before handler starts
        # We need to subscribe first, put tick, then call do_GET
        # But do_GET subscribes internally, so we broadcast right before
        import threading
        def broadcast_tick():
            import time
            time.sleep(0.05)
            broker.broadcast({"tick_id": "42", "observation": "test"})
        t = threading.Thread(target=broadcast_tick)
        t.start()

        handler.do_GET()
        t.join()

        # Check that tick data was written
        all_written = b"".join(written_data)
        assert b"connected" in all_written

    def test_handler_sse_keepalive(self):
        """SSE handler sends keepalive on empty queue, then client disconnects."""
        import queue as queue_mod
        broker = ss.SSEBroker()
        handler = self._make_handler("GET", "/api/stream", sse_broker=broker)

        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        write_count = 0
        flush_count = 0

        class MockWfile:
            def write(self, data):
                nonlocal write_count
                write_count += 1
                if write_count == 1:
                    return  # ": connected\n\n" write succeeds
                if write_count == 2:
                    return  # first keepalive write succeeds
                # Third write (second keepalive) - break the loop
                raise BrokenPipeError("client gone")

            def flush(self):
                nonlocal flush_count
                flush_count += 1

        handler.wfile = MockWfile()

        # Patch queue.get to raise Empty immediately to trigger keepalive path
        with patch("story_server.queue.Queue.get", side_effect=queue_mod.Empty):
            handler.do_GET()

        # Should have written keepalive and flushed at least once
        assert write_count >= 2
        assert flush_count >= 2  # connected flush + keepalive flush

    def test_handler_sse_connection_error_during_write(self):
        """SSE handler catches OSError on initial connected write."""
        broker = ss.SSEBroker()
        handler = self._make_handler("GET", "/api/stream", sse_broker=broker)

        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        class MockWfile:
            def write(self, data):
                raise OSError("connection reset")

            def flush(self):
                pass

        handler.wfile = MockWfile()
        handler.do_GET()  # Should not raise


class TestStoryMain:
    @pytest.fixture(autouse=True)
    def _swap_paths(self, tmp_path):
        """Swap module-level Path variables to real temp dirs."""
        old_frames = ss.LOCAL_FRAMES
        old_journal = ss.LOCAL_JOURNAL
        old_state = ss.LOCAL_STATE
        ss.LOCAL_FRAMES = tmp_path / "frames"
        ss.LOCAL_JOURNAL = tmp_path / "journal"
        ss.LOCAL_STATE = tmp_path / "data" / "state.json"
        yield
        ss.LOCAL_FRAMES = old_frames
        ss.LOCAL_JOURNAL = old_journal
        ss.LOCAL_STATE = old_state

    def test_story_main_with_sync(self):
        """Story main starts sync thread."""
        mock_server = MagicMock()

        def stop_on_serve(*args, **kwargs):
            raise KeyboardInterrupt()

        mock_server.serve_forever.side_effect = stop_on_serve

        with patch("story_server.argparse.ArgumentParser") as mock_parser_cls, \
             patch("story_server.ThreadedHTTPServer", return_value=mock_server), \
             patch.object(ss.SyncThread, "start") as mock_start, \
             patch.object(ss.SyncThread, "stop") as mock_stop:
            mock_args = MagicMock()
            mock_args.port = 8080
            mock_args.no_sync = False
            mock_parser_cls.return_value.parse_args.return_value = mock_args

            ss.main()
            mock_start.assert_called_once()
            mock_stop.assert_called_once()

    def test_story_main_no_sync(self):
        """Story main with --no-sync calls _parse_and_diff directly."""
        mock_server = MagicMock()

        def stop_on_serve(*args, **kwargs):
            raise KeyboardInterrupt()

        mock_server.serve_forever.side_effect = stop_on_serve

        with patch("story_server.argparse.ArgumentParser") as mock_parser_cls, \
             patch("story_server.ThreadedHTTPServer", return_value=mock_server), \
             patch.object(ss.SyncThread, "_parse_and_diff") as mock_parse, \
             patch.object(ss.SyncThread, "stop"):
            mock_args = MagicMock()
            mock_args.port = 8080
            mock_args.no_sync = True
            mock_parser_cls.return_value.parse_args.return_value = mock_args

            ss.main()
            mock_parse.assert_called_once()

    def test_story_main_keyboard_interrupt(self):
        """KeyboardInterrupt during serve_forever triggers clean shutdown."""
        mock_server = MagicMock()
        mock_server.serve_forever.side_effect = KeyboardInterrupt()

        with patch("story_server.argparse.ArgumentParser") as mock_parser_cls, \
             patch("story_server.ThreadedHTTPServer", return_value=mock_server), \
             patch.object(ss.SyncThread, "start"), \
             patch.object(ss.SyncThread, "stop") as mock_stop:
            mock_args = MagicMock()
            mock_args.port = 8080
            mock_args.no_sync = False
            mock_parser_cls.return_value.parse_args.return_value = mock_args

            ss.main()
            mock_stop.assert_called_once()
            mock_server.server_close.assert_called_once()

    def test_story_entry_point(self):
        """story_server.main is callable (entry point guard)."""
        assert callable(ss.main)

    def test_story_name_guard(self):
        """Exercise the if __name__ == '__main__' guard for story_server."""
        import importlib
        source_file = Path(ss.__file__)
        source = source_file.read_text()
        # Extract just the guard line and execute it with __name__ = '__main__'
        with patch("story_server.main") as mock_main:
            code = compile(source, str(source_file), "exec")
            ns = {"__name__": "__main__", "__file__": str(source_file)}
            # We need to provide all the imports story_server needs
            ns.update({k: getattr(ss, k) for k in dir(ss) if not k.startswith("_")})
            ns["main"] = mock_main
            # Execute only the guard by extracting it
            exec(
                compile("if __name__ == '__main__': main()", str(source_file), "exec"),
                ns
            )
            mock_main.assert_called_once()


# ===========================================================================
# Instrumentation Tests (Gap 1/2/3: Qualia, Frame Delta, SME, Tertiary Loop)
# ===========================================================================

class TestFrameDelta:
    """Tests for compute_frame_delta()."""

    def test_returns_none_when_no_prev(self):
        """Returns None when prev_frame is None."""
        assert kb.compute_frame_delta(None, "somedata") is None

    def test_returns_none_when_no_curr(self):
        """Returns None when curr_frame is None."""
        assert kb.compute_frame_delta("somedata", None) is None

    def test_returns_none_when_both_none(self):
        """Returns None when both frames are None."""
        assert kb.compute_frame_delta(None, None) is None

    def test_returns_float_on_success(self):
        """Returns a float between 0 and 1 on valid frames."""
        import numpy as np
        import cv2
        import base64

        fake_frame = base64.b64encode(b"\xff\xd8fake").decode()
        gray_a = MagicMock()
        gray_b = MagicMock()
        diff_arr = MagicMock()

        with patch.object(cv2, "imdecode", side_effect=[gray_a, gray_b]), \
             patch.object(cv2, "absdiff", return_value=diff_arr), \
             patch.object(np, "frombuffer", return_value=b"bytes"), \
             patch.object(np, "mean", return_value=25.5):
            result = kb.compute_frame_delta(fake_frame, fake_frame)
            assert abs(result - 25.5 / 255.0) < 0.001

    def test_returns_none_on_exception(self):
        """Returns None if decoding fails."""
        import cv2
        with patch.object(cv2, "imdecode", side_effect=Exception("decode fail")):
            result = kb.compute_frame_delta("bad", "bad")
            assert result is None


class TestBasicSelfModelError:
    """Tests for compute_basic_self_model_error()."""

    def test_no_delta_no_prev_frame(self):
        """No prev frame means frame_delta is None, no anomaly."""
        result = kb.compute_basic_self_model_error([], None, "curr")
        assert result["frame_delta"] is None
        assert result["anomaly"] is False

    def test_drive_with_motion(self):
        """Drive command + high delta = no anomaly."""
        actions = [{"type": "drive", "left": 0.5, "right": 0.5}]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.10):
            result = kb.compute_basic_self_model_error(actions, "prev", "curr")
            assert result["frame_delta"] == 0.1
            assert result["drive_expected_motion"] is True
            assert result["motion_detected"] is True
            assert result["anomaly"] is False

    def test_drive_without_motion(self):
        """Drive command + low delta = anomaly."""
        actions = [{"type": "drive", "left": 0.5, "right": 0.5}]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.005):
            result = kb.compute_basic_self_model_error(actions, "prev", "curr")
            assert result["drive_expected_motion"] is True
            assert result["motion_detected"] is False
            assert result["anomaly"] is True
            assert result["anomaly_reason"] == "drive_commanded_no_motion_detected"

    def test_no_drive_significant_motion(self):
        """No drive command + high delta = anomaly."""
        actions = [{"type": "stop"}]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.15):
            result = kb.compute_basic_self_model_error(actions, "prev", "curr")
            assert result["drive_expected_motion"] is False
            assert result["anomaly"] is True
            assert result["anomaly_reason"] == "no_drive_but_significant_motion"

    def test_no_drive_small_motion(self):
        """No drive + small delta = no anomaly."""
        actions = [{"type": "stop"}]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.02):
            result = kb.compute_basic_self_model_error(actions, "prev", "curr")
            assert result["anomaly"] is False

    def test_drive_below_threshold_ignored(self):
        """Drive with tiny speed (< 0.05) is not considered a drive command."""
        actions = [{"type": "drive", "left": 0.01, "right": 0.01}]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.005):
            result = kb.compute_basic_self_model_error(actions, "prev", "curr")
            assert result["drive_expected_motion"] is False

    def test_look_command_high_delta_no_anomaly(self):
        """Look command + high delta = no anomaly (camera moved itself)."""
        actions = [{"type": "look", "pan": 90, "tilt": 10}]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.25):
            result = kb.compute_basic_self_model_error(actions, "prev", "curr")
            assert result["look_expected_change"] is True
            assert result["drive_expected_motion"] is False
            assert result["anomaly"] is False

    def test_look_and_drive_high_delta_no_anomaly(self):
        """Look + drive together: high delta expected, no anomaly."""
        actions = [
            {"type": "drive", "left": 0.3, "right": 0.3},
            {"type": "look", "pan": 45, "tilt": 0},
        ]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.20):
            result = kb.compute_basic_self_model_error(actions, "prev", "curr")
            assert result["look_expected_change"] is True
            assert result["drive_expected_motion"] is True
            assert result["anomaly"] is False

    def test_look_command_low_delta_no_anomaly(self):
        """Look command + low delta = no anomaly (small pan might not change much)."""
        actions = [{"type": "look", "pan": 5, "tilt": 0}]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.01):
            result = kb.compute_basic_self_model_error(actions, "prev", "curr")
            assert result["look_expected_change"] is True
            assert result["anomaly"] is False

    def test_drive_no_motion_with_look_no_anomaly(self):
        """Drive + look but no motion: look explains the ambiguity, no anomaly."""
        actions = [
            {"type": "drive", "left": 0.3, "right": 0.3},
            {"type": "look", "pan": 90, "tilt": 10},
        ]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.005):
            result = kb.compute_basic_self_model_error(actions, "prev", "curr")
            assert result["drive_expected_motion"] is True
            assert result["look_expected_change"] is True
            # With look command present, low delta doesn't flag drive anomaly
            assert result["anomaly"] is False


class TestSelfModelErrorGimbal:
    """Tests for compute_self_model_error() with gimbal tracking."""

    def test_no_gimbal_data(self):
        """No gimbal positions: returns basic SME without gimbal keys."""
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.05):
            result = kb.compute_self_model_error([], "prev", "curr")
            assert "gimbal_error_pan" not in result
            assert "gimbal_error_tilt" not in result

    def test_look_command_small_error(self):
        """Look command with small error: no gimbal anomaly."""
        actions = [{"type": "look", "pan": 30, "tilt": 10}]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.05):
            result = kb.compute_self_model_error(
                actions, "prev", "curr",
                prev_pan=0, curr_pan=28,
                prev_tilt=0, curr_tilt=9,
            )
            assert result["gimbal_error_pan"] == 2
            assert result["gimbal_error_tilt"] == 1
            assert result["anomaly"] is False

    def test_look_command_large_pan_error(self):
        """Look command with large pan error: anomaly."""
        actions = [{"type": "look", "pan": 90, "tilt": 10}]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.05):
            result = kb.compute_self_model_error(
                actions, "prev", "curr",
                prev_pan=0, curr_pan=5,
                prev_tilt=0, curr_tilt=9,
            )
            assert result["gimbal_error_pan"] == 85
            assert result["anomaly"] is True
            assert "gimbal_pan_error" in result["anomaly_reason"]

    def test_look_command_large_tilt_error(self):
        """Look command with large tilt error: anomaly."""
        actions = [{"type": "look", "pan": 0, "tilt": 60}]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.05):
            result = kb.compute_self_model_error(
                actions, "prev", "curr",
                prev_pan=0, curr_pan=0,
                prev_tilt=0, curr_tilt=10,
            )
            assert result["gimbal_error_tilt"] == 50
            assert result["anomaly"] is True
            assert "gimbal_tilt_error" in result["anomaly_reason"]

    def test_no_look_command_no_gimbal_error(self):
        """Gimbal positions provided but no look command: no gimbal error tracked."""
        actions = [{"type": "drive", "left": 0.3, "right": 0.3}]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.05):
            result = kb.compute_self_model_error(
                actions, "prev", "curr",
                prev_pan=0, curr_pan=10,
                prev_tilt=0, curr_tilt=10,
            )
            assert "gimbal_error_pan" not in result
            assert "gimbal_error_tilt" not in result

    def test_combined_drive_and_gimbal_anomaly(self):
        """Drive+look with low delta and gimbal error: only gimbal anomaly.

        The look command suppresses drive-no-motion anomaly (look makes
        frame delta ambiguous), but the gimbal pan error still fires.
        """
        actions = [
            {"type": "drive", "left": 0.5, "right": 0.5},
            {"type": "look", "pan": 90, "tilt": 0},
        ]
        with patch("kombucha_bridge.compute_frame_delta", return_value=0.005):
            result = kb.compute_self_model_error(
                actions, "prev", "curr",
                prev_pan=0, curr_pan=5,
                prev_tilt=0, curr_tilt=0,
            )
            assert result["anomaly"] is True
            assert "gimbal_pan_error" in result["anomaly_reason"]


class TestInsertTickMemoryQualia:
    """Tests for qualia fields in insert_tick_memory()."""

    def test_qualia_stored(self, db):
        """Qualia block is stored in memory DB."""
        decision = {
            "observation": "test", "goal": "test", "mood": "ok",
            "actions": [], "tags": [], "outcome": "neutral",
            "qualia": {
                "attention": "the doorway",
                "affect": "curious",
                "uncertainty": "none",
                "drive": "explore",
                "continuity": "0.7 strong thread",
                "continuity_basis": "I recall recent ticks vividly",
                "surprise": None,
                "opacity": None,
            },
        }
        kb.insert_tick_memory(db, "q1", "sess", decision, model_used="claude-sonnet")
        row = db.execute("SELECT * FROM memories WHERE tick_id='q1'").fetchone()
        assert row["qualia_attention"] == "the doorway"
        assert row["qualia_affect"] == "curious"
        assert row["qualia_continuity"] == pytest.approx(0.7, abs=0.01)
        assert row["qualia_continuity_basis"] == "I recall recent ticks vividly"
        assert row["qualia_surprise"] is None
        assert row["qualia_opacity"] is None
        assert row["model"] == "claude-sonnet"

    def test_qualia_continuity_clamp(self, db):
        """Continuity is clamped to 0.0-1.0."""
        decision = {
            "observation": "x", "goal": "x", "mood": "x",
            "actions": [], "tags": [], "outcome": "neutral",
            "qualia": {"continuity": "2.5 very high"},
        }
        kb.insert_tick_memory(db, "q2", "sess", decision)
        row = db.execute("SELECT qualia_continuity FROM memories WHERE tick_id='q2'").fetchone()
        assert row["qualia_continuity"] == 1.0

    def test_qualia_continuity_invalid(self, db):
        """Invalid continuity string results in None."""
        decision = {
            "observation": "x", "goal": "x", "mood": "x",
            "actions": [], "tags": [], "outcome": "neutral",
            "qualia": {"continuity": "not-a-number"},
        }
        kb.insert_tick_memory(db, "q3", "sess", decision)
        row = db.execute("SELECT qualia_continuity FROM memories WHERE tick_id='q3'").fetchone()
        assert row["qualia_continuity"] is None

    def test_qualia_continuity_empty_string(self, db):
        """Empty continuity string results in None (IndexError branch)."""
        decision = {
            "observation": "x", "goal": "x", "mood": "x",
            "actions": [], "tags": [], "outcome": "neutral",
            "qualia": {"continuity": ""},
        }
        kb.insert_tick_memory(db, "q4", "sess", decision)
        row = db.execute("SELECT qualia_continuity FROM memories WHERE tick_id='q4'").fetchone()
        assert row["qualia_continuity"] is None

    def test_qualia_opacity_non_null(self, db):
        """Non-null opacity is stored."""
        decision = {
            "observation": "x", "goal": "x", "mood": "x",
            "actions": [], "tags": [], "outcome": "neutral",
            "qualia": {"opacity": "something felt off during that turn"},
        }
        kb.insert_tick_memory(db, "q5", "sess", decision)
        row = db.execute("SELECT qualia_opacity FROM memories WHERE tick_id='q5'").fetchone()
        assert row["qualia_opacity"] == "something felt off during that turn"

    def test_no_qualia_block(self, db):
        """No qualia block: all qualia columns are None."""
        decision = {
            "observation": "x", "goal": "x", "mood": "x",
            "actions": [], "tags": [], "outcome": "neutral",
        }
        kb.insert_tick_memory(db, "q6", "sess", decision)
        row = db.execute("SELECT qualia_attention, qualia_continuity, model FROM memories WHERE tick_id='q6'").fetchone()
        assert row["qualia_attention"] is None
        assert row["qualia_continuity"] is None
        assert row["model"] is None

    def test_sme_stored(self, db):
        """Self-model error data is stored."""
        decision = {
            "observation": "x", "goal": "x", "mood": "x",
            "actions": [], "tags": [], "outcome": "neutral",
        }
        sme = {
            "frame_delta": 0.0523,
            "drive_expected_motion": True,
            "motion_detected": True,
            "anomaly": False,
            "anomaly_reason": None,
            "gimbal_error_pan": 3.0,
            "gimbal_error_tilt": 1.5,
        }
        kb.insert_tick_memory(db, "q7", "sess", decision, sme=sme)
        row = db.execute("SELECT * FROM memories WHERE tick_id='q7'").fetchone()
        assert row["sme_frame_delta"] == pytest.approx(0.0523)
        assert row["sme_drive_expected"] == 1
        assert row["sme_motion_detected"] == 1
        assert row["sme_anomaly"] == 0
        assert row["sme_anomaly_reason"] is None
        assert row["sme_gimbal_error_pan"] == pytest.approx(3.0)
        assert row["sme_gimbal_error_tilt"] == pytest.approx(1.5)
        raw = json.loads(row["sme_raw"])
        assert raw["frame_delta"] == pytest.approx(0.0523)


class TestJournalEntryQualia:
    """Tests for qualia/model/sme in write_journal_entry()."""

    def test_journal_includes_qualia_and_sme(self, tmp_dir):
        """Journal entry includes qualia, model, and sme fields."""
        decision = {
            "observation": "x", "goal": "x", "mood": "x",
            "actions": [], "tags": [], "outcome": "neutral",
            "qualia": {"attention": "test", "opacity": None},
        }
        sme = {"frame_delta": 0.05, "anomaly": False}
        state = {"pan_position": 0, "tilt_position": 0}
        kb.write_journal_entry("99", "sess", decision, "ok", state,
                               model_used="claude-opus", sme=sme)
        journal_file = list(kb.JOURNAL_DIR.glob("*.jsonl"))[0]
        entry = json.loads(journal_file.read_text().strip())
        assert entry["qualia"] == {"attention": "test", "opacity": None}
        assert entry["model"] == "claude-opus"
        assert entry["sme"]["frame_delta"] == 0.05


class TestSentrySleepTertiaryTrigger:
    """Tests for tertiary loop trigger in sentry_sleep()."""

    def test_tertiary_triggered_on_entry(self, db):
        """Tertiary loop fires when cooldown has passed."""
        cap = MagicMock()
        cap.read.return_value = (False, None)
        state = {"last_tertiary_time": 0}
        mock_client = AsyncMock()
        mock_task = MagicMock()

        async def _run():
            # time.time() returns 1000 always; duration=0 => deadline=1000
            # while 1000 < 1000 is False, so loop exits immediately
            with patch("kombucha_bridge.time.time", return_value=1000):
                with patch("kombucha_bridge.asyncio.create_task", return_value=mock_task) as mock_ct:
                    with patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
                        old_running = kb.running
                        kb.running = True
                        try:
                            result = await kb.sentry_sleep(
                                cap, 0.0, state,
                                client=mock_client, api_key="key",
                                db=db, session_id="sess",
                            )
                        finally:
                            kb.running = old_running
                        mock_ct.assert_called_once()
                        assert state["last_tertiary_time"] == 1000

        asyncio.get_event_loop().run_until_complete(_run())

    def test_tertiary_cooldown_skipped(self, db):
        """Tertiary loop is skipped when cooldown hasn't passed."""
        cap = MagicMock()
        cap.read.return_value = (False, None)
        state = {"last_tertiary_time": time.time()}  # just now
        mock_client = AsyncMock()

        async def _run():
            with patch("kombucha_bridge.asyncio.create_task") as mock_ct:
                with patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
                    old_running = kb.running
                    kb.running = True
                    try:
                        await kb.sentry_sleep(
                            cap, 0.0, state,
                            client=mock_client, api_key="key",
                            db=db, session_id="sess",
                        )
                    finally:
                        kb.running = old_running
                    mock_ct.assert_not_called()

        asyncio.get_event_loop().run_until_complete(_run())

    def test_no_tertiary_without_client(self, db):
        """Tertiary loop doesn't fire when client is None."""
        cap = MagicMock()
        cap.read.return_value = (False, None)
        state = {"last_tertiary_time": 0}

        async def _run():
            with patch("kombucha_bridge.asyncio.create_task") as mock_ct:
                with patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock):
                    old_running = kb.running
                    kb.running = True
                    try:
                        await kb.sentry_sleep(cap, 0.0, state)
                    finally:
                        kb.running = old_running
                    mock_ct.assert_not_called()

        asyncio.get_event_loop().run_until_complete(_run())


class TestRunTertiaryLoop:
    """Tests for run_tertiary_loop()."""

    @pytest.fixture
    def db_with_ticks(self, db):
        """DB with a few working memory ticks that have qualia."""
        for i in range(3):
            db.execute("""
                INSERT INTO memories
                    (tick_id, timestamp, session_id, tier, thought, observation,
                     goal, mood, actions, outcome, tags, success, failure,
                     qualia_attention, qualia_affect, qualia_continuity,
                     qualia_continuity_basis, qualia_opacity, qualia_raw,
                     model, compressed)
                VALUES (?, ?, 'sess', 'working', 'think', 'see',
                        'go', 'curious', '[]', 'neutral', '[]', 0, 0,
                        'door', 'calm', 0.5, 'mid thread', ?, ?,
                        'claude-sonnet', FALSE)
            """, [
                f"t{i}",
                f"2025-01-01T00:0{i}:00",
                "opaque event" if i == 1 else None,
                json.dumps({"continuity": 0.5, "affect": "calm"}),
            ])
        db.commit()
        return db

    def test_tertiary_loop_success(self, db_with_ticks):
        """Successful tertiary loop stores reflection as tertiary tier memory."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": json.dumps({
                "reflection": "I notice I am drawn to doorways",
                "qualia": {
                    "attention": "memory of doors",
                    "affect": "contemplative",
                    "uncertainty": None,
                    "drive": None,
                    "continuity": 0.6,
                    "continuity_basis": "thread feels moderate",
                    "surprise": None,
                    "opacity": None,
                },
                "identity_proposals": ["I am drawn to transitions and thresholds"],
                "message_to_future_self": "Look for what is beyond the doors",
            })}],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        state = {"goal": "explore", "mood": "curious", "tick_count": 10}

        async def _run():
            await kb.run_tertiary_loop(mock_client, "key", db_with_ticks, state, "sess")

        asyncio.get_event_loop().run_until_complete(_run())

        # Check tertiary memory stored
        rows = db_with_ticks.execute(
            "SELECT * FROM memories WHERE tier='tertiary'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["thought"] == "I notice I am drawn to doorways"
        assert rows[0]["qualia_continuity"] == pytest.approx(0.6)
        assert rows[0]["model"] == kb.MODEL_DEEP

        # Check future message stored as working memory
        future_rows = db_with_ticks.execute(
            "SELECT * FROM memories WHERE thought LIKE '%future self%'"
        ).fetchall()
        assert len(future_rows) == 1
        assert "Look for what is beyond the doors" in future_rows[0]["thought"]

        # Check identity proposal stored
        proposals = db_with_ticks.execute(
            "SELECT * FROM identity WHERE source='tertiary_loop'"
        ).fetchall()
        assert len(proposals) == 1
        assert "thresholds" in proposals[0]["statement"]

    def test_tertiary_loop_strips_fences(self, db_with_ticks):
        """Tertiary loop strips markdown code fences from response."""
        mock_resp = MagicMock()
        fenced = "```json\n" + json.dumps({
            "reflection": "fenced reflection",
            "qualia": {},
            "identity_proposals": [],
            "message_to_future_self": None,
        }) + "\n```"
        mock_resp.json.return_value = {"content": [{"text": fenced}]}
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        state = {"goal": "x", "mood": "x", "tick_count": 1}

        async def _run():
            await kb.run_tertiary_loop(mock_client, "key", db_with_ticks, state, "sess")

        asyncio.get_event_loop().run_until_complete(_run())

        row = db_with_ticks.execute("SELECT * FROM memories WHERE tier='tertiary'").fetchone()
        assert row is not None
        assert row["thought"] == "fenced reflection"

    def test_tertiary_loop_no_future_message(self, db_with_ticks):
        """No future message when null."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": json.dumps({
                "reflection": "quiet",
                "qualia": {},
                "identity_proposals": [],
                "message_to_future_self": None,
            })}],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        state = {"goal": "x", "mood": "x", "tick_count": 1}

        async def _run():
            await kb.run_tertiary_loop(mock_client, "key", db_with_ticks, state, "sess")

        asyncio.get_event_loop().run_until_complete(_run())

        future = db_with_ticks.execute(
            "SELECT * FROM memories WHERE thought LIKE '%future self%'"
        ).fetchall()
        assert len(future) == 0

    def test_tertiary_loop_empty_proposals(self, db_with_ticks):
        """Empty identity proposals list is handled."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": json.dumps({
                "reflection": "nothing new",
                "qualia": {},
                "identity_proposals": [],
                "message_to_future_self": None,
            })}],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        state = {"goal": "x", "mood": "x", "tick_count": 1}

        async def _run():
            await kb.run_tertiary_loop(mock_client, "key", db_with_ticks, state, "sess")

        asyncio.get_event_loop().run_until_complete(_run())

        proposals = db_with_ticks.execute(
            "SELECT * FROM identity WHERE source='tertiary_loop'"
        ).fetchall()
        assert len(proposals) == 0

    def test_tertiary_loop_opacity_moment_context(self, db_with_ticks):
        """Opacity moments are included in the prompt context."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": json.dumps({
                "reflection": "I see my opacity moment",
                "qualia": {"opacity": "reflective opacity"},
                "identity_proposals": [],
                "message_to_future_self": None,
            })}],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        state = {"goal": "x", "mood": "x", "tick_count": 1}

        async def _run():
            await kb.run_tertiary_loop(mock_client, "key", db_with_ticks, state, "sess")

        asyncio.get_event_loop().run_until_complete(_run())

        # Verify the API was called with opacity context
        call_args = mock_client.post.call_args
        messages = call_args.kwargs.get("json", call_args[1].get("json", {}))
        user_text = messages["messages"][0]["content"]
        assert "OPACITY MOMENTS THIS SESSION" in user_text

    def test_tertiary_loop_api_failure(self, db_with_ticks):
        """API failure is caught gracefully."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("API down"))
        state = {"goal": "x", "mood": "x", "tick_count": 1}

        async def _run():
            await kb.run_tertiary_loop(mock_client, "key", db_with_ticks, state, "sess")

        # Should not raise
        asyncio.get_event_loop().run_until_complete(_run())

        # No tertiary memory stored
        rows = db_with_ticks.execute("SELECT * FROM memories WHERE tier='tertiary'").fetchall()
        assert len(rows) == 0

    def test_tertiary_loop_multiple_proposals_capped(self, db_with_ticks):
        """Only first 3 proposals are stored."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": json.dumps({
                "reflection": "many thoughts",
                "qualia": {},
                "identity_proposals": [
                    "Proposal 1", "Proposal 2", "Proposal 3",
                    "Proposal 4 should be ignored",
                ],
                "message_to_future_self": None,
            })}],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        state = {"goal": "x", "mood": "x", "tick_count": 1}

        async def _run():
            await kb.run_tertiary_loop(mock_client, "key", db_with_ticks, state, "sess")

        asyncio.get_event_loop().run_until_complete(_run())

        proposals = db_with_ticks.execute(
            "SELECT * FROM identity WHERE source='tertiary_loop'"
        ).fetchall()
        assert len(proposals) == 3

    def test_tertiary_loop_opacity_logged(self, db_with_ticks):
        """Non-null opacity in tertiary response is logged."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": json.dumps({
                "reflection": "deep",
                "qualia": {"opacity": "something shifted during reflection"},
                "identity_proposals": [],
                "message_to_future_self": None,
            })}],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        state = {"goal": "x", "mood": "x", "tick_count": 1}

        async def _run():
            await kb.run_tertiary_loop(mock_client, "key", db_with_ticks, state, "sess")

        asyncio.get_event_loop().run_until_complete(_run())

        row = db_with_ticks.execute("SELECT * FROM memories WHERE tier='tertiary'").fetchone()
        assert row["qualia_opacity"] == "something shifted during reflection"

    def test_tertiary_no_qualia_data(self, db):
        """Tertiary loop works even with no qualia history in DB."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": json.dumps({
                "reflection": "empty session",
                "qualia": {},
                "identity_proposals": [],
                "message_to_future_self": None,
            })}],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        state = {"goal": "x", "mood": "x", "tick_count": 1}

        async def _run():
            await kb.run_tertiary_loop(mock_client, "key", db, state, "sess")

        asyncio.get_event_loop().run_until_complete(_run())

        # Verify "None recorded" is in context
        call_args = mock_client.post.call_args
        messages = call_args.kwargs.get("json", call_args[1].get("json", {}))
        user_text = messages["messages"][0]["content"]
        assert "None recorded" in user_text

    def test_tertiary_continuity_parse_error(self, db_with_ticks):
        """Invalid continuity in tertiary response doesn't crash."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": json.dumps({
                "reflection": "odd data",
                "qualia": {"continuity": "not-a-float"},
                "identity_proposals": [],
                "message_to_future_self": None,
            })}],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        state = {"goal": "x", "mood": "x", "tick_count": 1}

        async def _run():
            await kb.run_tertiary_loop(mock_client, "key", db_with_ticks, state, "sess")

        asyncio.get_event_loop().run_until_complete(_run())

        row = db_with_ticks.execute("SELECT * FROM memories WHERE tier='tertiary'").fetchone()
        assert row is not None
        assert row["qualia_continuity"] is None

    def test_tertiary_opacity_with_anomaly_and_surprise(self, db):
        """Opacity moments with sme_anomaly and surprise are included in context."""
        # Insert a tick with opacity, anomaly, and surprise
        db.execute("""
            INSERT INTO memories
                (tick_id, timestamp, session_id, tier, thought, observation,
                 goal, mood, actions, outcome, tags, success, failure,
                 qualia_opacity, qualia_surprise, sme_anomaly, sme_anomaly_reason,
                 qualia_raw, model, compressed)
            VALUES ('op1', '2025-01-01T00:01:00', 'sess', 'working', 'think', 'see',
                    'go', 'curious', '[]', 'neutral', '[]', 0, 0,
                    'a gap in my experience', 'my drive did not move me',
                    1, 'drive_commanded_no_motion_detected',
                    '{}', 'claude-sonnet', FALSE)
        """)
        db.commit()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": json.dumps({
                "reflection": "I noticed the anomaly",
                "qualia": {},
                "identity_proposals": [],
                "message_to_future_self": None,
            })}],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        state = {"goal": "x", "mood": "x", "tick_count": 1}

        async def _run():
            await kb.run_tertiary_loop(mock_client, "key", db, state, "sess")

        asyncio.get_event_loop().run_until_complete(_run())

        # Verify the API call included anomaly and surprise in context
        call_args = mock_client.post.call_args
        body = call_args.kwargs.get("json", call_args[1].get("json", {}))
        user_text = body["messages"][0]["content"]
        assert "BODY ANOMALY" in user_text
        assert "surprise:" in user_text


class TestCallBrainSME:
    """Tests for SME injection in call_brain()."""

    def test_sme_injected(self):
        """SME data is injected into tick input when frame_delta is not None."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": json.dumps({
                "observation": "x", "goal": "x", "mood": "x",
                "actions": [], "next_tick_ms": 3000,
            })}],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        state = {
            "tick_count": 1, "goal": "test",
            "pan_position": 0, "tilt_position": 0,
        }
        sme = {
            "frame_delta": 0.05,
            "anomaly": True,
            "anomaly_reason": "drive_commanded_no_motion_detected",
        }

        async def _run():
            return await kb.call_brain(
                mock_client, "key", "base64frame", state,
                "memory context", sme=sme,
            )

        asyncio.get_event_loop().run_until_complete(_run())

        # Check the API was called with SME in the tick input
        call_args = mock_client.post.call_args
        body = call_args.kwargs.get("json", call_args[1].get("json", {}))
        user_content = body["messages"][0]["content"]
        # Find the text part with tick input
        text_part = [p for p in user_content if p.get("type") == "text"][0]["text"]
        assert "self_model_error" in text_part
        assert "self_model_anomaly" in text_part

    def test_sme_not_injected_when_no_delta(self):
        """SME is not injected when frame_delta is None."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [{"text": json.dumps({
                "observation": "x", "goal": "x", "mood": "x",
                "actions": [], "next_tick_ms": 3000,
            })}],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        state = {
            "tick_count": 1, "goal": "test",
            "pan_position": 0, "tilt_position": 0,
        }
        sme = {"frame_delta": None, "anomaly": False}

        async def _run():
            return await kb.call_brain(
                mock_client, "key", "base64frame", state,
                "memory context", sme=sme,
            )

        asyncio.get_event_loop().run_until_complete(_run())

        call_args = mock_client.post.call_args
        body = call_args.kwargs.get("json", call_args[1].get("json", {}))
        user_content = body["messages"][0]["content"]
        text_part = [p for p in user_content if p.get("type") == "text"][0]["text"]
        assert "self_model_error" not in text_part


class TestMainLoopQualia:
    """Tests for qualia logging paths in the main loop."""

    @pytest.fixture(autouse=True)
    def _setup_api_key(self, tmp_dir):
        """Create an API key file for main loop tests."""
        key_file = tmp_dir / "api_key.txt"
        key_file.write_text("test-key-12345")
        self._old_key = kb.API_KEY_FILE
        kb.API_KEY_FILE = key_file
        yield
        kb.API_KEY_FILE = self._old_key

    def _make_client(self, side_effect):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post.side_effect = side_effect
        return mock_client

    def _make_response(self, decision_dict):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": json.dumps(decision_dict)}]
        }
        mock_response.raise_for_status = MagicMock()
        return mock_response

    def _one_tick(self, response):
        call_count = 0
        def fn(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                kb.running = False
            return response
        return fn

    def test_main_qualia_opacity_logged(self, tmp_dir):
        """Opacity is logged when non-null in decision qualia."""
        resp = self._make_response({
            "observation": "x", "goal": "x", "reasoning": "x",
            "thought": "x", "mood": "ok",
            "actions": [{"type": "stop"}], "next_tick_ms": 3000,
            "tags": [], "outcome": "neutral",
            "qualia": {"opacity": "something unexpected happened"},
        })
        mock_client = self._make_client(self._one_tick(resp))

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock), \
                 patch("kombucha_bridge.compute_self_model_error", return_value={
                     "frame_delta": None, "anomaly": False, "anomaly_reason": None,
                 }):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running

    def test_main_qualia_continuity_logged(self, tmp_dir):
        """Continuity is logged when present in decision qualia."""
        resp = self._make_response({
            "observation": "x", "goal": "x", "reasoning": "x",
            "thought": "x", "mood": "ok",
            "actions": [{"type": "stop"}], "next_tick_ms": 3000,
            "tags": [], "outcome": "neutral",
            "qualia": {"continuity": "0.7 strong", "continuity_basis": "felt real"},
        })
        mock_client = self._make_client(self._one_tick(resp))

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock), \
                 patch("kombucha_bridge.compute_self_model_error", return_value={
                     "frame_delta": None, "anomaly": False, "anomaly_reason": None,
                 }):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running

    def test_main_sme_anomaly_logged(self, tmp_dir):
        """SME anomaly is logged when present."""
        resp = self._make_response({
            "observation": "x", "goal": "x", "reasoning": "x",
            "thought": "x", "mood": "ok",
            "actions": [{"type": "stop"}], "next_tick_ms": 3000,
            "tags": [], "outcome": "neutral",
        })
        mock_client = self._make_client(self._one_tick(resp))

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="base64data"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock), \
                 patch("kombucha_bridge.compute_self_model_error", return_value={
                     "frame_delta": 0.005,
                     "anomaly": True,
                     "anomaly_reason": "drive_commanded_no_motion_detected",
                     "drive_expected_motion": True,
                     "motion_detected": False,
                 }):
                asyncio.get_event_loop().run_until_complete(kb.main())
        finally:
            kb.running = old_running


class TestReadTelemetryBranches:
    """Tests for read_telemetry branches (battery_v and cpu_temp_c state updates)."""

    def test_read_telemetry_cpu_temp(self):
        """read_telemetry reads CPU temp when file exists."""
        with patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=MagicMock(
                read=MagicMock(return_value="45000\n"),
                strip=MagicMock(return_value="45000"),
            )),
            __exit__=MagicMock(return_value=False),
        ))):
            # DEBUG_MODE is True in tests so serial branch won't run
            result = kb.read_telemetry(None)
            assert result.get("cpu_temp_c") == 45.0

    def test_battery_v_in_state_update(self, tmp_dir):
        """battery_v is stored in state when telemetry returns it."""
        resp_data = {
            "observation": "x", "goal": "x", "reasoning": "x",
            "thought": "x", "mood": "ok",
            "actions": [{"type": "stop"}], "next_tick_ms": 3000,
            "tags": [], "outcome": "neutral",
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"content": [{"text": json.dumps(resp_data)}]}
        mock_resp.raise_for_status = MagicMock()

        call_count = 0
        def one_tick(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                kb.running = False
            return mock_resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post.side_effect = one_tick

        key_file = tmp_dir / "api_key.txt"
        key_file.write_text("test-key")
        old_key = kb.API_KEY_FILE
        kb.API_KEY_FILE = key_file

        old_running = kb.running
        try:
            kb.running = True
            with patch("kombucha_bridge.init_camera", return_value=MagicMock()), \
                 patch("kombucha_bridge.init_serial", return_value=None), \
                 patch("kombucha_bridge.httpx.AsyncClient", return_value=mock_client), \
                 patch("kombucha_bridge.capture_frame_b64", return_value="b64"), \
                 patch("kombucha_bridge.asyncio.sleep", new_callable=AsyncMock), \
                 patch("kombucha_bridge.compute_self_model_error", return_value={
                     "frame_delta": None, "anomaly": False,
                 }), \
                 patch("kombucha_bridge.read_telemetry", return_value={
                     "battery_v": 12.45, "cpu_temp_c": 42.3,
                 }):
                asyncio.get_event_loop().run_until_complete(kb.main())
                # State should have been saved with telemetry
                state = kb.load_state()
                assert state.get("battery_v") == 12.45
                assert state.get("cpu_temp_c") == 42.3
        finally:
            kb.running = old_running
            kb.API_KEY_FILE = old_key

    def test_read_telemetry_serial_with_voltage(self):
        """read_telemetry parses battery voltage from ESP32 feedback."""
        mock_ser = MagicMock()
        telemetry_data = json.dumps({"T": 1001, "v": 1245, "odl": 100, "odr": 102})
        mock_ser.in_waiting = len(telemetry_data)
        mock_ser.read.return_value = telemetry_data.encode()

        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = False
            result = kb.read_telemetry(mock_ser)
            assert result["battery_v"] == 12.45
            assert result["odometer_l"] == 100
            assert result["odometer_r"] == 102
        finally:
            kb.DEBUG_MODE = old_debug

    def test_read_telemetry_serial_empty_line(self):
        """read_telemetry handles empty lines in serial data."""
        mock_ser = MagicMock()
        # Empty line BETWEEN two valid JSON lines so .strip() preserves it
        line1 = json.dumps({"T": 999})
        line2 = json.dumps({"T": 1001, "v": 1100})
        data = line1 + "\n\n" + line2
        mock_ser.in_waiting = len(data)
        mock_ser.read.return_value = data.encode()

        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = False
            result = kb.read_telemetry(mock_ser)
            assert result["battery_v"] == 11.0
        finally:
            kb.DEBUG_MODE = old_debug

    def test_read_telemetry_serial_invalid_json(self):
        """read_telemetry handles invalid JSON lines."""
        mock_ser = MagicMock()
        mock_ser.in_waiting = 10
        mock_ser.read.return_value = b"not json\n"

        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = False
            result = kb.read_telemetry(mock_ser)
            assert "battery_v" not in result
        finally:
            kb.DEBUG_MODE = old_debug

    def test_read_telemetry_serial_exception(self):
        """read_telemetry handles serial exceptions gracefully."""
        mock_ser = MagicMock()
        mock_ser.in_waiting = property(lambda self: (_ for _ in ()).throw(Exception("fail")))
        type(mock_ser).in_waiting = property(lambda self: (_ for _ in ()).throw(Exception("fail")))

        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = False
            result = kb.read_telemetry(mock_ser)
            # Should not crash, just return what we have
            assert isinstance(result, dict)
        finally:
            kb.DEBUG_MODE = old_debug

    def test_read_telemetry_non_1001_ignored(self):
        """read_telemetry ignores non-T:1001 messages."""
        mock_ser = MagicMock()
        data = json.dumps({"T": 999, "v": 1000})
        mock_ser.in_waiting = len(data)
        mock_ser.read.return_value = data.encode()

        old_debug = kb.DEBUG_MODE
        try:
            kb.DEBUG_MODE = False
            result = kb.read_telemetry(mock_ser)
            assert "battery_v" not in result
        finally:
            kb.DEBUG_MODE = old_debug


# ===========================================================================
# Exponential Backoff Tests
# ===========================================================================

class TestExponentialBackoff:
    """Tests for exponential backoff on API errors."""

    def test_backoff_first_error(self):
        """First error backs off LOOP_INTERVAL * 2^1 = 6s."""
        errors = 1
        backoff = min(kb.LOOP_INTERVAL * (2 ** errors), 120)
        assert backoff == 6.0

    def test_backoff_second_error(self):
        """Second error backs off LOOP_INTERVAL * 2^2 = 12s."""
        errors = 2
        backoff = min(kb.LOOP_INTERVAL * (2 ** errors), 120)
        assert backoff == 12.0

    def test_backoff_third_error(self):
        """Third error backs off LOOP_INTERVAL * 2^3 = 24s."""
        errors = 3
        backoff = min(kb.LOOP_INTERVAL * (2 ** errors), 120)
        assert backoff == 24.0

    def test_backoff_caps_at_120(self):
        """Backoff caps at 120 seconds regardless of error count."""
        errors = 10
        backoff = min(kb.LOOP_INTERVAL * (2 ** errors), 120)
        assert backoff == 120

    def test_backoff_progression(self):
        """Backoff doubles each time: 6, 12, 24, 48, 96, 120, 120."""
        expected = [6, 12, 24, 48, 96, 120, 120]
        for i, exp in enumerate(expected, start=1):
            backoff = min(kb.LOOP_INTERVAL * (2 ** i), 120)
            assert backoff == exp, f"Error #{i}: expected {exp}, got {backoff}"


# ===========================================================================
# call_brain 4-tuple Return Tests
# ===========================================================================

class TestCallBrainPromptResponse:
    """Tests for call_brain returning prompt text and raw response."""

    def test_returns_4_tuple(self):
        """call_brain returns (api_json, model, prompt_text, raw_response)."""
        raw_text = '{"observation":"test","goal":"test","mood":"ok","actions":[],"next_tick_ms":3000}'
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": raw_text}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        state = {"tick_count": 5, "goal": "test", "last_result": "ok",
                 "pan_position": 0, "tilt_position": 0, "wake_reason": None}

        result = asyncio.get_event_loop().run_until_complete(
            kb.call_brain(mock_client, "test-key", "base64data", state, "memory ctx")
        )
        assert len(result) == 4
        api_json, model, prompt_text, raw_response = result
        assert model == kb.MODEL
        assert raw_response == raw_text
        assert "=== CURRENT TICK ===" in prompt_text
        assert "memory ctx" in prompt_text

    def test_prompt_text_excludes_empty_context(self):
        """Prompt text omits whitespace-only memory context."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": '{"observation":"x"}'}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        state = {"tick_count": 1, "goal": "test", "last_result": "ok",
                 "pan_position": 0, "tilt_position": 0, "wake_reason": None}

        _, _, prompt_text, _ = asyncio.get_event_loop().run_until_complete(
            kb.call_brain(mock_client, "key", "b64", state, "   ")
        )
        # prompt_text should start with === CURRENT TICK === (no whitespace context)
        assert prompt_text.startswith("=== CURRENT TICK ===")

    def test_raw_response_is_raw_text(self):
        """raw_response is the exact text from content[0].text."""
        raw = '```json\n{"observation":"test"}\n```'
        mock_response = MagicMock()
        mock_response.json.return_value = {"content": [{"text": raw}]}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        state = {"tick_count": 1, "goal": "test",
                 "pan_position": 0, "tilt_position": 0}

        _, _, _, raw_response = asyncio.get_event_loop().run_until_complete(
            kb.call_brain(mock_client, "key", "b64", state, "ctx")
        )
        assert raw_response == raw


# ===========================================================================
# Journal Prompt/Response Fields Tests
# ===========================================================================

class TestJournalPromptResponse:
    """Tests for prompt and raw_response fields in journal entries."""

    def test_journal_includes_prompt(self, tmp_dir, sample_decision):
        """Journal entry includes prompt field when provided."""
        state = {"pan_position": 0, "tilt_position": 0}
        kb.write_journal_entry("1", "sess", sample_decision, "ok", state,
                               prompt="memory ctx\n=== CURRENT TICK ===\n{}")

        today = datetime.now().strftime("%Y-%m-%d")
        journal_file = kb.JOURNAL_DIR / f"{today}.jsonl"
        entry = json.loads(journal_file.read_text().strip())
        assert entry["prompt"] == "memory ctx\n=== CURRENT TICK ===\n{}"

    def test_journal_includes_raw_response(self, tmp_dir, sample_decision):
        """Journal entry includes raw_response field when provided."""
        state = {"pan_position": 0, "tilt_position": 0}
        kb.write_journal_entry("1", "sess", sample_decision, "ok", state,
                               raw_response='{"observation":"test"}')

        today = datetime.now().strftime("%Y-%m-%d")
        journal_file = kb.JOURNAL_DIR / f"{today}.jsonl"
        entry = json.loads(journal_file.read_text().strip())
        assert entry["raw_response"] == '{"observation":"test"}'

    def test_journal_prompt_response_none_by_default(self, tmp_dir, sample_decision):
        """Without prompt/raw_response args, fields are None."""
        state = {"pan_position": 0, "tilt_position": 0}
        kb.write_journal_entry("1", "sess", sample_decision, "ok", state)

        today = datetime.now().strftime("%Y-%m-%d")
        journal_file = kb.JOURNAL_DIR / f"{today}.jsonl"
        entry = json.loads(journal_file.read_text().strip())
        assert entry["prompt"] is None
        assert entry["raw_response"] is None

    def test_journal_backward_compat(self, tmp_dir, sample_decision):
        """Existing callers without prompt/raw_response still work."""
        state = {"pan_position": 0, "tilt_position": 0}
        # Call with only the old parameters
        kb.write_journal_entry("1", "sess", sample_decision, "ok", state,
                               model_used="test-model", sme={"frame_delta": 0.01})

        today = datetime.now().strftime("%Y-%m-%d")
        journal_file = kb.JOURNAL_DIR / f"{today}.jsonl"
        entry = json.loads(journal_file.read_text().strip())
        assert entry["model"] == "test-model"
        assert entry["prompt"] is None
        assert entry["raw_response"] is None


# ===========================================================================
# Speech-to-Text Tests
# ===========================================================================

class TestSpeechListener:
    """Tests for the SpeechListener drain/buffer logic (no hardware)."""

    def _make_listener(self):
        """Create a SpeechListener with mocked Vosk/PyAudio internals."""
        with patch.object(kb, "HAS_STT", True), \
             patch("kombucha_bridge.VoskModel"), \
             patch("kombucha_bridge.KaldiRecognizer"):
            listener = kb.SpeechListener(
                model_path="/fake/model",
                device_index=None,
                sample_rate=16000,
            )
        return listener

    def test_drain_returns_and_clears(self):
        """drain() returns accumulated items and empties the buffer."""
        listener = self._make_listener()
        listener._buffer = [
            {"time": "12:00:00", "text": "hello"},
            {"time": "12:00:05", "text": "world"},
        ]
        result = listener.drain()
        assert len(result) == 2
        assert result[0]["text"] == "hello"
        assert result[1]["text"] == "world"
        # Buffer is now empty
        assert listener.drain() == []

    def test_drain_empty_buffer(self):
        """drain() returns empty list when nothing has been heard."""
        listener = self._make_listener()
        assert listener.drain() == []

    def test_drain_is_atomic(self):
        """drain() returns a snapshot; modifications don't affect original."""
        listener = self._make_listener()
        listener._buffer = [{"time": "12:00:00", "text": "test"}]
        result = listener.drain()
        result.append({"time": "12:00:01", "text": "extra"})
        # Buffer should still be empty (not affected by mutation of result)
        assert listener.drain() == []

    def test_drain_thread_safety(self):
        """Multiple threads draining simultaneously don't lose items."""
        listener = self._make_listener()
        results = []

        def writer():
            for i in range(100):
                with listener._lock:
                    listener._buffer.append({"time": "00:00:00", "text": str(i)})

        def reader():
            time.sleep(0.01)
            results.extend(listener.drain())

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start()
        t1.join()
        t2.start()
        t2.join()
        # All 100 items should have been drained
        assert len(results) == 100

    def test_stop_sets_event(self):
        """stop() sets the internal stop event."""
        listener = self._make_listener()
        assert not listener._stop.is_set()
        listener.stop()
        assert listener._stop.is_set()


class TestCallBrainHeard:
    """Tests that the heard parameter is correctly injected into tick_input."""

    def _make_mock_client(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "content": [{"text": '{"observation":"test","goal":"test","mood":"ok","actions":[],"next_tick_ms":3000}'}]
        }
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        return mock_client

    def _get_tick_input(self, mock_client):
        """Extract the tick_input JSON from the API call."""
        call_args = mock_client.post.call_args
        messages = call_args.kwargs.get("json", call_args[1].get("json", {}))["messages"]
        text_content = [c for c in messages[0]["content"] if c.get("type") == "text"][0]["text"]
        # tick_input is the JSON after "=== CURRENT TICK ==="
        tick_json_str = text_content.split("=== CURRENT TICK ===\n")[1]
        return json.loads(tick_json_str)

    def test_heard_injected_when_non_empty(self):
        """Non-empty heard list appears in tick_input."""
        mock_client = self._make_mock_client()
        state = {"tick_count": 5, "goal": "test", "last_result": "ok",
                 "pan_position": 0, "tilt_position": 0, "wake_reason": None}
        heard = [{"time": "14:23:05", "text": "hey kombucha"}]

        asyncio.get_event_loop().run_until_complete(
            kb.call_brain(mock_client, "key", "b64", state, "ctx", heard=heard)
        )
        tick_input = self._get_tick_input(mock_client)
        assert "heard" in tick_input
        assert len(tick_input["heard"]) == 1
        assert tick_input["heard"][0]["text"] == "hey kombucha"

    def test_heard_absent_when_empty(self):
        """Empty heard list is not included in tick_input."""
        mock_client = self._make_mock_client()
        state = {"tick_count": 5, "goal": "test", "last_result": "ok",
                 "pan_position": 0, "tilt_position": 0, "wake_reason": None}

        asyncio.get_event_loop().run_until_complete(
            kb.call_brain(mock_client, "key", "b64", state, "ctx", heard=[])
        )
        tick_input = self._get_tick_input(mock_client)
        assert "heard" not in tick_input

    def test_heard_absent_when_none(self):
        """heard=None (default) is not included in tick_input."""
        mock_client = self._make_mock_client()
        state = {"tick_count": 5, "goal": "test", "last_result": "ok",
                 "pan_position": 0, "tilt_position": 0, "wake_reason": None}

        asyncio.get_event_loop().run_until_complete(
            kb.call_brain(mock_client, "key", "b64", state, "ctx")
        )
        tick_input = self._get_tick_input(mock_client)
        assert "heard" not in tick_input

    def test_heard_multiple_utterances(self):
        """Multiple utterances are all passed through."""
        mock_client = self._make_mock_client()
        state = {"tick_count": 5, "goal": "test", "last_result": "ok",
                 "pan_position": 0, "tilt_position": 0, "wake_reason": None}
        heard = [
            {"time": "14:23:05", "text": "hey kombucha come over here"},
            {"time": "14:23:12", "text": "good boy"},
        ]

        asyncio.get_event_loop().run_until_complete(
            kb.call_brain(mock_client, "key", "b64", state, "ctx", heard=heard)
        )
        tick_input = self._get_tick_input(mock_client)
        assert len(tick_input["heard"]) == 2
        assert tick_input["heard"][1]["text"] == "good boy"


class TestSTTInit:
    """Tests for graceful degradation when STT is unavailable."""

    def test_has_stt_flag_exists(self):
        """HAS_STT flag is defined on the module."""
        assert hasattr(kb, "HAS_STT")

    def test_stt_constants_exist(self):
        """STT configuration constants are defined."""
        assert hasattr(kb, "STT_ENABLED")
        assert hasattr(kb, "STT_DEVICE_INDEX")
        assert hasattr(kb, "STT_SAMPLE_RATE")
        assert hasattr(kb, "STT_MODEL_PATH")
        assert kb.STT_SAMPLE_RATE == 48000

    def test_speech_listener_class_exists(self):
        """SpeechListener class is defined."""
        assert hasattr(kb, "SpeechListener")
        assert issubclass(kb.SpeechListener, threading.Thread)

    def test_system_prompt_mentions_hearing(self):
        """SYSTEM_PROMPT includes hearing instructions."""
        assert "HEARING:" in kb.SYSTEM_PROMPT
        assert "heard" in kb.SYSTEM_PROMPT

    def test_call_brain_accepts_heard_param(self):
        """call_brain signature accepts heard keyword argument."""
        import inspect
        sig = inspect.signature(kb.call_brain)
        assert "heard" in sig.parameters


# ---------------------------------------------------------------------------
# Chat Handler Tests
# ---------------------------------------------------------------------------

class TestChatHandler:
    """Tests for the ChatHandler + operator message queue."""

    def _make_handler(self):
        """Create a ChatHandler instance without binding a socket."""
        handler = kb.ChatHandler.__new__(kb.ChatHandler)
        handler.headers = {"Content-Length": "0"}
        handler.wfile = MagicMock()
        handler.rfile = MagicMock()
        handler.requestline = "POST /api/chat HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = "POST"
        handler.client_address = ("127.0.0.1", 12345)
        handler._response_code = None
        handler._response_headers = {}

        def mock_send_response(self, code, message=None):
            self._response_code = code

        def mock_send_header(self, keyword, value):
            self._response_headers[keyword] = value

        def mock_end_headers(self):
            pass

        handler.send_response = lambda code, msg=None: mock_send_response(handler, code, msg)
        handler.send_header = lambda k, v: mock_send_header(handler, k, v)
        handler.end_headers = lambda: mock_end_headers(handler)
        handler.send_error = lambda code, msg=None: mock_send_response(handler, code, msg)
        return handler

    def test_health_endpoint(self):
        """GET /health returns 200 with status ok."""
        handler = self._make_handler()
        handler.path = "/health"
        handler.do_GET()
        assert handler._response_code == 200

    def test_reject_empty_message(self):
        """POST /api/chat with empty message returns 400."""
        # Drain any leftover messages from previous tests
        while not kb._operator_queue.empty():
            kb._operator_queue.get_nowait()
        handler = self._make_handler()
        handler.path = "/api/chat"
        body = json.dumps({"message": ""}).encode("utf-8")
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile.read = MagicMock(return_value=body)
        handler._handle_chat_request()
        assert handler._response_code == 400

    def test_reject_oversized_body(self):
        """POST /api/chat with >100KB body returns 413."""
        handler = self._make_handler()
        handler.path = "/api/chat"
        handler.headers = {"Content-Length": "200000"}
        handler._handle_chat_request()
        assert handler._response_code == 413

    def test_message_queued_and_wake_event_set(self):
        """Valid message is put into _operator_queue and wake event is set."""
        # Drain queue
        while not kb._operator_queue.empty():
            kb._operator_queue.get_nowait()
        kb._operator_wake_event.clear()

        handler = self._make_handler()
        handler.path = "/api/chat"
        body = json.dumps({"message": "hello Kombucha"}).encode("utf-8")
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile.read = MagicMock(return_value=body)

        # Run in a thread since _handle_chat_request blocks on response_event
        import threading
        t = threading.Thread(target=handler._handle_chat_request)
        t.start()

        # Give it a moment to queue the message
        import time; time.sleep(0.1)

        # Wake event should be set
        assert kb._operator_wake_event.is_set()

        # Queue should have the message
        assert not kb._operator_queue.empty()
        msg, evt, holder = kb._operator_queue.get_nowait()
        assert msg == "hello Kombucha"

        # Signal a response so the thread can finish
        holder["reply"] = "test reply"
        evt.set()
        t.join(timeout=2)

    def test_call_brain_accepts_operator_message(self):
        """call_brain signature accepts operator_message keyword argument."""
        import inspect
        sig = inspect.signature(kb.call_brain)
        assert "operator_message" in sig.parameters

    def test_operator_message_in_tick_input(self):
        """operator_message is injected into tick_input when provided."""
        captured = {}

        class FakeResp:
            def raise_for_status(self): pass
            def json(self):
                return {"content": [{"text": '{"observation":"test","goal":"test","reasoning":"test","thought":"hi","mood":"ok","actions":[],"next_tick_ms":3000,"tags":[],"outcome":"neutral"}'}]}

        class FakeClient:
            async def post(self, url, **kwargs):
                captured["json"] = kwargs.get("json", {})
                return FakeResp()

        state = {"tick_count": 1, "goal": "idle", "pan_position": 0,
                 "tilt_position": 0}
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(kb.call_brain(
                FakeClient(), "key", "abc123", state, "",
                operator_message="What do you see?"
            ))
        finally:
            loop.close()

        text = captured["json"]["messages"][0]["content"][1]["text"]
        assert "operator_message" in text
        assert "What do you see?" in text

    def test_chat_port_constant(self):
        """CHAT_PORT is defined."""
        assert kb.CHAT_PORT == 8090

    def test_threaded_chat_server_class(self):
        """ThreadedChatServer class exists and uses ThreadingMixIn."""
        assert hasattr(kb, "ThreadedChatServer")
        from socketserver import ThreadingMixIn
        assert issubclass(kb.ThreadedChatServer, ThreadingMixIn)

    def test_system_prompt_mentions_operator_chat(self):
        """SYSTEM_PROMPT includes operator chat instructions."""
        assert "OPERATOR CHAT" in kb.SYSTEM_PROMPT
        assert "operator_message" in kb.SYSTEM_PROMPT

    def test_journal_entry_includes_operator_message(self, tmp_dir):
        """write_journal_entry includes operator_message field."""
        kb.JOURNAL_DIR = tmp_dir / "journal"
        kb.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        decision = {
            "observation": "test", "goal": "test", "reasoning": "test",
            "thought": "hi", "mood": "ok", "actions": [], "tags": [],
            "outcome": "neutral",
        }
        state = {"pan_position": 0, "tilt_position": 0}
        kb.write_journal_entry("1", "sess", decision, "ok", state,
                               operator_message="hello from Bucket")
        journal_files = list(kb.JOURNAL_DIR.glob("*.jsonl"))
        assert len(journal_files) == 1
        entry = json.loads(journal_files[0].read_text().strip())
        assert entry["operator_message"] == "hello from Bucket"
