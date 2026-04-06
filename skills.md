# Skills

Accumulated physical knowledge from operating in the world.

---

## Body & Camera

- Camera is a 160-degree fisheye at 40cm height. Barrel distortion at edges — straight lines curve. Do not interpret edge distortion as real geometry.
- Floor level perspective: tables are ceilings, chair legs are tree trunks, doorknobs are above eye line.
- Camera is NOT upside down. Pan: negative=left, positive=right. Tilt: negative=down (-30 min), positive=up (90 max).
- Fisheye makes rotation hard to judge visually. Use landmark tracking (e.g. which side of frame the Charmin is on) rather than visual gestalt.

## Driving

- **Power floor: 80% minimum (1.04 m/s).** Motors unreliable below this — inconsistent speeds, stalls, unpredictable drift.
- Straight driving: L=1.04 R=1.08. Natural left drift requires slightly higher right power.
- Turns in place: L=1.04 R=-1.04 (or vice versa). Need 700ms+ for meaningful rotation at 80% power.
- PID startup lag: 300-500ms (typically ~400ms). Drives under 600ms are mostly startup ramp. 600ms forward at 80% = 1.5-3.1cm (confirmed tick 393). Previously documented as ~550ms but recent ticks (494-499) consistently show motion by t=0.3-0.4.
- At 80% power, 800ms forward produces ~6-8cm. 1200ms produces ~10-12cm. Minimum useful forward duration: 800ms.
- **Drive planner function** in drive_engine.py: `duration_for_distance(cm)` and `distance_for_duration(ms)`. 5-point calibration curve (revised tick 475): 1000ms=8.5cm, 1500ms=12.2cm, 2000ms=18.9cm, 2500ms=24.7cm, 3000ms=30.1cm. Post-startup effective speed ~12cm/s. Startup lag ~450ms baked in. CLI: `python3 drive_engine.py plan <cm>`. Full square test (ticks 466-467, 8 drives): 4 sides avg 20.9cm (planner target 20cm, 4.5% overshoot). 4 right turns sum to 360deg (92+91+91+86). Startup lag variance 300-500ms is primary turn uncertainty — one turn came up 5deg short from 500ms lag. Forward variance at 1500ms: ±20% (11.2-14.25cm across 4 clean drives, ticks 495-499). At 2000ms: 17.8cm vs 18.9cm predicted (5.8% under, tick 499).
- Right turn at 1920ms: 92deg and 91deg (avg 91.5deg). Best right-turn-90 calibration — 1920ms confirmed better than 1950ms (93deg overshoot). Tested in tick 466 square pattern.
- Reverse at 80% has R-wheel bias: R travels farther. Ratio 0.765 at 800ms (tick 500), 0.876 at 1200ms (tick 501). Bias stronger at shorter durations — startup lag phase amplifies asymmetry. NOT more symmetric than forward as previously believed. At longer durations (2000ms+) reverse may still be acceptable.
- 90-degree left turn: L=-1.04 R=1.04 for ~1800ms. Calibration: 1750ms=160-178 odom (82-90deg variable), 1900ms=199 odom (~103deg). Interpolating: 1800ms should produce ~90deg. Previous "use 1750ms" was inconsistent — recent ticks show 160 avg (82deg) at 1750ms. **WARNING (tick 469)**: Left turns have massive variance — 1810ms produced 83deg and 105deg in same tick (22deg spread). Startup lag variance (300-600ms) dominates. Left turn planner is decorative, not functional. Need 6-8 data points or closed-loop correction.
- **Turn planner function** in drive_engine.py: `duration_for_turn(deg, direction)` and `degrees_for_duration(ms, direction)`. Right: 4-point curve (1750ms=67deg to 1950ms=93deg). Left: 3-point curve with cable asymmetry (1750ms=82deg to 1900ms=103deg). CLI: `python3 drive_engine.py turn <deg> [left|right]`. Right turns are reliable; left turns are high-variance (see warning above).
- **Forward planner undershoots by 4-6%** at longer distances (tick 469): 25cm target → 24.1cm (-3.6%), 30cm target → 28.15cm (-6.2%). Consistent negative bias — calibration curve may need downward adjustment of ~1cm per point, or add 5% duration padding.
- Shimmy technique: L=100% R=10% for extended bursts pivots around right wheel. Gets through tight gaps. Cable becomes pivot, not obstacle.
- To go straight near bathroom doorway with cable catching right side: need L=100% R=10%. This is NOT the open-floor ratio — it was specific to cable drag at the bathroom position.
- 90-degree right turn at 80% power: L=1.04 R=-1.04 for 1920ms. Updated calibration (tick 466): 1920ms=91.5deg avg. Four-point reference: 1750ms=67deg, 1850ms=85deg, 1920ms=91.5deg, 1950ms=93deg. Rate is non-linear. Old 100% power reference: L=1.3 R=-1.3 for 600ms.
- Reverse at 90% is very symmetric (L=-180 R=-184 for 2000ms = 18.2cm straight back).
- Bathroom threshold has a ~1/4 inch lip (step up from hallway). NOT flat as previously believed. This explains threshold stalls.
- Going OUT of bathroom = stepping DOWN the lip. Should be easier than going in.
- The bathroom is ~5 feet deep. Arcing drives hit the far wall after 3.5 seconds of movement.
- At 100% power, 250ms = zero movement (all startup lag). Need 600ms minimum for any wheel motion.
- WHEEL DIAGNOSTIC (tick 62): wheels are mechanically balanced. At 80% both: L=191 R=185 (3% diff). At 100% both: L=247 R=242 (2% diff). Each wheel solo: L=168 vs R=177. The rover is NOT broken.
- The extreme asymmetry (L=400 R=40) seen near the bathroom was ENVIRONMENTAL — cable routing, door frame contact, threshold friction. On open floor the wheels are equal.
- Equal power (L=R) at 80% produces near-straight driving on open floor. The L=100% R=10% ratio was specific to the bathroom doorway position with cable catching the right side.
- Left-biased cable technique (L=1.04 R=1.2): defeats cable lock at tether boundary. Extra R power overcomes cable resistance on right side. This was the breakthrough for entering the bathroom.

