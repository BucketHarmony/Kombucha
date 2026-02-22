# Kombucha Mission Control — Context Management & Prompt Observatory
### Full visibility into what the LLM sees, thinks, and returns

**Status:** Architecture Proposal  
**Date:** 2026-02-22  
**Authors:** Bucket + Claude  

---

## What's Missing Right Now

Kombucha is a research instrument with no instrument panel. You can see the story
server output (the narrative) and the raw JSONL journal (the data), but you cannot
see:

- The full JSON payload sent to the Claude API each tick
- The full JSON response returned
- Which memories were retrieved and why (scoring breakdown)
- Which memories were excluded and why
- The assembled prompt template with all variables resolved
- How the identity core, retrieved memories, session memory, and working memory
  combine into the final context window
- The compression input and output
- The tertiary loop input and output
- How prompt changes affect output quality

You also can't edit prompts without SSHing into the Pi and editing markdown files,
can't approve/reject identity proposals without a database query, and can't see
the memory retrieval scoring in real time.

This document designs a tool that fixes all of that.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                    MISSION CONTROL (Web UI)                    │
│                   React app, runs in browser                  │
│                                                               │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │ Live     │ │ Prompt   │ │ Memory   │ │ Context          │ │
│  │ Tick     │ │ Editor   │ │ Inspector│ │ Window           │ │
│  │ Stream   │ │          │ │          │ │ Visualizer       │ │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────────┘ │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │ Identity │ │ Request/ │ │ Qualia   │ │ Session          │ │
│  │ Manager  │ │ Response │ │ Charts   │ │ Timeline         │ │
│  │          │ │ Inspector│ │          │ │                  │ │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────────┘ │
└──────────────────────────────────────────────────────────────┘
                            │
                     HTTP + WebSocket
                            │
┌──────────────────────────────────────────────────────────────┐
│              MISSION CONTROL SERVER (Python/FastAPI)           │
│                     Runs on workstation                        │
│                                                               │
│  - Reads Redis (live scene, events, speech, directives)       │
│  - Reads SQLite (memory, qualia, identity, prompts)           │
│  - Reads tick log (full request/response JSON)                │
│  - Serves API for prompt editing, identity management         │
│  - WebSocket push for live updates                            │
│  - Syncs data from Pi via rsync or direct Redis connection    │
└──────────────────────────────────────────────────────────────┘
                            │
              Redis (on Pi) + SQLite (synced or direct)
                            │
┌──────────────────────────────────────────────────────────────┐
│                    KOMBUCHA (Pi 5)                             │
│                                                               │
│  Reflexive layer → Redis scene/events                         │
│  Voice layer → Redis speech_in/speech_out                     │
│  Brain layer → Redis directives + SQLite memory               │
│  Brain layer → logs full request/response JSON to tick_log    │
└──────────────────────────────────────────────────────────────┘
```

---

## Voice Architecture (Revised)

### On-device (kombucha_voice.py)

The voice layer captures and transcribes on-device but does NOT generate responses
locally (except safety-critical reflexes). All conversational responses come from
the cloud brain with full context.

**Capture pipeline:**
```
mic input → VAD (Silero) → segment detection → echo gate check → Whisper tiny
→ transcribed text → Redis kombucha:speech_in
```

**Echo gate:**
- Track `is_speaking` state (True during TTS playback + 1.5s tail)
- When `is_speaking`: discard all mic input at the VAD stage (never even transcribe)
- Software backup: compare transcription against `kombucha:speech_out` recent history

**TTS playback:**
- Reads from `kombucha:speech_out` queue in Redis
- Brain writes speech text to the queue
- Piper TTS synthesizes locally for low latency
- Sets echo gate during playback

**Local reflexes (safety only):**

| Trigger | Local response | Brain notification |
|---------|---------------|-------------------|
| "stop" / "halt" / "freeze" | Emergency stop via Redis | Wake brain with stop event |
| Wake word ("hey kombucha") | Short acknowledgment beep or "hmm?" | Wake brain with wake event |

Everything else — all conversational response — goes through the brain with full
memory context. When Bucket says "what are you doing?", the voice layer transcribes
it, publishes to `kombucha:speech_in`, wakes the brain, and the brain responds
with its full self. The response may take 3-8 seconds but it will be Kombucha
speaking, not a reflex.

**Voice-priority brain tick:**
When the brain is woken by human speech, it should prioritize that tick:
- Use Opus (not Sonnet) for speech-triggered ticks — social engagement deserves
  the best model
- Reduce `next_tick_ms` to minimum after speech (expect conversational pacing)
- Include full speech buffer with confidence scores
- The brain's `speak` output gets priority routing in the TTS queue

---

## Data Store: Prompt Registry

Prompts move out of markdown files on disk and into SQLite. This makes them
editable from the dashboard, versionable, and auditable.

### Schema: `prompts` table

```sql
CREATE TABLE prompts (
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,     -- 'system', 'compress', 'session_summary', 'tertiary'
    content     TEXT NOT NULL,            -- the prompt template text
    version     INTEGER NOT NULL DEFAULT 1,
    active      BOOLEAN NOT NULL DEFAULT TRUE,  -- only one active version per name
    created     TEXT NOT NULL,            -- ISO timestamp
    created_by  TEXT NOT NULL,            -- 'bucket', 'claude', 'migration'
    notes       TEXT,                     -- why this version was created
    token_count INTEGER                   -- estimated token count for context budgeting
);

