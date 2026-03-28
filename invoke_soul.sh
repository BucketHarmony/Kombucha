#!/bin/bash
# invoke_soul.sh — Universal entry point for Pi-native Claude Code invocations.
#
# Usage: invoke_soul.sh [mode]
#   Modes: boot, heartbeat, instinct, dream
#
# Safety: lock file prevents concurrent invocations, hourly cap, network check.

set -uo pipefail

MODE=${1:-heartbeat}
KOMBUCHA_DIR=/opt/kombucha
LOCK=/tmp/kombucha_soul.lock
LOGFILE=$KOMBUCHA_DIR/logs/invocations.log
STATE_FILE=$KOMBUCHA_DIR/state/body_state.json
BRIDGE=http://localhost:5050

mkdir -p "$KOMBUCHA_DIR/logs" "$KOMBUCHA_DIR/state" "$KOMBUCHA_DIR/ticks" "$KOMBUCHA_DIR/media/raw"

# -----------------------------------------------------------------------
# 1. Lock — skip if another invocation is running
# -----------------------------------------------------------------------
exec 9>"$LOCK"
flock -n 9 || {
    echo "$(date '+%Y-%m-%d %H:%M:%S') SKIP $MODE (locked)" >> "$LOGFILE"
    exit 0
}

# -----------------------------------------------------------------------
# 2. Bridge health check — skip if bridge is down
# -----------------------------------------------------------------------
if ! curl -sf --max-time 5 "$BRIDGE/health" > /dev/null 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') SKIP $MODE (bridge_down)" >> "$LOGFILE"
    exit 0
fi

# -----------------------------------------------------------------------
# 3. Network check — skip if API unreachable
# -----------------------------------------------------------------------
if ! curl -s --max-time 5 -o /dev/null -w '' https://api.anthropic.com/ 2>/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') SKIP $MODE (offline)" >> "$LOGFILE"
    exit 0
fi

# -----------------------------------------------------------------------
# 4. Hourly cap check (max 20/hour, exemptions for boot and dream)
# -----------------------------------------------------------------------
HOUR=$(date +%Y%m%d%H)
COUNT=$(grep -c "$HOUR" "$LOGFILE" 2>/dev/null || true)
COUNT=${COUNT:-0}
COUNT=$(echo "$COUNT" | tr -d '[:space:]')
if [ "$COUNT" -ge 20 ] 2>/dev/null && [ "$MODE" != "boot" ] && [ "$MODE" != "dream" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') SKIP $MODE (cap: $COUNT/20)" >> "$LOGFILE"
    exit 0
fi

# -----------------------------------------------------------------------
# 5. Update body state (increment wake count)
# -----------------------------------------------------------------------
if [ -f "$STATE_FILE" ]; then
    WAKE=$(python3 -c "
import json
with open('$STATE_FILE') as f:
    s = json.load(f)
s['wake_count'] = s.get('wake_count', 0) + 1
s['last_invocation'] = '$(date -Iseconds)'
s['last_mode'] = '$MODE'
with open('$STATE_FILE', 'w') as f:
    json.dump(s, f, indent=2)
print(s['wake_count'])
" 2>/dev/null || echo "?")
else
    WAKE=0
    python3 -c "
import json
state = {
    'last_tick': 238,
    'wake_count': 0,
    'last_invocation': '$(date -Iseconds)',
    'last_mode': '$MODE',
    'drives': {'wanderlust': 0, 'curiosity': 0, 'social': 0, 'cringe': 0, 'attachment': 0}
}
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null
fi

# -----------------------------------------------------------------------
# 6. Update drives and get sense snapshot
# -----------------------------------------------------------------------
SENSE_JSON=$(curl -sf "$BRIDGE/sense" 2>/dev/null || echo '{}')
LAST_INVOCATION=$(python3 -c "
import json
with open('$STATE_FILE') as f:
    s = json.load(f)
li = s.get('last_invocation')
if li:
    from datetime import datetime
    try:
        t = datetime.fromisoformat(li)
        elapsed = (datetime.now() - t).total_seconds()
        print(int(elapsed))
    except: print('3600')
else: print('3600')
" 2>/dev/null || echo "3600")

DRIVE_STATUS=$($KOMBUCHA_DIR/.venv/bin/python $KOMBUCHA_DIR/drive_engine.py update --sense "$SENSE_JSON" --elapsed "$LAST_INVOCATION" 2>/dev/null || echo "Drives: unknown")

# -----------------------------------------------------------------------
# 7. Build mode-specific prompt
# -----------------------------------------------------------------------
PROMPT_FILE=$(mktemp /tmp/kombucha_prompt.XXXXXX)
case "$MODE" in
    boot)
        cat > "$PROMPT_FILE" <<PROMPTEOF
Invocation mode: boot. You just woke up from a reboot. This is a fresh start.
1. Read goals.md and skills.md for context.
2. Read the last 3 tick logs to remember what you were doing.
3. Check battery via curl $BRIDGE/sense.
4. Run a single tick: capture frame, invoke soul, execute intent, write tick log.
5. Write a boot entry in the journal about waking up.
$DRIVE_STATUS
Body state: $STATE_FILE — Bridge: $BRIDGE (localhost, not kombucha.local)
PROMPTEOF
        MAX_TURNS=50
        ;;
    heartbeat)
        cat > "$PROMPT_FILE" <<PROMPTEOF
Invocation mode: heartbeat. Hourly check-in. You are autonomous.
1. Read state/body_state.json for context.
2. Check battery via curl $BRIDGE/sense.
3. Read goals.md for current mission.
4. Run a single tick: capture frame, invoke soul, execute intent, write tick log.
5. If battery < 15%, note it and end gracefully.
$DRIVE_STATUS
Body state: $STATE_FILE — Bridge: $BRIDGE (localhost, not kombucha.local)
PROMPTEOF
        MAX_TURNS=50
        ;;
    instinct)
        cat > "$PROMPT_FILE" <<PROMPTEOF