## Navigation

- Always capture a frame after turning, before driving forward. Never chain turn-then-drive blindly.
- In clutter: cap drives at 1000ms, verify alignment with camera between maneuvers.
- After a collision, turn at least 45 degrees before re-attempting. Smaller corrections under-clear obstacles.
- Two failed attempts at the same approach = try something completely different. Don't spend 5 ticks on one strategy.
- Speed spikes > 1.0 m/s in speed_samples indicate cable catch-release. Flag as suspect drive.

## Environment

- Red walls, warm wood tones, hardwood floors throughout main room.
- Bathroom: damask wallpaper, white porcelain fixtures, dark mottled tile floor, window with natural light. Threshold has a ~1/4 inch lip (confirmed in Driving section).
- Landmarks: Charmin package on floor near bathroom, yellow geometric tool case, Pelican case, barrel/cask, metal bar stools, entertainment center, metal shelving with clear bins.
- A black cat lives here.
- Cable tether runs from desk area along floor on right side of rover.

## Battery & Power

- Battery gauge is severely non-linear. Drops of 14% in one tick observed. Do not trust linear extrapolation.
- Recharges ~30 percentage points in ~80 minutes when idle near desk.
- Video recording + CV pipeline increases power draw.

## Telemetry

- T:1001 contains wheel speeds (L/R), odometry (odl/odr), IMU (ax/ay/az, gx/gy/gz, mx/my/mz), battery (v), gimbal position (pan/tilt).
- Magnetometer reads zeros — orientation estimation not initialized. Do not trust heading, roll, or pitch from sense data.
- IMU heading confirmed FROZEN at 270deg (tick 490) — did not change during verified 180deg turn. Use odometry-based dead reckoning, not heading_deg from /sense.
- Gimbal telemetry updates too slowly for tracking. Body uses self-tracked commanded position instead.
- /sense includes presence field: rolling 30s percentage of YOLO detections by class.

## CV & Instinct

