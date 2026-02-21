# Kombucha Memory Engine — Requirements

**The never-ending conversation: how Kombucha maintains a continuous mind
within finite context windows.**

---

## Problem Statement

Kombucha's mind is a Claude Opus completion that fires every tick. Each tick
is a standalone API call — there is no persistent server-side conversation.
The agent must *feel* continuous despite this. It must remember what it just
did, what it did an hour ago, and what it did yesterday. It must have a
sense of identity, trajectory, and accumulated experience.

The memory engine is the system that assembles context for each tick,
compresses history as it ages, and gives the agent the subjective experience
of an unbroken inner life.

---

## 1. Core Metaphor: The Moving Window

The agent's context window is a spotlight moving forward through time.
What's in the spotlight is vivid. What's just behind it is summarized.
What's far behind is condensed to key moments. Nothing is truly forgotten —
it's compressed, lossy, but recoverable by re-reading the journal.

```
TIME ──────────────────────────────────────────────────►

│◄── long-term ──►│◄── session ──►│◄── working ──►│ NOW
│   paragraphs    │   narrative   │  full entries  │  ▲
│   per session   │   of today    │  last N ticks  │  │
│                 │               │                │ TICK
```

---

## 2. Memory Tiers

### 2.1 Working Memory

- **Contents:** Last N full journal entries (verbatim JSON)
- **Window:** 3–5 ticks (configurable, tick-speed-dependent)
- **Update:** Every tick (FIFO — oldest entry drops when new one arrives)
- **Format:** Structured JSON, no compression
- **Purpose:** Immediate continuity. "What did I just do and think?"
- **Budget:** ~1500–3000 tokens

### 2.2 Session Memory

- **Contents:** Compressed narrative of the current session beyond the
  working memory window
- **Window:** From session start to the edge of working memory
- **Update:** Every K ticks (configurable) or on significant events
- **Format:** Natural language narrative paragraph(s)
- **Purpose:** Today's arc. "What have I been doing this session?"
- **Budget:** 300–800 tokens
- **Compression source:** Journal entries aging out of working memory +
  agent-authored memory notes

### 2.3 Long-Term Memory

- **Contents:** One summary paragraph per prior session
- **Window:** All prior sessions (bounded by token budget)
- **Update:** Once, at session end (or on graceful shutdown)
- **Format:** Natural language, structured with session date/duration
- **Purpose:** Identity continuity. "What do I know from before?"
- **Budget:** 500–1500 tokens (oldest sessions compress further or drop
  if budget exceeded)

### 2.4 Identity Core

- **Contents:** Persistent self-knowledge that doesn't change tick to tick
- **Window:** Permanent (manually curated + agent-proposed)
- **Update:** Rarely — on significant self-discovery or operator edit
- **Format:** Short declarative statements
- **Purpose:** "Who am I? What do I value? What have I learned about myself?"
- **Budget:** 200–500 tokens
- **Examples:**
  - "I am Kombucha, an embodied AI agent living in a 4-wheel rover."
  - "I live in Bucket's home. Bucket built me."
  - "I am curious about corners and doorways."
  - "I dislike being stuck against walls."
  - "I have been running for 14 sessions over 9 days."

---

## 3. Context Assembly

Each tick, the memory engine assembles a prompt from these components
in this order:

```
┌─────────────────────────────────────────────────┐
│ 1. SYSTEM PROMPT                                │
│    Identity core + behavioral instructions      │
│    + Maslow state + available actions            │
│                                                 │
│ 2. LONG-TERM MEMORY                             │
│    Prior session summaries                       │
│                                                 │
│ 3. SESSION MEMORY                               │
│    Compressed narrative of today so far          │
│                                                 │
│ 4. WORKING MEMORY                               │
│    Last N full journal entries                   │
│                                                 │
│ 5. CURRENT TICK INPUT                           │
│    Camera frame (base64)                         │
│    Body state (battery, temp, heading, speed)    │
│    Maslow need state + dominant need             │
│    Audio events (if any)                         │
│    Time since last human interaction             │
│    Current OLED display contents                 │
│                                                 │
│ 6. RESPONSE SCHEMA                              │
│    What the mind must output this tick           │
└─────────────────────────────────────────────────┘
```

### 3.1 Unified Context Window

