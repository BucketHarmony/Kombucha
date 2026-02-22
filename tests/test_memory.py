"""Tests for kombucha.memory — MemoryEngine, tagging, context assembly."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from kombucha.config import MemoryConfig
from kombucha.memory import MemoryEngine, enrich_tags, DEFAULT_STATE


@pytest.fixture
def mem_config(tmp_path):
    return MemoryConfig(
        db_path=str(tmp_path / "memory.db"),
        journal_dir=str(tmp_path / "journal"),
        state_file=str(tmp_path / "state.json"),
    )


@pytest.fixture
def engine(mem_config):
    e = MemoryEngine(mem_config)
    yield e
    e.close()


@pytest.fixture
def sample_decision():
    return {
        "observation": "I see a bright hallway with a doorway at the end",
        "goal": "explore the hallway",
        "reasoning": "The doorway looks interesting",
        "thought": "Light spills through the doorway like an invitation",
        "mood": "curious",
        "actions": [
            {"type": "drive", "left": 0.3, "right": 0.3},
            {"type": "oled", "line": 0, "text": "curious"},
        ],
        "next_tick_ms": 5000,
        "tags": ["loc:hallway", "obj:doorway"],
        "outcome": "success",
        "lesson": "Driving straight at 0.3 works well in open hallways",
        "memory_note": "Found a new hallway with natural light",
    }


# ===========================================================================
# Database Initialization
# ===========================================================================

class TestMemoryDB:
    def test_init_creates_tables(self, engine):
        tables = [r[0] for r in engine.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "memories" in tables
        assert "identity" in tables

    def test_init_seeds_identity(self, engine):
        count = engine.db.execute("SELECT COUNT(*) FROM identity WHERE active=TRUE").fetchone()[0]
        assert count == 5

    def test_init_idempotent(self, mem_config):
        e1 = MemoryEngine(mem_config)
        e2 = MemoryEngine(mem_config)
        count = e2.db.execute("SELECT COUNT(*) FROM identity WHERE active=TRUE").fetchone()[0]
        assert count == 5
        e1.close()
        e2.close()

    def test_wal_mode(self, engine):
        mode = engine.db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_creates_indexes(self, engine):
        indexes = [r[0] for r in engine.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()]
        assert "idx_memories_session" in indexes
        assert "idx_memories_tier" in indexes

    def test_v2_tables_created(self, engine):
        """Tick log and prompts tables exist."""
        tables = [r[0] for r in engine.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "tick_log" in tables
        assert "prompts" in tables

    def test_v2_migration_columns(self, engine):
        """v2 migration columns exist on memories table."""
        cols = [r[1] for r in engine.db.execute("PRAGMA table_info(memories)").fetchall()]
        assert "scene_summary" in cols
        assert "directive" in cols

    def test_identity_migration_columns(self, engine):
        """Identity management improvement columns exist."""
        cols = [r[1] for r in engine.db.execute("PRAGMA table_info(identity)").fetchall()]
        assert "reviewed" in cols
        assert "rejected" in cols


# ===========================================================================
# Tagging
# ===========================================================================

class TestTagging:
    def test_enrich_tags_adds_mood(self):
        tags = enrich_tags([], {"mood": "Curious", "actions": []})
        assert "mood:curious" in tags

    def test_enrich_tags_adds_goal(self):
        tags = enrich_tags([], {"goal": "find the door", "actions": []})
        assert any(t.startswith("goal:") for t in tags)

    def test_enrich_tags_adds_action_types(self):
        tags = enrich_tags([], {"actions": [{"type": "drive"}, {"type": "look"}]})
        assert "act:drive" in tags
        assert "act:look" in tags

    def test_enrich_tags_adds_outcome(self):
        tags = enrich_tags([], {"outcome": "failure", "actions": []})
        assert "out:failure" in tags

    def test_enrich_tags_skips_neutral_outcome(self):
        tags = enrich_tags([], {"outcome": "neutral", "actions": []})
        assert "out:neutral" not in tags

    def test_enrich_tags_preserves_agent_tags(self):
        tags = enrich_tags(["loc:kitchen", "person:bucket"], {"actions": []})
        assert "loc:kitchen" in tags
        assert "person:bucket" in tags

    def test_enrich_tags_deduplicates(self):
        tags = enrich_tags(["mood:curious"], {"mood": "curious", "actions": []})
        assert tags.count("mood:curious") == 1


# ===========================================================================
# Memory Insert and Retrieve
# ===========================================================================

class TestMemoryInsert:
    def test_insert_tick(self, engine, sample_decision):
        engine.insert_tick("1", "sess1", sample_decision)
        row = engine.db.execute("SELECT * FROM memories WHERE tick_id='1'").fetchone()
        assert row is not None
        assert row["tier"] == "working"
        assert row["observation"] == sample_decision["observation"]

    def test_insert_stores_tags(self, engine, sample_decision):
        engine.insert_tick("1", "sess1", sample_decision)
        row = engine.db.execute("SELECT tags FROM memories WHERE tick_id='1'").fetchone()
        tags = json.loads(row["tags"])
        assert "loc:hallway" in tags

    def test_insert_logs_identity_proposal(self, engine):
        decision = {"observation": "test", "goal": "test", "mood": "ok",
                     "actions": [], "identity_proposal": "I am brave"}
        engine.insert_tick("1", "sess1", decision)
        row = engine.db.execute(
            "SELECT * FROM identity WHERE source='agent_proposal'"
        ).fetchone()
        assert row is not None
        assert "brave" in row["statement"]

    def test_insert_with_qualia(self, engine):
        decision = {"observation": "test", "goal": "test", "mood": "ok",
                     "actions": [], "qualia": {"continuity": 0.75, "affect": "warm"}}
        engine.insert_tick("1", "sess1", decision)
        row = engine.db.execute("SELECT * FROM memories WHERE tick_id='1'").fetchone()
        assert row["qualia_continuity"] == 0.75
        assert row["qualia_affect"] == "warm"

    def test_insert_with_sme(self, engine):
        decision = {"observation": "test", "goal": "test", "mood": "ok", "actions": []}
        sme = {"frame_delta": 0.05, "anomaly": True, "anomaly_reason": "stuck"}
        engine.insert_tick("1", "sess1", decision, sme=sme)
        row = engine.db.execute("SELECT * FROM memories WHERE tick_id='1'").fetchone()
        assert row["sme_frame_delta"] == 0.05
        assert row["sme_anomaly"] == 1


class TestMemoryRetrieve:
    def test_retrieve_from_other_sessions(self, engine, sample_decision):
        engine.insert_tick("1", "old_session", sample_decision)
        results = engine.retrieve(["loc:hallway"], "new_session", set())
        assert len(results) > 0

    def test_retrieve_excludes_current_session(self, engine, sample_decision):
        engine.insert_tick("1", "sess1", sample_decision)
        results = engine.retrieve(["loc:hallway"], "sess1", set())
        assert len(results) == 0


# ===========================================================================
# Context Assembly
# ===========================================================================

class TestContextAssembly:
    def test_empty_context_has_identity(self, engine):
        state = DEFAULT_STATE.copy()
        ctx = engine.assemble_context(state, "sess1")
        assert "WHO I AM" in ctx
        assert "Kombucha" in ctx

    def test_context_includes_working_memory(self, engine, sample_decision):
        engine.insert_tick("1", "sess1", sample_decision)
        state = DEFAULT_STATE.copy()
        ctx = engine.assemble_context(state, "sess1")
        assert "RECENT TICKS" in ctx


# ===========================================================================
# State Persistence
# ===========================================================================

class TestStatePersistence:
    def test_load_default_state(self, engine):
        state = engine.load_state()
        assert state["goal"] == "wake up and explore"
        assert state["tick_count"] == 0

    def test_save_and_load_state(self, engine, mem_config):
        state = {"goal": "test goal", "tick_count": 42}
        engine.save_state(state)
        loaded = engine.load_state()
        assert loaded["goal"] == "test goal"
        assert loaded["tick_count"] == 42

    def test_load_state_fills_defaults(self, engine, mem_config):
        engine.save_state({"goal": "test"})
        loaded = engine.load_state()
        assert loaded["tick_count"] == 0  # default filled in


# ===========================================================================
# Journal
# ===========================================================================

class TestJournal:
    def test_write_journal_entry(self, engine):
        decision = {"observation": "test", "goal": "test", "mood": "ok",
                     "actions": [], "tags": []}
        state = {"pan_position": 0, "tilt_position": 0}
        engine.write_journal_entry("1", "sess1", decision, "ok", state)

        journal_dir = Path(engine.config.journal_dir)
        files = list(journal_dir.glob("*.jsonl"))
        assert len(files) == 1

        entries = files[0].read_text().strip().split("\n")
        assert len(entries) == 1
        entry = json.loads(entries[0])
        assert entry["tick"] == 1


# ===========================================================================
# Crash Recovery
# ===========================================================================

class TestCrashRecovery:
    def test_recover_replays_missing_entries(self, engine):
        journal_dir = Path(engine.config.journal_dir)
        journal_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "tick": 42,
            "timestamp": datetime.now().isoformat(),
            "session_id": "recovered_sess",
            "observation": "found the door",
            "tags": ["loc:door"],
            "outcome": "success",
        }
        (journal_dir / "2025-01-01.jsonl").write_text(json.dumps(entry) + "\n")

        engine.recover_from_crash()
        row = engine.db.execute("SELECT * FROM memories WHERE tick_id='42'").fetchone()
        assert row is not None
        assert row["observation"] == "found the door"

    def test_recover_skips_existing(self, engine):
        decision = {"observation": "already here", "goal": "test",
                     "mood": "ok", "actions": [], "tags": []}
        engine.insert_tick("42", "sess1", decision)

        journal_dir = Path(engine.config.journal_dir)
        journal_dir.mkdir(parents=True, exist_ok=True)
        entry = {"tick": 42, "observation": "duplicate", "tags": []}
        (journal_dir / "2025-01-01.jsonl").write_text(json.dumps(entry) + "\n")

        engine.recover_from_crash()
        rows = engine.db.execute("SELECT * FROM memories WHERE tick_id='42'").fetchall()
        assert len(rows) == 1
        assert rows[0]["observation"] == "already here"


# ===========================================================================
# Tick Log
# ===========================================================================

class TestTickLog:
    def test_insert_tick_log(self, engine):
        engine.insert_tick_log(
            tick_id="1", session_id="sess1", model="test-model",
            request_json="{}", system_prompt="test", user_message="test",
            context_budget="{}", response_json="{}", response_parsed="{}",
            response_tokens=100, response_time_ms=500, tick_type="routine",
        )
        row = engine.db.execute("SELECT * FROM tick_log WHERE tick_id='1'").fetchone()
        assert row is not None
        assert row["model"] == "test-model"