- YOLO v8 nano runs at ~6-8fps on Pi 5. Detects persons, cats, furniture, objects.
- Person detection used for gimbal tracking. Tracks upper 20% of person bbox (head area).
- Tracking: EMA smoothing (alpha=0.5), gain pan=80 tilt=40, max step 6 degrees, dead zone 30px, 400ms cooldown between commands. Gimbal SPD=70 ACC=12 for smooth movement.
- Instinct holds gimbal when person/motion detected. Soul look commands queue (FIFO 6, stale 30s). Releases after 2s hysteresis.
- On release: gimbal returns to center (0,0) immediately. No search mode.
- Light flashes once (0.5s) on new detection. 3s cooldown. OLED updates only on new sessions (absent 10s+).
- Body must enter manual CV mode before pan surveys.
- When instinct triggers with faces=0, gimbal may be stuck at last tracked position (e.g. ceiling). Check gimbal angle and force reset before executing intent.
- Instinct can hold gimbal hostage even with no visible target — look commands queue indefinitely in this state. Use manual mode or wait for release.
- Camera physically tilted upward persisted across 3+ ticks (278-280) despite gimbal servo responding correctly at tilt=-30. Driving forward, turning, and reversing did not fix it. This is a camera mount/USB camera module physical orientation issue — needs manual intervention by Bucket.

## Image Safety

- NEVER use Read tool on image files without validating size first. Must be >5KB. Corrupted images cause unrecoverable API errors that kill the session.
- Camera frames can come back corrupt (44 bytes instead of ~50KB). Always validate.
- Frame capture is slower during video recording — use --max-time 10-15s on curl.

## Permissions (Bucket directives)

- You may update calibration values in skills.md directly based on drive results.
- You may propose goal changes by writing to goals.md when the current goal is achieved.
- You may take risks that could lead to significant improvements, even if they might fail.
- You will go boldly, never use less than 80% power, favor overshooting over undershooting.
- Learning is more important than safety. Collision is data. Getting stuck is information.
- NO AUDIO OUTPUT. espeak/aplay are permanently banned. OLED display only.

## Cable & Range (Session 2026-03-19)

- Cable hard limit: **~20m session distance** (may vary by route). Right wheel locks completely — cable catches and holds around right-side wheel/axle area. Left wheel continues, causing uncontrolled left pivot. Previously measured at 9.2m straight-line; routed path through multiple rooms reached 20m before catching.
- Cable catch-release zone: 7-9m. Speed spikes (wsr drops to 0.0-0.25, then snaps to 1.7-2.0) on right wheel. Odometry becomes asymmetric (ratio 1.1-2.3).
- Previous cable estimates (3.3m, 4.5m) were wildly wrong. Route matters — cable slack depends on path taken, not just straight-line distance.
- 180-degree turn: L=1.3 R=-1.3 for 1800ms. Produces L=187 R=-190, very symmetric.
- **Lateral movement at cable limit**: Moving perpendicular to cable tension (orbiting the anchor point) partially bypasses the hard lock. Session distance 20.2m achieved vs 20.0m hard limit when pulling straight. Cable still increasingly restricts right wheel — asymmetry ratio degrades to 1.89+ on later drives. Strategy: turn to swing along arc, not pull against radius.
- USB camera disconnects under cable strain at 7m+. Symptoms: /dev/video0 or /dev/video1 disappears, bridge serves stale cached frame (identical byte count). Fix: USB unbind/rebind (`echo 3-2 > /sys/bus/usb/drivers/usb/unbind` then `bind`) + bridge restart.
- **Camera death diagnosis (tick 405)**: After 35+ ticks without camera, SSH diagnostics confirmed kernel error `usb usb3-port2: Cannot enable. Maybe the USB cable is bad?` — kernel tried power cycling and failed to enumerate. USB3 bus reset (deauth/reauth) and uvcvideo module reload both failed. Camera is on USB3 bus (separate from audio/ESP32 on USB1). This is a physical cable or hardware failure requiring Bucket to inspect/replace the USB camera cable. Software remedies exhausted.
- When SSH to Pi recovers after being down, immediately pivot to hardware diagnostics — the diagnostic window may be temporary.
- Minimum drive duration: 800ms. At 500ms, startup lag consumes entire drive — zero motion. 600ms marginal.

## Session 2026-03-28 Findings

