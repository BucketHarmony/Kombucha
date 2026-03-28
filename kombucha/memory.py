"""Memory engine for Kombucha v2.

SQLite-backed memory with five tiers: working, session, longterm, tertiary.
Handles insertion, retrieval, compression (via LLM), context assembly,
state persistence, and crash recovery from JSONL journals.
"""

import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from kombucha.config import MemoryConfig

log = logging.getLogger("kombucha.memory")


# --- Structured summary formatting -------------------------------------------

COMPRESS_SECTIONS = [
    ("spatial", "Spatial"), ("social", "Social"), ("lessons", "Lessons"),
    ("sensory_calibration", "Calibration"), ("emotional_arc", "Emotional Arc"),
    ("identity_moments", "Identity"), ("narrative", "Narrative"),
    ("bookmarks", "Bookmarks"), ("opacity_events", "Opacity Events"),
]

SESSION_SUMMARY_SECTIONS = [
    ("spatial_map", "Spatial Map"), ("social_knowledge", "Social"),
    ("lessons", "Lessons"), ("sensory_calibration", "Calibration"),
    ("arc", "Arc"), ("identity", "Identity"),
    ("continuity_trajectory", "Continuity"), ("open_threads", "Open Threads"),
]


def _format_structured_summary(result, section_keys):
    """Concatenate structured LLM output sections into a summary string."""
    parts = []
    for key, label in section_keys:
        val = result.get(key)
        if not val:
            continue
        if isinstance(val, list):
            if not val:
                continue
            val = "\n".join(
                f"  - {item}" if isinstance(item, str) else f"  - {json.dumps(item)}"
                for item in val
            )
        parts.append(f"[{label}] {val}")
    return "\n".join(parts)


# --- Default state -----------------------------------------------------------

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


# --- Tagging Engine ----------------------------------------------------------

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


# --- Memory Engine class -----------------------------------------------------

