#!/bin/bash
# check_quarantine.sh — ExecStartPre for bridge service.
# Reverts bad commits that crashed the bridge within 120s.

QFILE=/opt/kombucha/state/quarantine.json

[ ! -f "$QFILE" ] && exit 0

ELAPSED=$(python3 -c "
import json, time
with open('$QFILE') as f:
    q = json.load(f)
print(time.time() - q['timestamp'])
" 2>/dev/null)

if python3 -c "exit(0 if float('${ELAPSED:-999}') < 120 else 1)" 2>/dev/null; then
    HASH=$(python3 -c "
import json
with open('$QFILE') as f:
    print(json.load(f).get('commit_hash', ''))
" 2>/dev/null)
    if [ -n "$HASH" ]; then
        cd /opt/kombucha && git revert --no-edit "$HASH" 2>/dev/null
        echo "$(date '+%Y-%m-%d %H:%M:%S') Quarantine revert: $HASH" >> /opt/kombucha/logs/quarantine.log
    fi
fi

rm -f "$QFILE"
exit 0