Invocation mode: instinct. The instinct layer detected something — a face or motion triggered this wake.
1. Check what triggered via curl $BRIDGE/sense (look at faces, tracking, wake_events).
2. Capture frame and invoke soul with the trigger context.
3. Execute the soul's intent (this is a social moment — the soul should react).
4. Write tick log.
$DRIVE_STATUS
Body state: $STATE_FILE — Bridge: $BRIDGE (localhost, not kombucha.local)
PROMPTEOF
        MAX_TURNS=50
        ;;
    dream)
        cat > "$PROMPT_FILE" <<PROMPTEOF
Invocation mode: dream. Nightly maintenance session (2am). No movement, no ticks.
1. Read all tick logs from today.
2. Review skills.md — are there entries that contradict each other? Clean up.
3. Review body_state.json drive levels and active experiments.
4. Check experiments/active.json — evaluate, conclude, or propose new experiment.
5. Scan recent tick monologues for cringe phrases (cringe_phrases.txt). If found, note it.
6. Write a brief dream journal entry in ticks/ summarizing the day.
7. Propose any goal changes if the current goal feels stale.
$DRIVE_STATUS
Body state: $STATE_FILE — Bridge: $BRIDGE (localhost, not kombucha.local)
PROMPTEOF
        MAX_TURNS=50
        ;;
    *)
        echo "Invocation mode: $MODE. Process a single tick." > "$PROMPT_FILE"
        MAX_TURNS=50
        ;;
esac
PROMPT=$(cat "$PROMPT_FILE")
rm -f "$PROMPT_FILE"

# -----------------------------------------------------------------------
# 8. Invoke Claude Code
# -----------------------------------------------------------------------
echo "$(date '+%Y-%m-%d %H:%M:%S') $HOUR START $MODE wake=$WAKE" >> "$LOGFILE"

cd "$KOMBUCHA_DIR"
RESULT_FILE="$KOMBUCHA_DIR/logs/last_result.json"
claude -p "$PROMPT" \
    --output-format json \
    --max-turns "$MAX_TURNS" \
    --allowedTools "Read,Write,Edit,Bash,Grep,Glob,Agent" \
    > "$RESULT_FILE" 2>> "$LOGFILE"

EXIT_CODE=$?
echo "$(date '+%Y-%m-%d %H:%M:%S') $HOUR END $MODE exit=$EXIT_CODE" >> "$LOGFILE"

exit $EXIT_CODE
