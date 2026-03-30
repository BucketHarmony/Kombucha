"""
imu_audio.py — IMU-reactive audio for Kombucha.

Monitors accelerometer/gyro and plays R2-style trills in response to
physical movement. Tilt, jolt, lift, and rotation each produce sound.

Farther from upright = more dissonance.
Upright = harmonic.
Speed of change = pitch sweep rate.
"""

import math
import threading
import time
import logging

log = logging.getLogger(__name__)


class IMUAudioReactor(threading.Thread):
    """Background thread that turns physical motion into sound."""

    def __init__(self, telemetry, poll_hz=10):
        super().__init__(daemon=True)
        self._telemetry = telemetry
        self._poll_interval = 1.0 / poll_hz
        self._running = False

        # State tracking
        self._last_roll = 0.0
        self._last_tilt = 0.0
        self._last_az = 9.8  # gravity
        self._last_sound_time = 0.0
        self._sound_cooldown = 0.3  # min seconds between sounds
        self._last_jolt_time = 0.0

        # Lazy audio import
        self._player = None

    def _get_player(self):
        if self._player is None:
            try:
                from audio_harmony import (
                    HarmonicPlayer, _render_chord, _render_harmonic_chirp,
                    _render_tremolo_chord, _silence, _concat, _humanize_freq,
                    SAMPLE_RATE,
                )
                self._player = HarmonicPlayer(volume=0.4)
                self._render_chord = _render_chord
                self._render_chirp = _render_harmonic_chirp
                self._render_tremolo = _render_tremolo_chord
                self._silence = _silence
                self._concat = _concat
                self._humanize = _humanize_freq
                self._sr = SAMPLE_RATE
                log.info("IMU audio reactor: player loaded")
            except Exception as e:
                log.warning(f"IMU audio: player failed ({e})")
                self._player = False
        return self._player if self._player else None

    def _play_samples(self, samples):
        """Play raw samples via aplay."""
        if not samples:
            return
        import struct, tempfile, wave, subprocess
        try:
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False, dir='/tmp') as f:
                tmp = f.name
            clamped = [max(-1.0, min(1.0, s * 0.4)) for s in samples]
            int_s = [int(s * 32767) for s in clamped]
            data = struct.pack('<%dh' % len(int_s), *int_s)
            with wave.open(tmp, 'w') as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(22050)
                w.writeframes(data)
            subprocess.Popen(
                ['aplay', '-D', 'plughw:4,0', '-q', tmp],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def run(self):
        self._running = True
        log.info("IMU audio reactor started")

        while self._running:
            time.sleep(self._poll_interval)

            if not self._telemetry:
                continue

            snap = self._telemetry.snapshot()
            ax = snap.get("ax", 0)
            ay = snap.get("ay", 0)
            az = snap.get("az", 0)
            gx = snap.get("gx", 0)
            gy = snap.get("gy", 0)
            gz = snap.get("gz", 0)

            if snap.get("last_update", 0) == 0:
                continue  # No telemetry yet

            now = time.time()
            if now - self._last_sound_time < self._sound_cooldown:
                continue

            player = self._get_player()
            if not player:
                continue

            # Calculate orientation
            denom = math.sqrt(ay * ay + az * az)
            tilt = math.degrees(math.atan2(-ax, denom)) if denom > 0.001 else 0.0
            roll = math.degrees(math.atan2(ay, az)) if abs(az) > 0.001 else 0.0

            # Deviation from upright (0 = upright, 180 = fully inverted)
            # Gravity vector angle from vertical
            g_mag = math.sqrt(ax*ax + ay*ay + az*az)
            if g_mag > 0.1:
                upright_angle = math.degrees(math.acos(
                    max(-1, min(1, az / g_mag))))
            else:
                upright_angle = 0

            # Rate of change
            tilt_rate = abs(tilt - self._last_tilt)
            roll_rate = abs(roll - self._last_roll)
            total_rate = tilt_rate + roll_rate

            # Jolt detection (sudden acceleration change)
            az_delta = abs(az - self._last_az)
            jolt = az_delta > 2.0  # Significant jolt

            self._last_tilt = tilt
            self._last_roll = roll
            self._last_az = az

            # --- Generate sounds based on physical state ---

            # JOLT — sharp startled trill
            if jolt and now - self._last_jolt_time > 2.0:
                self._last_jolt_time = now
                self._last_sound_time = now
                intensity = min(1.0, az_delta / 10.0)
                samples = self._render_jolt(intensity)
                self._play_samples(samples)
                continue

            # TILT/ROTATION — continuous orientation sound
            # Only trigger if significantly off-upright or moving fast
            if upright_angle > 15 and total_rate > 3.0:
                self._last_sound_time = now
                samples = self._render_orientation(upright_angle, total_rate, roll)
                self._play_samples(samples)
            elif total_rate > 20:
                # Fast rotation even if near upright
                self._last_sound_time = now
                samples = self._render_spin(total_rate, roll)
                self._play_samples(samples)

    def _render_jolt(self, intensity):
        """Sudden jolt — startled yelp."""
        base = 600 + intensity * 800  # Higher pitch for harder jolt
        return self._concat(
            self._render_chord(base, 'cluster', 40, 0.7),
            self._silence(10),
            self._render_chirp(base, base * 0.5, 'dim', 80, 0.6),
        )

    def _render_orientation(self, upright_angle, rate, roll):
        """Tilt/lean sound. More dissonant farther from upright."""
        # 0-30 degrees: harmonic (major/warm)
        # 30-90: increasingly tense (sus4 → minor → dim)
        # 90+: full dissonance (cluster, anxious)
        dissonance = min(1.0, upright_angle / 120.0)
        speed_ms = max(30, int(150 - rate * 2))  # Faster movement = shorter notes

        # Base frequency follows roll direction (left=low, right=high)
        base = 300 + (roll + 180) / 360 * 400  # 300-700Hz

        if dissonance < 0.25:
            chord = 'major'
        elif dissonance < 0.5:
            chord = 'sus4'
        elif dissonance < 0.75:
            chord = 'minor'
        else:
            chord = 'dim' if dissonance < 0.9 else 'cluster'

        # Tremolo increases with dissonance
        if dissonance > 0.5:
            trem = 3 + dissonance * 15
            return self._render_tremolo(base, chord, speed_ms, trem, 0.3 + dissonance * 0.3)
        else:
            return self._render_chord(base, chord, speed_ms, 0.3)

    def _render_spin(self, rate, roll):
        """Fast rotation — whirring sweep."""
        speed = min(1.0, rate / 60.0)
        dur = max(40, int(100 - speed * 50))

        # Sweep direction follows rotation
        if roll > 0:
            return self._render_chirp(400, 400 + speed * 400, 'power', dur, 0.3)
        else:
            return self._render_chirp(800, 800 - speed * 400, 'power', dur, 0.3)

    def stop(self):
        self._running = False