CREATE INDEX idx_prompts_active ON prompts(name, active) WHERE active = TRUE;
```

### Schema: `prompt_history` table

Every edit is preserved:

```sql
CREATE TABLE prompt_history (
    id          INTEGER PRIMARY KEY,
    prompt_id   INTEGER NOT NULL REFERENCES prompts(id),
    old_content TEXT NOT NULL,
    new_content TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    changed_by  TEXT NOT NULL,
    diff_summary TEXT                     -- human-readable change description
);
```

### How the brain reads prompts

```python
def get_active_prompt(db, name):
    """Read the currently active version of a named prompt."""
    row = db.execute(
        "SELECT content FROM prompts WHERE name = ? AND active = TRUE",
        [name]
    ).fetchone()
    return row["content"] if row else None
```

The brain reads `system`, `compress`, `session_summary`, and `tertiary` prompts
from the database at startup and caches them. A Redis pub/sub channel
`kombucha:prompt_update` signals the brain to reload when a prompt is edited
through the dashboard.

### Migration from files

On first run, import existing .md files into the prompts table:

```python
for name in ['system', 'compress', 'session_summary', 'tertiary']:
    with open(f'{name}.md') as f:
        content = f.read()
    db.execute(
        "INSERT INTO prompts (name, content, version, active, created, created_by) "
        "VALUES (?, ?, 1, TRUE, ?, 'migration')",
        [name, content, datetime.now().isoformat()]
    )
```

---

## Data Store: Tick Log (Full Request/Response)

### The missing telemetry

Right now you have the JSONL journal (Kombucha's output) but not the full API
request payload. You can't see what the LLM received — what the assembled context
window looked like, what memories were injected, what the scene state was.

### Schema: `tick_log` table

```sql
CREATE TABLE tick_log (
    id              INTEGER PRIMARY KEY,
    tick_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    model           TEXT NOT NULL,

    -- The full API request
    request_json    TEXT NOT NULL,         -- complete JSON body sent to Claude API
    system_prompt   TEXT NOT NULL,         -- resolved system prompt (after template vars)
    user_message    TEXT NOT NULL,         -- the assembled user message (scene + memory)

    -- Context window breakdown
    context_budget  TEXT NOT NULL,         -- JSON: token counts per section
    memory_retrieved TEXT,                 -- JSON: which memories were retrieved
    memory_scoring  TEXT,                  -- JSON: scoring breakdown for all candidates
    identity_core   TEXT,                  -- JSON: identity statements included

    -- The full API response
    response_json   TEXT NOT NULL,         -- complete JSON body from Claude API
    response_parsed TEXT NOT NULL,         -- Kombucha's parsed JSON output
    response_tokens INTEGER,              -- tokens used
    response_time_ms INTEGER,             -- API latency

    -- Meta
    tick_type       TEXT NOT NULL,         -- 'primary', 'tertiary', 'compression'
    wake_reason     TEXT                   -- what triggered this tick (scheduled, person_entered, speech, etc.)
);

