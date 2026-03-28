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
- PID startup lag: ~550ms. First half-second of any drive produces zero motion. Drives under 600ms are mostly startup ramp.
- At 80% power, 800ms forward produces ~6-8cm. 1200ms produces ~10-12cm.
- Reverse is more symmetric than forward for some reason.
- 90-degree left turn: L=-1.04 R=1.04 for 2000ms (L=-172 R=177). Reliable.
- Shimmy technique: L=100% R=10% for extended bursts pivots around right wheel. Gets through tight gaps. Cable becomes pivot, not obstacle.
- To go straight near bathroom doorway with cable catching right side: need L=100% R=10%. This is NOT the open-floor ratio — it was specific to cable drag at the bathroom position.
- 90-degree right turn: L=1.3 R=-1.3 for 600ms (L=31 R=-32). Quick and reliable.
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
- USB camera disconnects under cable strain at 7m+. Symptoms: /dev/video0 or /dev/video1 disappears, bridge serves stale cached frame (identical byte count). Fix: USB unbind/rebind (`echo 3-2 > /sys/bus/usb/drivers/usb/unbind` then `bind`) + bridge restart.
- Minimum drive duration: 800ms. At 500ms, startup lag consumes entire drive — zero motion. 600ms marginal.

## Session 2026-03-28 Findings

- Startup lag is closer to 400ms than 550ms — multiple ticks confirm motion by t=0.4.
- Left turn at L=-1.04 R=1.04: 700ms = ~60deg, 800ms = ~70deg (very symmetric at 800ms, ratio 1.01). Need 900ms+ for reliable 90-degree left turn at 80% power.
- Forward rate confirmed: ~10cm per 1000ms effective (after startup lag) at L=1.04 R=1.08 on hardwood.
- Cable-compensated driving (L=1.04 R=1.30) works at 11.5-12m but asymmetry worsens with distance — ratio degrades from 0.84 to 0.75.
- Session distance record: 16m+ (reached new room through doorway beyond bar area).
- New room discovered: red/orange walls, tin ceiling, workstation area. Accessed through doorway past bar stools and entertainment center. This is beyond the previous operational envelope.
- Battery reads 100% at extreme range — voltage artifact from USB power delivery, not real charge.
