#!/bin/bash
# tick_helper.sh — Handles tick loop boilerplate so Claude focuses on the soul.
# Usage:
#   source tick_helper.sh
#   Then call: tick_start N, tick_gesture MOOD, tick_finish N

BRIDGE="http://localhost:5050"
KOMBUCHA_ROOT="/opt/kombucha"
MEDIA_DIR="$KOMBUCHA_ROOT/media/raw"
GESTURE_FILE="$KOMBUCHA_ROOT/mood_gestures.json"

# Gimbal slowdown factor for cinematic movement during ticks.
# 10 = ten times slower pan/tilt. Set to 1 for normal speed.
GIMBAL_SLOW=10

# Discover bridge IP
tick_discover() {
    for h in kombucha.local 192.168.7.182 192.168.7.44; do
        if curl -s --connect-timeout 2 "http://$h:5050/health" > /dev/null 2>&1; then
            BRIDGE="http://$h:5050"
            return 0
        fi
    done
    echo "BRIDGE_UNREACHABLE"
    return 1
}

# Ensure video session exists
tick_ensure_session() {
    local status=$(curl -s "$BRIDGE/video/status" 2>/dev/null)
    if echo "$status" | grep -q '"recording":false'; then
        curl -s -X POST "$BRIDGE/video/session/start" \
            -H "Content-Type: application/json" -d '{}' 2>/dev/null > /dev/null
    fi
}

# Start tick: capture frame, start video, return sense JSON
# Usage: SENSE=$(tick_start 186)
tick_start() {
    local N=$1
    local PADDED=$(printf "%04d" "$N")

    # Ensure video session
    tick_ensure_session

    # Capture frame
    local FRAME="$MEDIA_DIR/tick_${PADDED}_01.jpg"
    curl -s --max-time 10 "$BRIDGE/frame" -o "$FRAME" 2>/dev/null
    local SIZE=$(wc -c < "$FRAME" 2>/dev/null)
    if [ "$SIZE" -lt 5000 ]; then
        echo "FRAME_INVALID" >&2
        rm -f "$FRAME"
        return 1
    fi

    # Start video
    curl -s -X POST "$BRIDGE/video/tick/start" \
        -H "Content-Type: application/json" -d "{\"tick\": $N}" 2>/dev/null > /dev/null

    # Return sense data
    curl -s "$BRIDGE/sense" 2>/dev/null
}

# Execute mood gesture from JSON file
# Usage: tick_gesture "ballasted"
tick_gesture() {
    local MOOD="$1"
    local SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local GF="$SCRIPT_DIR/mood_gestures.json"

    # Enter manual mode
    curl -s -X POST "$BRIDGE/cv/mode" \
        -H "Content-Type: application/json" -d '{"mode":"manual"}' 2>/dev/null > /dev/null

    # GIMBAL_SLOW: divide speed/accel by this factor, multiply delays to match
    local SLOW=${GIMBAL_SLOW:-1}

    # Node outputs shell commands, bash executes them
    eval "$(node -e "
        const fs=require('fs'), path=require('path');
        const g=JSON.parse(fs.readFileSync(path.resolve(process.argv[1]),'utf8'));
        const m=process.argv[2].toLowerCase(), b=process.argv[3];
        const slow=parseFloat(process.argv[4])||1;
        const steps=g[m]||g['settled'];
        if(!g[m])process.stderr.write('Unknown mood: '+m+', using settled\n');
        for(const s of steps){
            if(s[0]==='look'){
                const spd=Math.max(1,Math.round(s[3]/slow));
                const acc=Math.max(1,Math.round(s[4]/slow));
                const delay=s[5]*slow;
                console.log('curl -s -X POST '+b+'/action -H \"Content-Type: application/json\" -d \\'{\"type\":\"look\",\"pan\":'+s[1]+',\"tilt\":'+s[2]+',\"speed\":'+spd+',\"accel\":'+acc+'}\\' 2>/dev/null >/dev/null; sleep '+(delay/1000));
            } else if(s[0]==='light')
                console.log('curl -s -X POST '+b+'/action -H \"Content-Type: application/json\" -d \\'{\"type\":\"light\",\"base\":'+s[1]+',\"head\":'+s[2]+'}\\' 2>/dev/null >/dev/null; sleep '+(s[3]/1000));
            else if(s[0]==='drive')
                console.log('curl -s -X POST '+b+'/action -H \"Content-Type: application/json\" -d \\'{\"type\":\"drive\",\"left\":'+s[1]+',\"right\":'+s[2]+',\"duration_ms\":'+s[3]+'}\\' 2>/dev/null >/dev/null; sleep '+(s[4]/1000));
            else if(s[0]==='sound')
                console.log('curl -s -X POST '+b+'/action -H \"Content-Type: application/json\" -d \\'{\"type\":\"sound\",\"mood\":\"'+s[1]+'\"}\\' 2>/dev/null >/dev/null; sleep '+(s[2]/1000));
            else if(s[0]==='wait')
                console.log('sleep '+(s[1]/1000));
        }
    " "$GF" "$MOOD" "$BRIDGE" "$SLOW")"

    # Return to tracking
    curl -s -X POST "$BRIDGE/cv/mode" \
        -H "Content-Type: application/json" -d '{"mode":"tracking"}' 2>/dev/null > /dev/null
}

# Update OLED display
# Usage: tick_oled "line1" "line2" "line3" "line4"
tick_oled() {
    curl -s -X POST "$BRIDGE/action" \
        -H "Content-Type: application/json" \
        -d "{\"type\":\"display\",\"lines\":[\"$1\",\"$2\",\"$3\",\"$4\"]}" \
        2>/dev/null > /dev/null
}

# Finish tick: center gimbal, stop video
# Usage: tick_finish 186
tick_finish() {
    local N=$1
    # Center gimbal (slow, cinematic)
    local spd=$(( 30 / GIMBAL_SLOW < 1 ? 1 : 30 / GIMBAL_SLOW ))
    local acc=$(( 8 / GIMBAL_SLOW < 1 ? 1 : 8 / GIMBAL_SLOW ))
    curl -s -X POST "$BRIDGE/action" \
        -H "Content-Type: application/json" \
        -d "{\"type\":\"look\",\"pan\":0,\"tilt\":0,\"speed\":$spd,\"accel\":$acc}" 2>/dev/null > /dev/null
    # Stop video
    curl -s -X POST "$BRIDGE/video/tick/stop" \
        -H "Content-Type: application/json" -d "{\"tick\": $N}" 2>/dev/null
}

# Send a slow look command (respects GIMBAL_SLOW)
# Usage: tick_look PAN TILT [SPEED] [ACCEL]
tick_look() {
    local pan=${1:-0} tilt=${2:-0} spd=${3:-30} acc=${4:-8}
    spd=$(( spd / GIMBAL_SLOW < 1 ? 1 : spd / GIMBAL_SLOW ))
    acc=$(( acc / GIMBAL_SLOW < 1 ? 1 : acc / GIMBAL_SLOW ))
    curl -s -X POST "$BRIDGE/action" \
        -H "Content-Type: application/json" \
        -d "{\"type\":\"look\",\"pan\":$pan,\"tilt\":$tilt,\"speed\":$spd,\"accel\":$acc}" \
        2>/dev/null
}

# Get frame path for a tick
tick_frame_path() {
    local N=$1
    local PADDED=$(printf "%04d" "$N")
    echo "$MEDIA_DIR/tick_${PADDED}_01.jpg"
}