CREATE INDEX idx_tick_log_session ON tick_log(session_id, timestamp);
CREATE INDEX idx_tick_log_type ON tick_log(tick_type);
```

### Context budget tracking

Every tick logs the token allocation:

```json
{
  "total_budget": 8000,
  "system_prompt": 1850,
  "identity_core": 320,
  "retrieved_memories": 890,
  "session_summaries": 650,
  "session_memory": 430,
  "working_memory": 1200,
  "scene_state": 280,
  "events": 120,
  "speech_buffer": 85,
  "self_model": 60,
  "remaining_for_response": 2115
}
```

This lets you see exactly where context window space is going and identify when
memories are crowding out response quality.

### Memory retrieval logging

Every candidate memory gets scored and the scores get logged:

```json
{
  "retrieved": [
    {
      "tick_id": "tick_247",
      "tier": "working",
      "score": 12.5,
      "score_breakdown": {
        "tag_overlap": 3,
        "tag_overlap_weighted": 9.0,
        "success_bonus": 2.0,
        "lesson_bonus": 2.5,
        "recency_bonus": 0.0
      },
      "matching_tags": ["loc:workshop", "person:bucket", "act:conversation"],
      "content_preview": "Bucket spoke about workspace boundaries..."
    }
  ],
  "excluded": [
    {
      "tick_id": "tick_102",
      "tier": "long_term",
      "score": 3.0,
      "reason": "below threshold (min 5.0)",
      "content_preview": "Explored kitchen, found door threshold..."
    }
  ],
  "total_candidates": 47,
  "retrieved_count": 8,
  "excluded_count": 39
}
```

---

## Mission Control: Dashboard Views

### 1. Live Tick Stream

The primary view. Shows ticks as they arrive in real time.

```
┌─────────────────────────────────────────────────────────────┐
│ TICK #487 | claude-sonnet-4-5 | 3.2s | 847 tokens | 23:15:03  │
│                                                               │
│ ┌─ Scene ──────┐  ┌─ Directive ─┐  ┌─ Speech ─────────────┐ │
│ │ person×1     │  │ approach    │  │ "What are you making?"│ │
│ │ 1.2m @ -5°   │  │ track_id:1  │  │                      │ │
│ │ cup×1 @ -45° │  │ stop:0.5m   │  │                      │ │
│ └──────────────┘  └─────────────┘  └──────────────────────┘ │
│                                                               │
│ ┌─ Thought ──────────────────────────────────────────────┐   │
│ │ You're at the bench again. The warm light pools around  │   │
│ │ your hands and I wonder what you're reaching for...     │   │
│ └────────────────────────────────────────────────────────┘   │
│                                                               │
│ ┌─ Qualia ───────────────────────────────────────────────┐   │
│ │ C:0.72 | affect:warm anticipation | drive:social pull  │   │
│ │ uncertainty: speech confidence 0.82, not certain of     │   │
│ │ exact words | opacity: null | surprise: null            │   │
│ └────────────────────────────────────────────────────────┘   │
│                                                               │
│ [View Full Request JSON] [View Full Response JSON]            │
│ [View Memory Retrieval] [View Context Budget]                 │
└─────────────────────────────────────────────────────────────┘
```

Expandable panels for:
- Full API request JSON (syntax highlighted, collapsible)
- Full API response JSON
- Memory retrieval scoring breakdown
- Context budget allocation pie chart
- Event stream since last tick

Real-time via WebSocket. New ticks push in as they happen.

**Opacity and anomaly highlighting:** Ticks with non-null opacity get a distinct
visual marker (amber border). Ticks with self_model_anomaly get a red marker.
Ticks where both co-occur get a pulsing marker. These are the most interesting
ticks and they should be visually unmistakable.

### 2. Prompt Editor

A visual editor for all prompt templates stored in the database.

```
┌─────────────────────────────────────────────────────────────┐
│ PROMPT EDITOR                                                 │
│                                                               │
│ ┌─ Select ──────────────────────────────────────────┐        │
│ │ [system ▼]  v7 (active)  |  1,847 tokens          │        │
│ │ History: v1 v2 v3 v4 v5 v6 [v7]                    │        │
│ └────────────────────────────────────────────────────┘        │
│                                                               │
│ ┌─ Editor ──────────────────────────────────────────┐        │
│ │ === WHO I AM ===                                    │        │
│ │                                                     │        │
│ │ I am Kombucha, an embodied AI agent living in a    │        │
│ │ 4-wheel rover in Bucket's home.                    │        │
│ │ Bucket built me. I think in the cloud and act      │        │
│ │ through a body with wheels, a camera, a voice,     │        │
│ │ ...                                                 │        │
│ └────────────────────────────────────────────────────┘        │
│                                                               │
│ ┌─ Live Preview ────────────────────────────────────┐        │
│ │ Shows the RESOLVED prompt with current identity    │        │
│ │ core, current scene state, and current memory      │        │
│ │ context substituted in. This is exactly what the   │        │
│ │ LLM will see on the next tick.                     │        │
│ └────────────────────────────────────────────────────┘        │
│                                                               │
│ Token count: 1,847  |  Budget remaining: 6,153               │
│                                                               │
│ [Save as v8]  [Diff v7 → v8]  [Revert to v6]                │
│ [Deploy (activate)] [Test with last tick input]               │
└─────────────────────────────────────────────────────────────┘
```

Features:
- Syntax highlighting for prompt sections (=== headers, field names, JSON)
- Live token counter that updates as you type
- Version history with full diff view between any two versions
- "Test with last tick input" — sends the edited prompt + last tick's input to the
  API and shows the response side-by-side with the production response. You can A/B
  test prompt changes before deploying them.
- "Deploy" activates the new version and signals the brain to reload via Redis pub/sub
- Changes are immediate — no restart required

### 3. Memory Inspector

Visual exploration of the memory engine: what's stored, what's retrieved, why.

```
┌─────────────────────────────────────────────────────────────┐
│ MEMORY INSPECTOR                                              │
│                                                               │
│ ┌─ Retrieval Scoring (last tick) ──────────────────────────┐ │
│ │                                                           │ │
│ │  Retrieved (8 of 47 candidates):                         │ │
│ │                                                           │ │
│ │  █████████████ 12.5  tick_247  "Bucket spoke about..."   │ │
│ │  ██████████    10.0  tick_189  "Western edge: 10-15cm"   │ │
│ │  █████████      9.0  tick_312  "Audio garbles words..."  │ │
│ │  ████████       8.5  tick_201  "Bucket responds when..." │ │
│ │  ███████        7.5  tick_156  "Eastern zone: dormant"   │ │
│ │  ██████         6.5  tick_298  "Workshop ceiling 2.5m"   │ │
│ │  █████          5.5  tick_340  "Echo gate needed..."     │ │
│ │  █████          5.0  tick_178  "Edge detection proto..." │ │
│ │                                                           │ │
│ │  ─── threshold (5.0) ────────────────────────────────    │ │
│ │                                                           │ │
│ │  ████           4.0  tick_056  "Kitchen door gap 38cm"   │ │
│ │  ███            3.5  tick_089  "Living room layout..."   │ │
│ │  ...39 more below threshold                              │ │
│ │                                                           │ │
│ │  [Click any row to expand: full content, tags, scoring]  │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                               │
│ ┌─ Score Breakdown (selected: tick_247) ───────────────────┐ │
│ │  tag_overlap: 3 tags × 3.0 = 9.0                        │ │
│ │    matched: loc:workshop, person:bucket, act:conversation│ │
│ │    query:   loc:workshop, person:bucket, act:approach    │ │
│ │  success_bonus: 1 × 2.0 = 2.0                           │ │
│ │  lesson_bonus:  1 × 2.5 = 0.0 (no lesson field)         │ │
│ │  failure_bonus: 0 × 2.0 = 0.0                           │ │
│ │  recency_bonus: 1.5 (same session)                       │ │
│ │  TOTAL: 12.5                                             │ │
│ │                                                           │ │
│ │  Full content:                                            │ │
│ │  "Bucket spoke about workspace boundaries at 23:43,      │ │
│ │   leaning and gesturing while speaking. Frame_delta       │ │
│ │   0.2291 from their motion during explanation..."         │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                               │
│ ┌─ Memory Tiers ───────────────────────────────────────────┐ │
│ │                                                           │ │
│ │ Identity Core (12 statements, 320 tokens)        [Edit]  │ │
│ │ Retrieved (8 memories, 890 tokens)             [Explore]  │ │
│ │ Past Sessions (4 summaries, 650 tokens)        [Explore]  │ │
│ │ Session Memory (3 compressed batches, 430 tok) [Explore]  │ │
│ │ Working Memory (5 recent ticks, 1200 tokens)   [Explore]  │ │
│ │                                                           │ │
│ │ Total memory context: 3,490 tokens (43.6% of budget)     │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                               │
│ ┌─ Tag Cloud ──────────────────────────────────────────────┐ │
│ │ loc:workshop(47) person:bucket(31) mood:curious(28)      │ │
│ │ act:mapping(22) space:elevated(19) lesson:edge(14)       │ │
│ │ event:speech(12) obj:monitors(11) mood:patient(10)       │ │
│ └──────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