class MemoryEngine:
    """Manages the SQLite memory database, JSONL journal, and state file."""

    def __init__(self, config: MemoryConfig):
        self.config = config
        self.db = self._init_db(config.db_path)

    def _init_db(self, db_path: str) -> sqlite3.Connection:
        """Initialize SQLite memory database with WAL mode."""
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
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

        # Research instrumentation columns (additive, safe to re-run)
        _migration_cols = [
            ("qualia_attention",       "TEXT"),
            ("qualia_affect",          "TEXT"),
            ("qualia_uncertainty",     "TEXT"),
            ("qualia_drive",           "TEXT"),
            ("qualia_continuity",      "REAL"),
            ("qualia_continuity_basis","TEXT"),
            ("qualia_surprise",        "TEXT"),
            ("qualia_opacity",         "TEXT"),
            ("qualia_raw",             "TEXT"),
            ("model",                  "TEXT"),
            ("sme_frame_delta",        "REAL"),
            ("sme_drive_expected",     "BOOLEAN"),
            ("sme_motion_detected",    "BOOLEAN"),
            ("sme_anomaly",            "BOOLEAN"),
            ("sme_anomaly_reason",     "TEXT"),
            ("sme_gimbal_error_pan",   "REAL"),
            ("sme_gimbal_error_tilt",  "REAL"),
            ("sme_raw",                "TEXT"),
        ]
        for col_name, col_type in _migration_cols:
            try:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass  # column already exists

        # v2 migration columns
        _v2_migration_cols = [
            ("scene_summary", "TEXT"),
            ("hardware_summary", "TEXT"),
            ("directive", "TEXT"),
            ("directive_params", "TEXT"),
            ("events", "TEXT"),
            ("sme_battery_pct", "INTEGER"),
            ("sme_distance_m", "REAL"),
        ]
        for col_name, col_type in _v2_migration_cols:
            try:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

        # Identity management improvements
        _identity_migration_cols = [
            ("reviewed", "BOOLEAN DEFAULT FALSE"),
            ("rejected", "BOOLEAN DEFAULT FALSE"),
            ("rejected_at", "TEXT"),
            ("rejected_by", "TEXT"),
            ("reject_reason", "TEXT"),
            ("source_tick", "TEXT"),
        ]
        for col_name, col_type in _identity_migration_cols:
            try:
                conn.execute(f"ALTER TABLE identity ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_tertiary
            ON memories(tier, session_id) WHERE tier = 'tertiary'
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_opacity
            ON memories(session_id, qualia_opacity) WHERE qualia_opacity IS NOT NULL
        """)

        # Prompt registry table
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created TEXT NOT NULL,
                created_by TEXT NOT NULL,
                notes TEXT,
                token_count INTEGER
            );

            CREATE TABLE IF NOT EXISTS tick_log (
                id INTEGER PRIMARY KEY,
                tick_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                model TEXT NOT NULL,
                request_json TEXT NOT NULL,
                system_prompt TEXT NOT NULL,
                user_message TEXT NOT NULL,
                context_budget TEXT NOT NULL,
                memory_retrieved TEXT,
                memory_scoring TEXT,
                identity_core TEXT,
                response_json TEXT NOT NULL,
                response_parsed TEXT NOT NULL,
                response_tokens INTEGER,
                response_time_ms INTEGER,
                tick_type TEXT NOT NULL,
                wake_reason TEXT,
                hardware_snapshot TEXT
            );
        """)

        conn.commit()

        # Seed prompts table from filesystem if empty
        prompt_count = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
        if prompt_count == 0:
            self._seed_prompts(conn)

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

    def close(self):
        """Close the database connection."""
        if self.db:
            try:
                self.db.close()
            except Exception:
                pass

    # --- Prompt registry ------------------------------------------------------

    def _seed_prompts(self, conn):
        """Seed prompts table from filesystem .md files."""
        prompts_dir = Path(self.config.db_path).parent.parent / "prompts"
        # Also check relative to CWD
        if not prompts_dir.exists():
            prompts_dir = Path("prompts")
        if not prompts_dir.exists():
            return
        now = datetime.now().isoformat()
        for md_file in sorted(prompts_dir.glob("*.md")):
            name = md_file.stem  # e.g., "system", "compress", "tertiary"
            content = md_file.read_text(encoding="utf-8")
            try:
                conn.execute(
                    "INSERT INTO prompts (name, content, version, active, created, created_by, token_count) "
                    "VALUES (?, ?, 1, TRUE, ?, 'seed', ?)",
                    [name, content, now, len(content) // 4],  # rough token estimate
                )
            except Exception:
                pass
        conn.commit()
        log.info(f"Seeded {conn.execute('SELECT COUNT(*) FROM prompts').fetchone()[0]} prompts from filesystem")

    def load_prompt(self, name: str, prompts_dir: Optional[str] = None) -> Optional[str]:
        """Load a prompt by name. DB first, then filesystem fallback."""
        # Try DB first
        row = self.db.execute(
            "SELECT content FROM prompts WHERE name = ? AND active = TRUE ORDER BY version DESC LIMIT 1",
            [name],
        ).fetchone()
        if row:
            return row["content"]

        # Filesystem fallback
        search_dirs = []
        if prompts_dir:
            search_dirs.append(Path(prompts_dir))
        search_dirs.append(Path("prompts"))
        search_dirs.append(Path(self.config.db_path).parent.parent / "prompts")

        for d in search_dirs:
            path = d / f"{name}.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None

    def reload_prompts(self, prompts_dir: Optional[str] = None):
        """Reload all prompts from filesystem into DB, bumping versions."""
        search_dirs = []
        if prompts_dir:
            search_dirs.append(Path(prompts_dir))
        search_dirs.append(Path("prompts"))
        search_dirs.append(Path(self.config.db_path).parent.parent / "prompts")

        now = datetime.now().isoformat()
        reloaded = 0
        for d in search_dirs:
            if not d.exists():
                continue
            for md_file in sorted(d.glob("*.md")):
                name = md_file.stem
                content = md_file.read_text(encoding="utf-8")
                # Get current version
                row = self.db.execute(
                    "SELECT version, content FROM prompts WHERE name = ? ORDER BY version DESC LIMIT 1",
                    [name],
                ).fetchone()
                if row and row["content"] == content:
                    continue  # no change
                version = (row["version"] + 1) if row else 1
                # Deactivate old versions
                self.db.execute("UPDATE prompts SET active = FALSE WHERE name = ?", [name])
                self.db.execute(
                    "INSERT INTO prompts (name, content, version, active, created, created_by, token_count) "
                    "VALUES (?, ?, ?, TRUE, ?, 'reload', ?)",
                    [name, content, version, now, len(content) // 4],
                )
                reloaded += 1
            break  # use first existing directory
        if reloaded:
            self.db.commit()
            log.info(f"Reloaded {reloaded} prompts from filesystem")

    # --- State persistence ----------------------------------------------------

    def load_state(self, state_file: Optional[str] = None) -> dict:
        """Load rover state from JSON file."""
        path = Path(state_file or self.config.state_file)
        if path.exists():
            try:
                state = json.loads(path.read_text())
                for key, val in DEFAULT_STATE.items():
                    state.setdefault(key, val)
                return state
            except Exception:
                pass
        state = DEFAULT_STATE.copy()
        state["session_start"] = datetime.now().isoformat()
        import uuid
        state["session_id"] = str(uuid.uuid4())[:8]
        return state

    def save_state(self, state: dict, state_file: Optional[str] = None) -> None:
        """Atomic write: write to temp file then rename."""
        path = Path(state_file or self.config.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # --- Memory insertion -----------------------------------------------------

    def insert_tick(self, tick_id, session_id, decision, model_used=None, sme=None,
                    scene_summary=None, hardware_summary=None, events=None):
        """Insert a working memory entry with qualia, model provenance, and SME."""
        agent_tags = decision.get("tags", [])
        tags = enrich_tags(agent_tags, decision)
        outcome = decision.get("outcome", "neutral")

        # Extract qualia block
        qualia = decision.get("qualia") or {}
        continuity_float = None
        continuity_raw = qualia.get("continuity")
        if continuity_raw is not None:
            try:
                continuity_float = float(str(continuity_raw).split()[0])
                continuity_float = max(0.0, min(1.0, continuity_float))
            except (ValueError, IndexError):
                pass
        opacity_val = qualia.get("opacity")

        self.db.execute("""
            INSERT INTO memories
                (tick_id, timestamp, session_id, tier, thought, observation,
                 goal, mood, actions, outcome, tags, success, failure,
                 lesson, memory_note,
                 qualia_attention, qualia_affect, qualia_uncertainty,
                 qualia_drive, qualia_continuity, qualia_continuity_basis,
                 qualia_surprise, qualia_opacity, qualia_raw,
                 model,
                 sme_frame_delta, sme_drive_expected, sme_motion_detected,
                 sme_anomaly, sme_anomaly_reason,
                 sme_gimbal_error_pan, sme_gimbal_error_tilt, sme_raw,
                 scene_summary, hardware_summary, events)
            VALUES (?, ?, ?, 'working', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?)
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
            qualia.get("attention"),
            qualia.get("affect"),
            qualia.get("uncertainty"),
            qualia.get("drive"),
            continuity_float,
            qualia.get("continuity_basis"),
            qualia.get("surprise"),
            opacity_val,
            json.dumps(qualia) if qualia else None,
            model_used,
            sme.get("frame_delta") if sme else None,
            sme.get("drive_expected_motion") if sme else None,
            sme.get("motion_detected") if sme else None,
            sme.get("anomaly") if sme else None,
            sme.get("anomaly_reason") if sme else None,
            sme.get("gimbal_error_pan") if sme else None,
            sme.get("gimbal_error_tilt") if sme else None,
            json.dumps(sme) if sme else None,
            scene_summary,
            hardware_summary,
            json.dumps(events) if events else None,
        ])
        self.db.commit()

        # Log identity proposals (not auto-accepted)
        proposal = decision.get("identity_proposal")
        if proposal and isinstance(proposal, str) and proposal.strip():
            self.db.execute(
                "INSERT INTO identity (statement, source, created, active) "
                "VALUES (?, 'agent_proposal', ?, FALSE)",
                [proposal.strip(), datetime.now().isoformat()]
            )
            self.db.commit()
            log.info(f"  IDENTITY PROPOSAL: {proposal.strip()}")

    # --- Memory retrieval -----------------------------------------------------

    def retrieve(self, current_tags, session_id, working_tick_ids):
        """Search for relevant past memories using tag overlap scoring."""
        if not current_tags:
            return []

        tag_set = set(current_tags)

        rows = self.db.execute("""
            SELECT * FROM memories
            WHERE archived = FALSE
              AND session_id != ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, [session_id, self.config.retrieval_scan_limit]).fetchall()

        scored = []
        for row in rows:
            if row["tick_id"] in working_tick_ids:
                continue
            mem_tags = json.loads(row["tags"]) if row["tags"] else []
            overlap = len(tag_set & set(mem_tags))
            if overlap == 0:
                continue

            score = (
                overlap * self.config.tag_weight_overlap
                + (self.config.tag_weight_success if row["success"] else 0.0)
                + (self.config.tag_weight_failure if row["failure"] else 0.0)
                + (self.config.tag_weight_lesson if row["lesson"] else 0.0)
            )
            scored.append((score, dict(row)))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = [row for _, row in scored[:self.config.retrieval_top_k]]

        # Update retrieval metadata
        now_iso = datetime.now().isoformat()
        for row in results:
            self.db.execute(
                "UPDATE memories SET relevance_hits = relevance_hits + 1, "
                "last_retrieved = ? WHERE id = ?",
                [now_iso, row["id"]]
            )
        if results:
            self.db.commit()

        return results

    # --- Context assembly -----------------------------------------------------

    def assemble_context(self, state, session_id):
        """Build the full memory context block for the mind prompt."""
        parts = []

        # 1. Identity core
        identity = self.db.execute(
            "SELECT statement FROM identity WHERE active = TRUE ORDER BY id"
        ).fetchall()
        if identity:
            parts.append("=== WHO I AM ===")
            for row in identity:
                parts.append(f"- {row['statement']}")
            parts.append("")

        # 2. Retrieved memories
        working = self.db.execute("""
            SELECT * FROM memories
            WHERE tier = 'working' AND session_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, [session_id, self.config.working_size]).fetchall()
        working_tick_ids = set(row["tick_id"] for row in working)

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

        retrieved = self.retrieve(retrieval_tags, session_id, working_tick_ids)
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

        # 3. Long-term memory
        longterm = self.db.execute("""
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

        # 4. Session memory
        session_mem = self.db.execute("""
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

        # 5. Working memory
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

    # --- Journal writer -------------------------------------------------------

    def write_journal_entry(self, tick_id, session_id, decision, result, state,
                            model_used=None, sme=None, prompt=None,
                            raw_response=None, operator_message=None):
        """Append a JSONL entry to the daily journal file."""
        journal_dir = Path(self.config.journal_dir)
        journal_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        journal_file = journal_dir / f"{today}.jsonl"

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
            "qualia": decision.get("qualia"),
            "model": model_used,
            "sme": sme,
            "prompt": prompt,
            "raw_response": raw_response,
            "operator_message": operator_message,
        }

        try:
            with open(journal_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            log.warning(f"Journal write failed: {e}")

    # --- Crash recovery -------------------------------------------------------

    def recover_from_crash(self):
        """Replay JSONL journal entries not yet in the database."""
        journal_dir = Path(self.config.journal_dir)
        if not journal_dir.exists():
            return

        existing = set()
        for row in self.db.execute("SELECT tick_id FROM memories WHERE tier='working'").fetchall():
            existing.add(row["tick_id"])

        recovered = 0
        for jsonl_file in sorted(journal_dir.glob("*.jsonl")):
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
                        self.db.execute("""
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
            self.db.commit()
            log.info(f"Crash recovery: replayed {recovered} journal entries into memory DB")

    # --- Compression (requires LLM client) ------------------------------------

    async def compress(self, client, api_key, session_id, load_prompt_fn,
                       api_url, model, api_version="2023-06-01"):
        """Compress aged working memories into session summaries via Haiku."""
        rows = self.db.execute("""
            SELECT * FROM memories
            WHERE tier = 'working'
              AND session_id = ?
              AND compressed = FALSE
            ORDER BY timestamp ASC
        """, [session_id]).fetchall()

        if len(rows) <= self.config.working_size:
            return

        to_compress = list(rows)[:-self.config.working_size]
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
            if row["qualia_continuity"] is not None:
                parts.append(f"Continuity: {row['qualia_continuity']:.2f}")
            if row["qualia_opacity"]:
                parts.append(f"Opacity: {row['qualia_opacity']}")
            if row["qualia_surprise"]:
                parts.append(f"Surprise: {row['qualia_surprise']}")
            if row["sme_frame_delta"] is not None:
                parts.append(f"Frame delta: {row['sme_frame_delta']:.3f}")
            entries_text.append(f"[Tick {row['tick_id']}] {'. '.join(parts)}")

        prompt = load_prompt_fn("compress.md").replace("{entries}", "\n".join(entries_text))

        try:
            resp = await client.post(
                api_url,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": api_version,
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 1200,
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

            summary = _format_structured_summary(result, COMPRESS_SECTIONS)
            if not summary:
                summary = result.get("summary", "")
            tags = result.get("tags", [])

            if summary:
                first_tick = to_compress[0]["tick_id"]
                last_tick = to_compress[-1]["tick_id"]
                self.db.execute("""
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
                self.db.execute(
                    "UPDATE memories SET compressed = TRUE WHERE id = ?",
                    [row["id"]]
                )

            self.db.commit()
            log.info(f"  Compressed {len(to_compress)} ticks into session memory")

        except Exception as e:
            log.warning(f"Compression failed (non-critical): {e}")

    async def generate_session_summary(self, client, api_key, session_id,
                                        load_prompt_fn, api_url, model,
                                        api_version="2023-06-01"):
        """Generate a long-term memory entry for the ending session."""
        tick_count = self.db.execute(
            "SELECT COUNT(*) FROM memories WHERE session_id = ? AND tier = 'working'",
            [session_id]
        ).fetchone()[0]

        if tick_count < 3:
            log.info("Session too short for long-term summary, skipping")
            return

        rows = self.db.execute("""
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

        prompt = load_prompt_fn("session_summary.md").replace("{entries}", "\n".join(entries))

        try:
            resp = await client.post(
                api_url,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": api_version,
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 1000,
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

            summary = _format_structured_summary(result, SESSION_SUMMARY_SECTIONS)
            if not summary:
                summary = result.get("summary", "")
            tags = result.get("tags", [])

            if summary:
                self.db.execute("""
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
                self.db.commit()
                log.info(f"Generated long-term memory for session {session_id}")

        except Exception as e:
            log.warning(f"Session summary generation failed: {e}")

    # --- Tick log (Mission Control) -------------------------------------------

    def insert_tick_log(self, tick_id, session_id, model, request_json,
                        system_prompt, user_message, context_budget,
                        response_json, response_parsed, response_tokens,
                        response_time_ms, tick_type, wake_reason=None,
                        hardware_snapshot=None, memory_retrieved=None,
                        memory_scoring=None, identity_core=None):
        """Log full request/response JSON for Mission Control inspection."""
        try:
            self.db.execute("""
                INSERT INTO tick_log
                    (tick_id, session_id, timestamp, model, request_json,
                     system_prompt, user_message, context_budget,
                     memory_retrieved, memory_scoring, identity_core,
                     response_json, response_parsed, response_tokens,
                     response_time_ms, tick_type, wake_reason, hardware_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                tick_id, session_id, datetime.now().isoformat(), model,
                request_json, system_prompt, user_message, context_budget,
                memory_retrieved, memory_scoring, identity_core,
                response_json, response_parsed, response_tokens,
                response_time_ms, tick_type, wake_reason, hardware_snapshot,
            ])
            self.db.commit()
        except Exception as e:
            log.warning(f"Tick log insert failed: {e}")