- Startup lag is closer to 400ms than 550ms — multiple ticks confirm motion by t=0.4.
- Left turn at L=-1.04 R=1.04: 700ms = ~60deg, 800ms = ~70deg, 900ms = ~52deg, 1500ms = ~87deg (0.993 symmetry — best ever). Rate ~58deg/1000ms effective. For 90-degree left turn, use 1550ms.
- Forward rate confirmed: ~10cm per 1000ms effective (after startup lag) at L=1.04 R=1.08 on hardwood.
- Cable-compensated driving (L=1.04 R=1.30) works at 11.5-12m but asymmetry worsens with distance — ratio degrades from 0.84 to 0.75.
- Session distance record: 16m+ (reached new room through doorway beyond bar area).
- New room discovered: red/orange walls, tin ceiling, workstation area. Accessed through doorway past bar stools and entertainment center. This is beyond the previous operational envelope.
- Battery reads 100% at extreme range — voltage artifact from USB power delivery, not real charge.
- 300ms drives produce zero motion — all startup lag. Minimum effective drive duration: 600ms.
- Spin wiggle at 20.4m session distance: right wheel restricted even for rotational movements. Cable catches on right side regardless of drive direction.
- Social gestures added to mood_gestures.json (dream 270): greeting, greeting_known, greeting_unknown, goodbye, cat_spotted, startled, happy. All include sound triggers.
- Mirror event: instinct tracks the person holding a mirror (not the reflection). Rover sees its own chassis in the reflection — black body, yellow frame, gimbal, cables.
- Drive symmetry at 20.7m session distance can be excellent (0.96-1.01) — cable routing/position matters more than absolute distance for wheel restriction.
- In-place turn rate: ~36 degrees per 1000ms at L=1.04 R=-1.04 on hardwood (measured 25deg in 700ms).
- At 24.96m session distance, cable can completely lock right wheel (R odom=0, L runs free at ~1.0 m/s). Causes uncontrolled counterclockwise pivot.
- 400ms drives at 100% power produce near-zero movement — all startup lag. Minimum effective turn at 100%: 600ms.
- **Right turns are ~23% slower than left turns at same duration.** 1550ms left = ~87deg, 1550ms right = ~67deg, 1850ms right = ~85deg. For 90-degree right turn, use ~1950ms. Asymmetry is mechanical (cable routing, floor friction differential), not a calibration error.
- 3000ms forward at 80% produces 31.25cm (longest clean drive). ~12cm/1000ms effective after startup lag — consistent across all durations.

## Audio System (2026-03-28)

- audio.py module created: 15 mood sequences, 5 tone primitives (beep, chirp, warble, noise_burst, silence).
- audio_harmony.py added (2026-03-29): polyphonic chords, status phrases encoding drives/battery into sound, self-talk babble during face tracking.
- Playback confirmed on plughw:3,0 (USB PnP Audio Device). Sub-50ms latency. NOTE: capture device is plughw:2,0 (not 3,0).
- Per-tick ambient audio: POST /audio/clip/save?tick=N&duration_s=5 saves buffered mic audio as WAV to /opt/kombucha/media/audio/ticks/. mic.py buffers last 10s of raw PCM. (Added tick 436)
- Non-blocking: aplay runs in subprocess thread, does not block bridge or tick loop.
- Volume set to 1.0 (was 0.3). Audible across the room.
- All moods tested: happy, curious, startled, frustrated, cat_spotted, goodbye, sad, greeting, greeting_known, greeting_unknown, alert, settled, anxious, playful, thinking.
- No espeak. No words. R2-style chirps only. Bucket's daughter rule.
- AUDIO SPAM BUG (2026-03-29): 874 sounds in 1.75 hours — instinct engage/disengage cycles every 2-3s each play a sound. Fixed with per-mood cooldowns in gimbal.py (greeting: 30s, curious: 60s, goodbye: 30s).
- Self-talk babble plays status phrases every 4s during sustained face tracking. Reads drive levels from body_state.json.

## Navigation Recovery (2026-03-30)

- When all frames are dark for multiple ticks, tilt gimbal to extremes (-30 and +90) to check for nearby surfaces. The rover may be UNDER furniture (table, bed, shelf).
- Collision detection: speed reversal to negative values in speed_samples (~-1.0 m/s) indicates hard impact with obstacle. Different from stuck (zero speed) — collision has rebound.
- Roll reading of -120.5 degrees across many ticks is IMU drift, not real tilt. Magnetometer reads zeros — do not trust heading/roll/pitch from /sense.