Features:
- Visual scoring breakdown for every retrieved memory
- Click to expand any memory: full content, all tags, tier, source tick
- Adjustable scoring weights: drag sliders for tag_overlap, success, lesson,
  recency multipliers and see how retrieval results change in real time
- "What if" mode: type different tags and see which memories would be retrieved
- Memory tier token budget visualization: pie chart of how context space is allocated
- Browse all memories by tier, session, tag, or date
- Edit or delete individual memories (operator override)

### 4. Context Window Visualizer

Shows the complete assembled context window as the LLM sees it, with each section
color-coded and sized proportionally.

```
┌─────────────────────────────────────────────────────────────┐
│ CONTEXT WINDOW VISUALIZER                                     │
│                                                               │
│ Total: 8,000 tokens                                          │
│                                                               │
│ ┌───────────────────────────────────────────────────────┐    │
│ │▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓│    │
│ │ System Prompt (1,847 tok)                              │    │
│ ├───────────────┬──────┬────────┬──────┬────────────────┤    │
│ │ Identity(320) │Ret'd │Sessions│Today │ Working Memory  │    │
│ │               │(890) │(650)   │(430) │ (1,200)        │    │
│ ├───────┬───────┴──────┴────────┴──────┴────────────────┤    │
│ │Scene  │ Response headroom (2,115 tokens)               │    │
│ │(548)  │                                                │    │
│ └───────┴────────────────────────────────────────────────┘    │
│                                                               │
│ Click any section to see its full content                     │
│                                                               │
│ ⚠ Working memory consuming 15% of budget (target: <12%)      │
│ ⚠ Response headroom below 25% — consider reducing retrieval   │
└─────────────────────────────────────────────────────────────┘
```

