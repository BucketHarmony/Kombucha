#!/bin/bash
# watcher.sh — Long-polls bridge CV endpoint, triggers soul on face detection.
#
# Runs as a systemd service. Restarts on failure.

BRIDGE=http://localhost:5050
COOLDOWN=120  # 2 minutes between instinct invocations — prevents runaway loops
LOGFILE=/opt/kombucha/logs/watcher.log

mkdir -p /opt/kombucha/logs

echo "$(date '+%Y-%m-%d %H:%M:%S') Watcher started" >> "$LOGFILE"

while true; do
    # Long-poll for face detection (60s timeout)
    RESULT=$(curl -sf "$BRIDGE/cv/wait?event=face&timeout=60" 2>/dev/null)

    if [ $? -ne 0 ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') Bridge unreachable, sleeping 10s" >> "$LOGFILE"
        sleep 10
        continue
    fi

    TRIGGERED=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('triggered', False))" 2>/dev/null)

    if [ "$TRIGGERED" = "True" ]; then
        # Double-check: verify face_count > 0 (not just motion)
        FACE_COUNT=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('face_count', 0))" 2>/dev/null)
        if [ "$FACE_COUNT" -gt 0 ] 2>/dev/null; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') Face confirmed (count=$FACE_COUNT), invoking soul" >> "$LOGFILE"
            /opt/kombucha/invoke_soul.sh instinct
            echo "$(date '+%Y-%m-%d %H:%M:%S') Instinct tick complete, cooldown ${COOLDOWN}s" >> "$LOGFILE"
            sleep $COOLDOWN
        else
            echo "$(date '+%Y-%m-%d %H:%M:%S') Trigger was motion-only (no face), skipping" >> "$LOGFILE"
            sleep 5
        fi
    fi

    # Brief pause to avoid tight-looping on timeout responses
    sleep 1
done