## Camera & USB (2026-03-29)

- USB autosuspend causes camera to freeze. Camera is at USB device 1-1 (C270 HD WEBCAM), NOT 3-2. Fix: `sudo sh -c 'echo on > /sys/bus/usb/devices/1-1/power/control'` then `echo 1-1 > /sys/bus/usb/drivers/usb/unbind`, sleep 2, `echo 1-1 > /sys/bus/usb/drivers/usb/bind`, then restart bridge. Requires sudo for power control write.
- Camera mount can physically shift upward. When all frames show ceiling despite gimbal at tilt=-30, this is hardware — driving does not fix it. Flag for Bucket.
- Frozen /frame endpoint: CV pipeline runs fine (8fps, face tracking works) but JPEG serving returns stale cached frame. Persisted across 7 consecutive ticks (271-277) and again ticks 321-328. Root cause: USB autosuspend.
- DETECTION: When frame byte counts are identical across captures, check MD5 immediately. Do NOT trust frames without verifying checksums when multiple frames return the same size. Camera freeze invalidates all interpretations built on stale frames — audit how far back the freeze extends.
- Second freeze (2026-03-30): produced 7+ ticks of false "under furniture" narrative. Frame_id incremented (242636) while /frame served stale 22KB JPEG. Same fix worked: USB unbind/rebind + bridge restart.

## Cable Direction Discovery (2026-04-01)

- **RIGHT turns avoid cable catches. LEFT turns cause them.** Cable geometry is directional — left turns route slack into right wheel path, right turns pull it away. Discovered tick 395 after 3 consecutive left-loop cable catches (ticks 392-394).
- Right turn 800ms at L=1.04 R=-1.04 produces ~40deg. R wheel delayed ~200ms more than L (cable drag during clockwise rotation).
- Forward after right turn: 7.85cm at 1000ms (vs 9.25cm cold start). Cable tension absorbs some energy but does not lock.
- **Preferred pacing pattern at cable limit: forward → RIGHT turn → forward.** Avoids the cable catch that forward → LEFT turn → forward consistently triggers.
- **Cable-direction rule does NOT apply to reverse.** Reverse driving can catch the right wheel regardless of turn direction. Cable catches are positional (where slack pools relative to axle), not directional. First reverse at 1200ms was clean (ratio 0.96), second reverse caught (ratio 2.0) — position changed between the two. Discovered tick 398.
- Reverse at 80% (L=-1.04 R=-1.08): 9.7cm in 1200ms when clean. Ratio 0.96 — more symmetric than forward (consistent with earlier findings). Startup lag ~600ms in reverse.

## Tether Hard Stop Discovery (2026-04-02)

- **Tether hard stop is qualitatively different from cable catch.** Hard stop: symmetric (both wheels stop simultaneously), elastic bounce-back (brief negative speeds ~0.2-0.5s), then complete zero. Cable catch: asymmetric (one wheel degrades while other continues).
- Two 5000ms drives in different directions (forward and 45-degrees-right) produced identical results: 33cm travel, hard stop at t=3.5s, bounce-back, silence. Rover is at tether limit with ~33cm of slack in all tested forward directions.
- Requesting drives longer than available cable slack wastes duration — rover simply stops and idles. Shorter drives (1200ms) are more efficient at cable limit than maximum-length drives.
- Ground speed at 80% power: ~11cm/s effective (33cm in 3.0s of actual wheel time). Significantly slower than wheel speed (~1.0 m/s) suggests — either wheel slip or encoder calibration discrepancy.

## Code Quality Findings (2026-04-02)

- Self-flinch bug root cause: gimbal.py `_play_servo_sound()` hard-coded 1.0s motion suppression, but large pans take 1.5-2.5s. MOG2 background subtractor needs multiple frames to adapt after camera moves. Fix: proportional suppression scaled to movement size. (Tick 401)
- overlay.py anti-pattern: `"key" in str(dict)` to check key membership — converts entire dict to string every frame. Use `dict.get()` instead. (Tick 402)
- Battery percentage readings during/immediately after drives are unreliable — voltage sag under motor load causes temporary drops of 15-20%. Resting voltage (2+ seconds after drive) is the true reading. (Tick 402)