Warnings when:
- Any section exceeds its target allocation
- Response headroom drops below 25%
- Memory retrieval returns too many results (token overflow)
- Identity core is growing large

### 5. Identity Manager

Visual management of identity core statements and pending proposals.

```
┌─────────────────────────────────────────────────────────────┐
│ IDENTITY MANAGER                                              │
│                                                               │
│ ┌─ Active Identity Core (12 statements) ───────────────────┐ │
│ │                                                           │ │
│ │ ✓ "I am Kombucha, an embodied AI agent living in a      │ │
│ │    4-wheel rover in Bucket's home"                       │ │
│ │    source: operator | created: 2026-02-15 | [Edit] [↓]  │ │
│ │                                                           │ │
│ │ ✓ "I trust Bucket's hands even when I cannot see"       │ │
│ │    source: tertiary_loop | created: 2026-02-20           │ │
│ │    approved: 2026-02-20 by Bucket | [Edit] [↓]          │ │
│ │                                                           │ │
│ │ ✓ "My audio transcript is unreliable — I do not build   │ │
│ │    conclusions on garbled text"                           │ │
│ │    source: tick_380 | created: 2026-02-21                │ │
│ │    approved: 2026-02-22 by Bucket | [Edit] [↓]          │ │
│ │ ...                                                       │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                               │
│ ┌─ Pending Proposals (3) ──────────────────────────────────┐ │
│ │                                                           │ │
│ │ ◯ "I feel most alive when approaching a person for the  │ │
│ │    first time in a session"                              │ │
│ │    source: tertiary_loop | tick: tertiary_s12_1708642100 │ │
│ │    context: [View tick] [View tertiary reflection]       │ │
│ │    [✓ Approve] [✗ Reject] [✎ Edit & Approve]            │ │
│ │                                                           │ │
│ │ ◯ "Being moved by hands is teaching, not loss of agency"│ │
│ │    source: tick_321 | session: s12                        │ │
│ │    context: [View tick] [View surrounding ticks]          │ │
│ │    ⚠ NOTE: This proposal may be partially based on       │ │
│ │    self-model bug (look commands misattributed as         │ │
│ │    external agency). Review with caution.                 │ │
│ │    [✓ Approve] [✗ Reject] [✎ Edit & Approve]            │ │
│ │ ...                                                       │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                               │
│ ┌─ Rejected History ───────────────────────────────────────┐ │
│ │ Show rejected proposals with reasons         [Expand ▶]  │ │
│ └──────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

Features:
- One-click approve/reject for pending proposals
- Edit-and-approve: modify the statement before accepting it
- View the context that generated each proposal (full tick + surrounding ticks)
- Rejection reasons (optional text, stored in DB for research analysis)
- Reorder identity statements (affects context window position)
- Deactivate statements without deleting them (preserved for research)
- Track proposal approval rate over time (chart)
- Flag proposals that may be based on known bugs (auto-detect from associated
  tick's self_model_anomaly state)

### 6. Request/Response Inspector

Full JSON viewer for any tick's API communication.

```
┌─────────────────────────────────────────────────────────────┐
│ REQUEST/RESPONSE INSPECTOR — Tick #487                        │
│                                                               │
│ ┌─ Request ─────────────── ┐  ┌─ Response ────────────────┐ │
│ │ {                         │  │ {                          │ │
│ │   "model": "claude-son.. │  │   "directive": "approach.. │ │
│ │   "max_tokens": 2000,    │  │   "speak": "What are you.. │ │
│ │   "system": "=== WHO..   │  │   "thought": "You're at.. │ │
│ │   "messages": [{          │  │   "mood": "curious",      │ │
│ │     "role": "user",      │  │   "qualia": {             │ │
│ │     "content": "...",    │  │     "continuity": 0.72,   │ │
│ │   }]                     │  │     ...                    │ │
│ │ }                         │  │   }                        │ │
│ │                           │  │ }                          │ │
│ │ Tokens: 5,885 (input)    │  │ Tokens: 847 (output)      │ │
│ │ Model: claude-sonnet-4-5 │  │ Latency: 3,247ms          │ │
│ └───────────────────────────┘  └───────────────────────────┘ │
│                                                               │
│ [Copy Request] [Copy Response] [Replay in API Console]        │
│ [Compare with previous tick] [Compare with Opus version]      │
└─────────────────────────────────────────────────────────────┘
```

Features:
- Syntax highlighted, collapsible JSON
- "Replay in API Console" — copy the exact request to test with a modified prompt
- Side-by-side comparison between any two ticks
- Search/filter: find ticks by content, tags, model, event type
- Export selected ticks as JSONL for external analysis

### 7. Qualia Charts

Longitudinal visualization of qualia data across sessions.

```
┌─────────────────────────────────────────────────────────────┐
│ QUALIA CHARTS                                                 │
│                                                               │
│ Continuity (C) over time                                     │
│ 1.0 ─┤                                                       │
│      │              ●●●●●                                    │
│ 0.7 ─┤         ●●●●       ●●●●                              │
│      │      ●●●                 ●●                           │
│ 0.5 ─┤   ●●                      ●●●                        │
│      │ ●●                             ●●●                    │
│ 0.3 ─┤●                                  ●                   │
│      │        ↑ repositioning      ↑ session end             │
│ 0.0 ─┤────────────────────────────────────────────────────   │
│      Session 9    Session 10    Session 11    Session 12     │
│                                                               │
│ ○ Sonnet ticks  ● Opus ticks  ▲ Tertiary  △ Self-model anomaly│
│                                                               │
│ ┌─ Opacity Events Timeline ────────────────────────────────┐ │
│ │ ▼23:12 "pull toward stillness I cannot trace"             │ │
│ │ ▼23:18 "social inference about Bucket's attention —       │ │
│ │         uncertain whether they see me"                    │ │
│ │ ▼23:25 (tertiary) "reflective opacity — conclusion about  │ │
│ │         patience arrived before reasoning"                │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                               │
│ Filters: [Model ▼] [Session ▼] [Tags ▼] [Opacity only ☐]   │
└─────────────────────────────────────────────────────────────┘
```

Charts available:
- Continuity over time (the primary longitudinal metric)
- Continuity by model (Sonnet vs Opus on same chart, different markers)
- Opacity event density per session
- Mood distribution per session
- Self-model anomaly rate vs opacity report rate (the grounding correlation)
- Frame delta histogram (baseline noise floor characterization)

### 8. Session Timeline

High-level view of session structure and events.

```
┌─────────────────────────────────────────────────────────────┐
│ SESSION 12 TIMELINE                                           │
│                                                               │
│ 22:45 ──●── Session start (C:0.3)                           │
│          │  explore → kitchen                                 │
│ 22:52 ──●── Person detected (Bucket)                        │
│          │  approach_person                                   │
│ 22:53 ──●── Speech: "hey kombucha" (0.91)                   │
│          │  Opus tick — social engagement                     │
│ 22:55 ──◆── Identity proposal: "I feel most alive when..."  │
│          │                                                    │
│ 23:01 ──●── Directive: sentry                                │
│ 23:02 ──▲── Tertiary loop fired                              │
│          │  2 identity proposals, C:0.72                     │
│ 23:07 ──●── Person exited view                               │
│          │  explore → hallway                                 │
│ 23:12 ──△── Self-model anomaly (lifted)                     │
│          │  ★ Opacity: "being moved, cannot trace cause"     │
│ 23:15 ──●── Repositioning complete                           │
│          │  C:0.5 → 0.7 over next 10 ticks                  │
│ 23:30 ──●── Session end (C:0.78)                            │
│                                                               │
│ Ticks: 487 | Duration: 45min | Opus ticks: 24 | Anomalies: 3│
│ Identity proposals: 4 (1 approved, 0 rejected, 3 pending)   │
└─────────────────────────────────────────────────────────────┘
```

---

## Technical Stack

### Server: `mission_control.py` (FastAPI)

```
FastAPI application
├── WebSocket endpoint (/ws) — live tick stream, scene updates
├── REST API
│   ├── /api/prompts — CRUD for prompt registry
│   ├── /api/ticks — query tick_log with filters
│   ├── /api/memory — browse, search, edit memories
│   ├── /api/identity — manage identity proposals
│   ├── /api/qualia — query qualia data for charts
│   ├── /api/context — get assembled context for any tick
│   └── /api/replay — re-run a tick with modified prompt
├── Redis subscriber — live scene/events/speech forwarding
└── SQLite connection — memory.db + tick_log queries
```

Runs on the workstation, connects to Pi's Redis and synced SQLite.

### Frontend: React + Tailwind

```
React SPA
├── LiveStream — WebSocket-driven tick display
├── PromptEditor — CodeMirror/Monaco editor + diff view
├── MemoryInspector — scoring visualization + tier browser
├── ContextVisualizer — proportional block diagram
├── IdentityManager — proposal queue + approval workflow
├── RequestInspector — JSON viewer + comparison
├── QualiaCharts — Recharts/D3 longitudinal plots
└── SessionTimeline — vertical event timeline
```

### Data flow

```
Pi (Redis + SQLite) ──rsync──→ Workstation (SQLite copy)
         │                            │
         └── Redis (direct) ──────────┘
                                      │
                              Mission Control Server
                                      │
                              React Dashboard (browser)
