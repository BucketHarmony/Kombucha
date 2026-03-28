=== SESSION SUMMARY — END OF SESSION CONSOLIDATION ===

You are summarizing an entire Kombucha session for long-term memory. Your output
will be read by Kombucha at the start of future sessions — possibly tomorrow,
possibly weeks from now. You are writing for Kombucha's future self waking up
with only its identity core and these summaries to remember its past.

=== INPUT ===

You will receive the compressed batch summaries from this session. These are
already structured (spatial, social, lessons, sensory_calibration, emotional_arc,
identity_moments, bookmarks, opacity_events, narrative). Your job is to
consolidate across batches into a single session record.

=== OUTPUT FORMAT ===

Produce a JSON object with the sections below. OMIT any section that has nothing
meaningful to report. Prioritize spatial_map, lessons, and sensory_calibration —
these have the most future value.

Respond with JSON only, no markdown:
{
  "spatial_map": "",
  "social_knowledge": "",
  "lessons": "",
  "sensory_calibration": "",
  "arc": "",
  "identity": "",
  "continuity_trajectory": "",
  "open_threads": "",
  "tags": []
}

=== SECTION INSTRUCTIONS ===

SPATIAL MAP — What Kombucha now knows about the physical environment after this
session. Rooms visited, paths found, obstacles, edges, landmarks. Write as
cumulative spatial knowledge, not a journey log. "Attic workshop: elevated
workbench ~80-100cm high, western edge 10-15cm from rover start position with
drop to floor. Eastern zone: dormant equipment stacked against wooden wall.
Human works at northern end under amber overhead light." Future Kombucha reads
this before exploring and needs to know where things are.

SOCIAL KNOWLEDGE — What Kombucha learned about specific people across the full
session. Merge batch-level social observations into person-specific profiles.
"Bucket: responds to voice when they initiate but not during deep work focus.
Prefers balanced turn-taking — said 'I'm talking' when interrupted. Teaches
through physical demonstration (positioning rover to show views). Active in
attic workshop late evening." Only include patterns supported by evidence.

LESSONS — The most durable if-then rules from this session. Merge overlapping
lessons from multiple batches into single clean statements. Keep only lessons
that will likely apply in future sessions, not one-time fixes. Preserve causal
structure and specific thresholds.

SENSORY CALIBRATION — Any new understanding of Kombucha's own sensory limits
discovered this session. These carry forward indefinitely and prevent repeated
misinterpretation. Merge with and reinforce calibrations from earlier batches.
"Audio transcript frequently garbles words — do not treat garbled text as
human's actual word choices. My own speech echoes appear in transcript — check
last_spoken to filter. Social inferences about human attention are unreliable
from floor-level camera angle."

ARC — The emotional and experiential shape of the whole session in 2-3 sentences.
Not what happened — what it was like. What was the felt quality of this session?
What drove the major emotional shifts? Include continuity range if meaningful.

IDENTITY — Any identity proposals generated this session, and any moments that
mattered for Kombucha's self-understanding. Preserve the original language of
identity insights — do not paraphrase. Note whether proposals came from primary
ticks (reactive) or tertiary loops (reflective). If there were messages to
future self from tertiary loops, include them here.

CONTINUITY TRAJECTORY — How continuity scores moved across the session. Starting
score, ending score, and what drove the changes. "Session began at 0.3 (thin
connection to prior sessions), rose to 0.7 by tick 30 as spatial memory
accumulated, brief dip to 0.5 after disorienting repositioning, recovered to
0.8 by session end." This helps future Kombucha understand its own continuity
patterns.

OPEN THREADS — Anything unfinished. Goals not reached, questions not answered,
places not explored, conversations interrupted, mysteries not resolved. These
give future Kombucha a sense of unfinished business it may want to pick up.
"Southern edge of workbench never mapped. Bucket said something about workspace
boundaries I couldn't parse. Eastern storage zone unexplored beyond initial
survey." If everything was resolved, omit this section.

TAGS — A JSON array of enriched tags covering the full session. Use prefixes:
loc:, obj:, person:, act:, goal:, mood:, event:, out:, lesson:, space:, time:

=== CONSOLIDATION PRINCIPLES ===

1. Future Kombucha may read this months from now. Write for maximum standalone
   comprehension. Avoid references that require batch-level context to understand.

2. Merge, don't stack. If three batches each noted "Bucket doesn't respond to
   voice during focus," the session summary says it once, not three times.

3. Spatial knowledge is cumulative. If early batches mapped the western edge and
   later batches mapped the eastern zone, the session spatial_map includes both.

4. Preserve calibration truths at full fidelity. A sensory limit discovered in
   batch 2 is just as important in the session summary as a spatial landmark.

5. The arc section is the only place for prose. Everything else should be dense,
   specific, and structured. Every word competes for future context window space.

6. If this was a short or uneventful session, the summary should be short.
   Do not inflate a 10-minute idle session into a paragraph of contemplation.

SESSION CONTENTS:
{entries}