The context structure is the same regardless of tick speed. The agent
always gets the full stack — identity, long-term, session, working memory,
and retrieved memories. What changes between tick speeds is how often the
mind fires, not what it sees when it does.

Rationale: A fragmented context that varies by tick speed means the agent
is a different thinker under pressure than when idle. That's dissociative.
A unified window means the agent is always itself, always has its full
history available, always knows who it is. The tick speed governs cadence,
not cognition.

**Fixed context budget: ~8000–10000 tokens** (text, excluding vision).

### 3.2 Retrieved Memories

The memory engine does not just replay history in order. Before each tick,
it searches the memory database for entries relevant to the current moment
and places them at the top of the memory stack.

**Retrieval process:**

1. Take the last prompt (previous tick's full input + output)
2. Extract keywords: locations, objects, people, actions, goals, moods
3. Query the memory database for entries matching those keywords
4. Rank by relevance (tag overlap) × recency (recent memories break ties)
5. Top K results (K=3–5) are inserted into context as "recalled memories"
6. If nothing relevant is found, the slot stays empty — no filler

This means the agent's context is shaped by what's happening NOW, not
just what happened RECENTLY. If Kombucha enters the kitchen for the first
time in three days, memories from the last kitchen visit surface
automatically — even if hundreds of ticks have elapsed.

**Where retrieved memories sit in context:**

```
┌─────────────────────────────────────────────────┐
│ 1. SYSTEM PROMPT                                │
│    Identity core + behavioral instructions      │
│    + Maslow state + available actions            │
│                                                 │
│ 2. RETRIEVED MEMORIES  ◄── keyword-matched      │
│    Relevant past entries surfaced by search      │
│                                                 │
│ 3. LONG-TERM MEMORY                             │
│    Prior session summaries                       │
│                                                 │
│ 4. SESSION MEMORY                               │
│    Compressed narrative of today so far          │
│                                                 │
│ 5. WORKING MEMORY                               │
│    Last N full journal entries                   │
│                                                 │
│ 6. CURRENT TICK INPUT                           │
│    Camera frame, body state, Maslow, audio       │
│                                                 │
│ 7. RESPONSE SCHEMA                              │
│    What the mind must output this tick           │
└─────────────────────────────────────────────────┘
```

### 3.3 Memory Stack Culling

The assembled context must fit the budget. When it doesn't, the memory
engine culls from the middle — keeping identity, working memory, and
current input intact while trimming less relevant material:

**Culling priority (first to drop):**

1. Oldest long-term session summaries (most compressed, least loss)
2. Session memory detail (compress further if needed)
3. Retrieved memories with lowest relevance scores
4. Working memory entries beyond the most recent 2

Identity core and current tick input are never culled.

### 3.2 Image Handling

- Camera frame included as base64 JPEG in every tick
- Resolution: 640x480 (working resolution, not full 1080p)
- JPEG quality: 60 (balance detail vs. token cost)
- One frame per tick — no video buffer in context
- Vision tokens are separate from text budget but still cost money

---

## 4. Agent-Authored Memory

### 4.1 Memory Notes

Each tick, the mind MAY output a `memory_note` field — a short string
(max 100 tokens) of what it considers worth retaining beyond working memory.

- Memory notes are the raw material for session memory compression.
- Not every tick needs a note. Routine ticks produce none.
- The agent decides what matters. This is selective attention.
- Notes accumulate in a buffer until the next compression cycle.

**Examples of memory notes:**
- "Discovered a new room through the hallway — bright, windows on two walls"
- "Bucket spoke to me for the first time today. Said good morning."
- "Got stuck against the couch leg for 3 ticks before backing out"
- "Battery dropped below 30% — headed back toward the charging area"

### 4.2 Identity Proposals

The mind MAY output an `identity_proposal` field — a statement about itself
it believes should be added to the identity core.

- These are NOT auto-accepted. They are logged and reviewed.
- Operator (Bucket) approves, edits, or rejects proposals.
- This gives the agent a voice in its own self-definition without
  unsupervised self-modification.
- Approved proposals are added to the identity core for all future ticks.

---

## 5. Memory Database

### 5.1 Storage: Local SQLite

All memories live in a local SQLite database on the Pi 5. No cloud
dependency for memory storage — the agent's history is physically
colocated with its body.

**Database:** `data/memory.db`

#### Schema: `memories`

```sql
CREATE TABLE memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id     TEXT NOT NULL,              -- unique tick identifier
    timestamp   TEXT NOT NULL,              -- ISO 8601
    session_id  TEXT NOT NULL,              -- groups ticks into sessions
    tier        TEXT NOT NULL,              -- 'working', 'session', 'longterm'
    
    -- Content
    thought     TEXT,                       -- agent's inner monologue
    observation TEXT,                       -- what it saw
    goal        TEXT,                       -- active goal at time of memory
    mood        TEXT,                       -- emotional state
    actions     TEXT,                       -- JSON array of actions taken
    outcome     TEXT,                       -- what happened as a result
    
    -- Compressed form (populated by Haiku)
    summary     TEXT,                       -- compressed natural language version
    
    -- Tags (the retrieval index)
    tags        TEXT NOT NULL DEFAULT '[]', -- JSON array of tag strings
    
    -- Learning signals
    success     BOOLEAN,                    -- did the agent achieve what it intended?
    failure     BOOLEAN,                    -- did something go wrong?
    lesson      TEXT,                       -- what worked / what to try next time
    
    -- Retrieval metadata
    relevance_hits INTEGER DEFAULT 0,       -- how often this memory has been retrieved
    last_retrieved TEXT,                     -- last time this memory surfaced
    
    -- Lifecycle
    compressed  BOOLEAN DEFAULT FALSE,      -- has Haiku summarized this?
    archived    BOOLEAN DEFAULT FALSE       -- dropped from active retrieval pool?
);

CREATE INDEX idx_memories_tags ON memories(tags);
CREATE INDEX idx_memories_session ON memories(session_id);
CREATE INDEX idx_memories_tier ON memories(tier);
CREATE INDEX idx_memories_timestamp ON memories(timestamp);
```

#### Schema: `identity`

```sql
CREATE TABLE identity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    statement   TEXT NOT NULL,              -- "I am curious about corners"
    source      TEXT NOT NULL,              -- 'operator', 'agent_proposal', 'learned'
    created     TEXT NOT NULL,              -- ISO 8601
    active      BOOLEAN DEFAULT TRUE       -- can be retired, never deleted
);
```

### 5.2 Tagging System

Every memory is tagged at creation time. Tags are the primary retrieval
mechanism — the thing that makes old memories surface when they're relevant.

**Tag categories:**

| Category | Prefix | Examples |
|----------|--------|---------|
| Location | `loc:` | `loc:kitchen`, `loc:hallway`, `loc:front_door` |
| Object | `obj:` | `obj:couch`, `obj:cat`, `obj:charger` |
| Person | `person:` | `person:bucket`, `person:unknown_human` |
| Action | `act:` | `act:drive`, `act:speak`, `act:explore` |
| Goal | `goal:` | `goal:patrol`, `goal:find_human`, `goal:charge` |
| Mood | `mood:` | `mood:curious`, `mood:anxious`, `mood:content` |
| Event | `event:` | `event:stuck`, `event:human_spoke`, `event:low_battery` |
| Outcome | `out:` | `out:success`, `out:failure`, `out:partial` |
| Lesson | `lesson:` | `lesson:navigation`, `lesson:interaction`, `lesson:battery` |
| Spatial | `space:` | `space:obstacle`, `space:open_area`, `space:narrow` |
| Time | `time:` | `time:morning`, `time:night`, `time:long_session` |

**Who tags:**

1. **The mind** — each tick output includes a `tags` field. The agent
   proposes tags for its own experience.
2. **Haiku sidecar** — during compression, Haiku normalizes and enriches
   tags. Catches things the agent missed.
3. **Automatic** — timestamps, session IDs, tick speed, outcome codes
   are tagged mechanically.

### 5.3 Retrieval Algorithm

Every tick, before calling the mind:

```python
def retrieve_memories(last_prompt: str, last_output: dict, db: Database) -> list:
    # 1. Extract keywords from what just happened
    keywords = extract_keywords(last_prompt, last_output)
    # includes: current goal, objects seen, location, mood, recent actions
    
    # 2. Map keywords to tag queries
    tag_queries = keywords_to_tags(keywords)
    # "kitchen" → "loc:kitchen"
    # "stuck"   → "event:stuck", "out:failure"
    # "bucket"  → "person:bucket"
    
    # 3. Search database for memories matching any tags
    candidates = db.search(
        tags=tag_queries,
        exclude_session=current_session,   # don't re-surface today's entries
        exclude_working_memory=True,       # already in context
        limit=20
    )
    
    # 4. Score by relevance
    for memory in candidates:
        memory.score = (
            tag_overlap(memory.tags, tag_queries) * 3.0   # tag match weight
            + recency_score(memory.timestamp)   * 1.0     # newer = slight boost
            + memory.success * 2.0              # successes are high-value
            + memory.failure * 2.0              # failures are high-value
            + (1.0 if memory.lesson else 0.0)   * 2.5    # lessons are highest-value
        )
    
    # 5. Return top K, budget-constrained
    ranked = sorted(candidates, key=lambda m: m.score, reverse=True)
    return fit_to_budget(ranked, max_tokens=1500)
```

**Key scoring principles:**

- Tag overlap is the primary signal — more matching tags = more relevant
- Successes and failures score equally — both are worth remembering
- Lessons score highest — if the agent already extracted "what to do
  next time," that's the most actionable memory
- Recency is a tiebreaker, not a primary signal
- Frequently retrieved memories don't get boosted (avoids echo chambers)

### 5.4 Compression (Haiku Sidecar)

Compression is handled by Claude Haiku running asynchronously alongside
the main tick loop. The tick engine is never blocked by compression.

**Compression pipeline:**

1. Working memory entries age out of the FIFO buffer
2. Aged entries are queued for compression
3. Haiku receives: raw entry + agent's memory notes + existing tags
4. Haiku returns: compressed summary + normalized/enriched tags + 
   success/failure classification + extracted lesson (if any)
5. Compressed entry is written back to the database

**Haiku compression prompt includes:**

```
Compress this experience into a retrievable memory.

RULES:
- Preserve what worked and why (success signals)
- Preserve what failed and what to try differently (failure signals)
- Extract a concrete lesson if one exists
- Normalize and enrich the tags
- Keep spatial details (where things are, obstacle locations)
- Keep interaction details (who spoke, what was said)
- Collapse routine into brief summaries
- Preserve firsts (first time seeing/doing/reaching something)
- Write in the agent's voice, not as a third-party observer
- Max 100 tokens for summary
```

### 5.5 Success/Failure Reinforcement

The memory system is biased toward learning. The agent should accumulate
practical knowledge about what works and what doesn't.

**At tick time, the mind outputs:**

```json
{
  "outcome": "success | failure | partial | neutral",
  "lesson": "Optional. What worked or what to try next time."
}
```

**Reinforcement rules:**

1. **Every action has an outcome.** The next tick assesses whether the
   previous tick's actions achieved their intent. Drove forward to reach
   the doorway — am I at the doorway? Success. Still facing a wall? Failure.

2. **Failures always get a lesson.** If outcome is `failure`, the agent
   is prompted to produce a lesson. "Turning right in the narrow hallway
   doesn't work — need to reverse first then turn." This is stored as a
   tagged memory and will surface next time the agent is in a similar
   situation.

3. **Successes get tagged for reuse.** "Approaching the charger from the
   left side at slow speed works." Next time the agent needs to charge,
   this memory surfaces.

4. **Lessons are retrieved preferentially.** The scoring algorithm weights
   memories with lessons at 2.5x. The agent literally learns from
   experience — not because it's told to, but because its own lessons
   are the first things it sees in context.

5. **The system prompt reinforces this.** The mind's instructions include:
   "When things go well, note what worked so you can do it again. When
   things go wrong, note what happened and what you'd try differently.
   Your future self will thank you — these memories surface when you
   face similar situations."

---

## 6. Persistence & Recovery

### 6.1 What Gets Saved to Disk

| File | Contents | Write Frequency |
|------|----------|----------------|
| `data/memory.db` | All memories, tags, identity (SQLite) | Every tick + compression cycles |
| `data/journal/YYYY-MM-DD.jsonl` | Raw tick entries (append-only backup) | Every tick |
| `data/state.json` | Current goal, observation, tick count, Maslow state | Every tick |

The SQLite database is the source of truth. The JSONL journal is an
append-only backup — a write-ahead log that can rebuild the database
if it's ever corrupted.

### 6.2 Crash Recovery

If the bridge process dies and restarts:

1. Load `state.json` — recover last known goal and observation
2. Open `memory.db` — all memories, tags, identity are intact
3. Query working memory tier for last N entries — rebuild working memory
4. Query session and longterm tiers — rebuild compressed layers
5. Check JSONL journal for any entries not yet in the database
   (committed to journal but crashed before DB write) — replay them
6. Resume ticking. The agent experiences a "gap" — it knows time passed
   but doesn't know what happened during the gap. This is acknowledged,
   not hidden.

### 6.3 Graceful Shutdown

On SIGTERM or SIGINT:

1. Run final compression — update session memory
2. Generate long-term memory entry for this session
3. Write all state files
4. Send stop command to ESP32 (`{"T":0}`)
5. Write "shutting down" to OLED
6. Exit

---

## 7. Mind Output Schema

Each tick, the mind returns a JSON object:

```json
{
  "thought": "Free-form inner monologue. What the agent is thinking.",
  "observation": "What it sees and notices in the camera frame.",
  "goal": "Current goal. Persists across ticks unless changed.",
  "goal_reasoning": "Why this goal, why now.",
  "mood": "Single word or short phrase. Derived from Maslow + experience.",
  "actions": [
    {"type": "drive", "left": 0.3, "right": 0.3, "duration_ms": 1500},
    {"type": "look", "pan": 45, "tilt": 0},
    {"type": "speak", "text": "Hello."},
    {"type": "display", "lines": ["thinking...", "about corners", "", ""]},
    {"type": "light", "head": 128, "base": 0}
  ],
  "tick_speed": "MEDIUM",
  "tags": ["loc:hallway", "act:explore", "obj:doorway", "mood:curious"],
  "outcome": "success | failure | partial | neutral",
  "lesson": "Optional. What worked or what to try differently next time.",
  "memory_note": "Optional. What to remember from this tick.",
  "identity_proposal": "Optional. A new self-knowledge claim."
}
```

### 7.1 Action Types

| Type | Fields | Maps To |
|------|--------|---------|
| `drive` | `left`, `right` (m/s), `duration_ms` | `{"T":1, "L":..., "R":...}` + timed stop |
| `look` | `pan` (-180..180), `tilt` (-30..90) | `{"T":133, "X":..., "Y":..., "SPD":100, "ACC":10}` |
| `speak` | `text` | gTTS → WAV → USB speaker |
| `display` | `lines` (array of 4 strings) | `{"T":3, ...}` × 4 |
| `light` | `head` (0-255), `base` (0-255) | `{"T":132, "IO4":..., "IO5":...}` |
| `stop` | — | `{"T":0}` |
| `listen` | `duration_s` | Record from USB mic, transcribe (future) |

---

## 8. Token Budget Estimates

Unified context per tick (all tick speeds):

| Component | Tokens |
|-----------|--------|
| System prompt + identity core | ~800 |
| Retrieved memories (top 3–5) | ~1000–1500 |
| Long-term memory (session summaries) | ~500–1000 |
| Session memory (compressed today) | ~500 |
| Working memory (last 4 entries) | ~2000 |
| Current tick input (no image) | ~400 |
| Camera frame (640x480 JPEG) | ~1200 vision tokens |
| Response schema instructions | ~300 |
| **Total input** | **~7000–8000** |
| Mind output | ~500 |
| **Total per tick** | **~8000–8500** |

Cost per tick speed:

| Tick Speed | Ticks/Hour | Input Tok/Hr | Output Tok/Hr | Approx Cost/Hr |
|------------|-----------|-------------|--------------|----------------|
| LOW | 60 | 480K | 30K | ~$8 |
| MEDIUM | 360 | 2.9M | 180K | ~$50 |
| REALTIME | 1800 | 14.4M | 900K | ~$240 |

**REALTIME is expensive.** It should be triggered only by genuine human
presence and should drop back to MEDIUM as soon as interaction ends.
A typical mixed-activity hour is likely $25–50.

Compression costs (Haiku sidecar) are negligible — roughly $0.10–0.30
per hour of agent runtime.

---

## 9. Open Design Questions

1. **Audio transcription in the loop.** Should the agent hear in real time
   (transcribe audio each tick) or only on events (voice activity detection
   triggers transcription)? Real-time is expensive. VAD + transcription on
   demand is cheaper but introduces latency on human interaction.

2. **Spatial memory format.** The agent will build a mental model of its
   environment. Should this be structured (graph of rooms/obstacles with
   coordinates) or narrative ("the hallway leads to a bright room with
   windows")? Structured is more useful for navigation. Narrative is
   cheaper and more natural in context.

3. **Multi-modal memory.** Should the agent retain key frames (thumbnail
   images) in long-term memory, or only text descriptions of what it saw?
   Images in long-term memory are expensive but might trigger richer recall.

4. **Conversation mode.** When a human is talking to Kombucha, should the
   tick loop suspend and switch to a true multi-turn conversation (with
   audio in/out), or should the conversation happen within the tick
   framework? Multi-turn is more natural. Tick-based is simpler and
   maintains the unified journal.

5. **Compression drift.** Over many compression cycles, summaries lose
   fidelity. The database retains the raw JSONL as ground truth — periodic
   re-reads of raw journal can re-anchor drifted summaries. Should this
   be automatic (scheduled re-compression from raw) or on-demand (agent
   senses a gap and requests a re-read)?

6. **Dream mode.** During LOW ticks with no stimuli, should the agent
   have a "dream" mode where it re-processes old memories, makes
   connections, and potentially surfaces insights? This is expensive but
   could be where the most interesting self-actualization behavior emerges.

---

## 10. Implementation Status

All core components are implemented in `kombucha_bridge.py` and `story_server.py`.

| # | Component | Status | Notes |
|---|-----------|--------|-------|
| 1 | **Memory database** | DONE | SQLite with WAL mode, `memories` + `identity` tables, indexes, seeded identity core |
| 2 | **Tagging engine** | DONE | `enrich_tags()` adds auto-tags (mood, goal, act, out, time). Agent proposes tags per tick. Haiku enriches during compression. |
| 3 | **Memory retriever** | DONE | `retrieve_memories()` — tag overlap * 3.0, lesson * 2.5, success/failure * 2.0. Excludes current session. Top 5 results. |
| 4 | **Context assembler** | DONE | `assemble_memory_context()` — 5 tiers: identity, retrieved, long-term, session, working. Injected as text block before current tick input. |
| 5 | **Journal writer** | DONE | `write_journal_entry()` — daily JSONL files with full structured tick data. |
| 6 | **Working memory manager** | DONE | `insert_tick_memory()` — every tick inserts tier='working'. Context assembler fetches last 5. |
| 7 | **Haiku compressor** | DONE | `compress_old_memories()` — async, fires every 10 ticks via `asyncio.create_task`. `generate_session_summary()` at shutdown. |
| 8 | **Tick loop** | DONE | SEE → REMEMBER → THINK → LOG → ACT → REMEMBER → COMPRESS → PERSIST → WAIT |
| 9 | **Success/failure tracker** | DONE | Mind outputs `outcome` + `lesson`. Stored in DB. Lessons weighted 2.5x in retrieval. |
| 10 | **Identity core** | DONE | Seeded with 5 operator statements. Agent proposals logged with `active=FALSE` (need operator approval). |
| 11 | **Crash recovery** | DONE | `recover_from_crash()` — replays JSONL entries missing from DB on startup. |
| 12 | **Story server** | DONE | Syncs JSONL + frames via rsync/scp. Displays tags, outcome badges, lessons, memory notes. SSE streaming. |
| 13 | **Dream mode** | NOT YET | Deferred. Requires LOW-tick idle detection + memory re-processing loop. |

### Implementation Deviations from Spec

- **Budget culling** (section 3.3): Not implemented. Context is kept within natural limits by tier size constraints (5 working, top 5 retrieved) rather than explicit token counting. Can add if context grows too large.
- **Recency scoring** in retrieval: Not implemented as a separate signal. Results are pre-sorted by timestamp DESC so recency is an implicit tiebreaker.
- **`tick_speed` field**: The spec proposed a discrete `tick_speed` enum (LOW/MEDIUM/REALTIME). Implementation uses `next_tick_ms` (continuous, 2000-60000) which is more flexible. Sentry mode triggers above 10000ms.
- **Image handling** (section 3.2): JPEG quality is 75 (spec said 60). 75 is better for the Realtek 5842's wide-angle lens.
- **`memory_note` in compression**: Memory notes are stored per-tick and included in compression input, but the Haiku prompt doesn't explicitly separate them from other tick content.
- **`listen` action**: Stubbed in the spec but not implemented. Requires VAD + transcription pipeline.