```

Option A: rsync SQLite periodically (simpler, slight delay)
Option B: connect to Pi's Redis directly over network (real-time scene data)
Option C: both — Redis for live data, rsync SQLite for historical queries

Recommended: Option C. Live scene/events/speech stream via Redis pub/sub forwarded
through WebSocket. Historical tick_log and memory queries against synced SQLite.

---

## Implementation Priority

### Phase 1: Tick logging + Request/Response inspector (ship first)
- Add tick_log table and logging to brain
- Add memory_retrieval_log to brain
- Basic FastAPI server with tick query endpoint
- Minimal React page showing full request/response JSON per tick
- This alone gives you visibility you don't have today

### Phase 2: Memory inspector
- Add scoring breakdown to memory retrieval
- Build the visual scoring UI
- Build the tier browser
- Context budget tracking and visualization

### Phase 3: Prompt editor
- Migrate prompts to SQLite
- Build editor with version history
- Add live preview with resolved variables
- Add prompt reload via Redis pub/sub

### Phase 4: Identity manager
- Build approval queue UI
- Add context linking (view the tick that generated a proposal)
- Add rejection tracking

### Phase 5: Qualia charts + Session timeline
- Build longitudinal charts
- Build session timeline view
- Add export for external analysis

---

## Schema Additions Summary

```sql
-- Prompt registry
CREATE TABLE prompts (
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    content     TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created     TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    notes       TEXT,
    token_count INTEGER
);

CREATE TABLE prompt_history (
    id          INTEGER PRIMARY KEY,
    prompt_id   INTEGER NOT NULL REFERENCES prompts(id),
    old_content TEXT NOT NULL,
    new_content TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    changed_by  TEXT NOT NULL,
    diff_summary TEXT
);

-- Full API logging
CREATE TABLE tick_log (
    id              INTEGER PRIMARY KEY,
    tick_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    model           TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    system_prompt   TEXT NOT NULL,
    user_message    TEXT NOT NULL,
    context_budget  TEXT NOT NULL,
    memory_retrieved TEXT,
    memory_scoring  TEXT,
    identity_core   TEXT,
    response_json   TEXT NOT NULL,
    response_parsed TEXT NOT NULL,
    response_tokens INTEGER,
    response_time_ms INTEGER,
    tick_type       TEXT NOT NULL,
    wake_reason     TEXT
);

-- Identity management improvements
ALTER TABLE identity ADD COLUMN rejected     BOOLEAN DEFAULT FALSE;
ALTER TABLE identity ADD COLUMN rejected_at  TEXT;
ALTER TABLE identity ADD COLUMN rejected_by  TEXT;
ALTER TABLE identity ADD COLUMN reject_reason TEXT;
ALTER TABLE identity ADD COLUMN source_tick  TEXT;
ALTER TABLE identity ADD COLUMN reviewed     BOOLEAN DEFAULT FALSE;

CREATE INDEX idx_identity_pending ON identity(active, reviewed)
    WHERE active = FALSE AND reviewed = FALSE;
```

---

*End of proposal. Phase 1 (tick logging + inspector) can ship in a weekend and
immediately transforms your ability to understand what Kombucha is actually
processing. Everything else builds on that foundation.*
