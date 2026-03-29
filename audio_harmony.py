"""
audio_harmony.py — Harmonic tone language for Kombucha.

Extends audio.py with:
- Polyphonic synthesis (2-7 simultaneous tones)
- Harmonic/disharmonic chord voicings
- Status language: encodes drives, battery, detections into sound
- Continuous self-talk during interactive moments

The Kombucha Tonal Language:
  Battery:     Base pitch (200Hz=dead, 600Hz=full)
  Wanderlust:  Tremolo speed (still=slow, restless=fast flutter)
  Social:      Consonance (seeking=dissonant, engaged=warm major)
  Curiosity:   Rising arpeggios (more curious = wider interval leaps)
  Distance:    Rhythmic density (far=many rapid pips, close=sparse)
  Cat memory:  Specific tritone-to-fifth motif (only when cat seen <1hr)
  Mood:        Chord quality (major=happy, minor=sad, dim=frustrated, aug=startled)
"""

import math
import random
import struct
import subprocess
import wave
import logging
import threading
import time
import json
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

SAMPLE_RATE = 22050
FADE_MS = 5
DEVICE = "plughw:3,0"
AUDIO_DIR = Path("/opt/kombucha/media/audio")

# --- Musical constants ---
# Interval ratios (equal temperament)
SEMITONE = 2 ** (1/12)
INTERVALS = {
    'unison': 0, 'minor2': 1, 'major2': 2, 'minor3': 3, 'major3': 4,
    'perfect4': 5, 'tritone': 6, 'perfect5': 7, 'minor6': 8, 'major6': 9,
    'minor7': 10, 'major7': 11, 'octave': 12,
}

# Chord templates (semitone offsets from root)
CHORDS = {
    'major':     [0, 4, 7],
    'minor':     [0, 3, 7],
    'dim':       [0, 3, 6],
    'aug':       [0, 4, 8],
    'major7':    [0, 4, 7, 11],
    'minor7':    [0, 3, 7, 10],
    'dom7':      [0, 4, 7, 10],
    'sus4':      [0, 5, 7],
    'power':     [0, 7, 12],
    'cluster':   [0, 1, 2, 3],        # dissonant
    'wholetone': [0, 2, 4, 6, 8, 10], # dreamy
    'open5ths':  [0, 7, 14, 21],      # spacious
    'warm':      [0, 4, 7, 12, 16],   # rich major with octave doubling
    'dark':      [0, 3, 7, 10, 14],   # minor 9th - brooding
    'bright':    [0, 4, 7, 11, 14, 19], # major add 9 + 5th above
    'anxious':   [0, 1, 5, 6, 11],    # chromatic tension
}


def _freq_at(root, semitones):
    """Frequency N semitones above root."""
    return root * (SEMITONE ** semitones)


def _render_chord(root_freq, chord_type, duration_ms, volume=1.0, detune_cents=0):
    """Render a polyphonic chord (2-7 simultaneous tones)."""
    offsets = CHORDS.get(chord_type, CHORDS['major'])
    n_samples = int(SAMPLE_RATE * duration_ms / 1000)
    fade_samples = int(SAMPLE_RATE * FADE_MS / 1000)
    per_voice = volume / max(len(offsets), 1)

    samples = [0.0] * n_samples
    for offset in offsets:
        freq = _freq_at(root_freq, offset)
        # Optional slight detune for warmth
        if detune_cents:
            freq *= (SEMITONE ** (detune_cents / 100))
        for i in range(n_samples):
            t = i / SAMPLE_RATE
            samples[i] += per_voice * math.sin(2 * math.pi * freq * t)

    # Envelope
    for i in range(min(fade_samples, n_samples)):
        env = 0.5 * (1 - math.cos(math.pi * i / fade_samples))
        samples[i] *= env
    for i in range(min(fade_samples, n_samples)):
        idx = n_samples - 1 - i
        env = 0.5 * (1 - math.cos(math.pi * i / fade_samples))
        samples[idx] *= env

    return samples


def _render_harmonic_chirp(root_start, root_end, chord_type, duration_ms, volume=1.0):
    """Sweep a chord from one root to another — all voices move in parallel."""
    offsets = CHORDS.get(chord_type, CHORDS['major'])
    n_samples = int(SAMPLE_RATE * duration_ms / 1000)
    fade_samples = int(SAMPLE_RATE * FADE_MS / 1000)
    per_voice = volume / max(len(offsets), 1)

    samples = [0.0] * n_samples
    for offset in offsets:
        phase = 0.0
        for i in range(n_samples):
            progress = i / max(n_samples - 1, 1)
            root = root_start + (root_end - root_start) * progress
            freq = _freq_at(root, offset)
            phase += 2 * math.pi * freq / SAMPLE_RATE
            samples[i] += per_voice * math.sin(phase)

    for i in range(min(fade_samples, n_samples)):
        env = 0.5 * (1 - math.cos(math.pi * i / fade_samples))
        samples[i] *= env
    for i in range(min(fade_samples, n_samples)):
        idx = n_samples - 1 - i
        env = 0.5 * (1 - math.cos(math.pi * i / fade_samples))
        samples[idx] *= env

    return samples


def _render_tremolo_chord(root_freq, chord_type, duration_ms, tremolo_hz, volume=1.0):
    """Chord with amplitude tremolo — encodes restlessness."""
    offsets = CHORDS.get(chord_type, CHORDS['major'])
    n_samples = int(SAMPLE_RATE * duration_ms / 1000)
    fade_samples = int(SAMPLE_RATE * FADE_MS / 1000)
    per_voice = volume / max(len(offsets), 1)

    samples = [0.0] * n_samples
    for offset in offsets:
        freq = _freq_at(root_freq, offset)
        for i in range(n_samples):
            t = i / SAMPLE_RATE
            tremolo = 0.5 + 0.5 * math.sin(2 * math.pi * tremolo_hz * t)
            samples[i] += per_voice * tremolo * math.sin(2 * math.pi * freq * t)

    for i in range(min(fade_samples, n_samples)):
        env = 0.5 * (1 - math.cos(math.pi * i / fade_samples))
        samples[i] *= env
    for i in range(min(fade_samples, n_samples)):
        idx = n_samples - 1 - i
        env = 0.5 * (1 - math.cos(math.pi * i / fade_samples))
        samples[idx] *= env

    return samples


def _silence(duration_ms):
    return [0.0] * int(SAMPLE_RATE * duration_ms / 1000)


def _concat(*sample_lists):
    """Concatenate multiple sample arrays."""
    out = []
    for s in sample_lists:
        out.extend(s)
    return out


# =========================================================================
# STATUS LANGUAGE — encodes rover state into harmonic phrases
# =========================================================================

def encode_battery(battery_pct, duration_ms=300):
    """Battery level as a chord. Low=dark low chord, full=bright high chord."""
    root = 200 + (battery_pct / 100) * 400  # 200-600Hz
    if battery_pct > 70:
        return _render_chord(root, 'major', duration_ms, volume=0.8)
    elif battery_pct > 30:
        return _render_chord(root, 'sus4', duration_ms, volume=0.8)
    else:
        return _render_tremolo_chord(root, 'dim', duration_ms, tremolo_hz=8, volume=0.9)


def encode_wanderlust(level, duration_ms=400):
    """Wanderlust as tremolo rate. Low=gentle pulse, high=frantic flutter."""
    root = 350
    tremolo = 2 + level * 18  # 2-20 Hz
    if level > 0.8:
        return _render_tremolo_chord(root, 'anxious', duration_ms, tremolo, volume=0.7)
    elif level > 0.4:
        return _render_tremolo_chord(root, 'minor', duration_ms, tremolo, volume=0.6)
    else:
        return _render_chord(root, 'major', duration_ms, volume=0.5)


def encode_social(level, has_face, duration_ms=350):
    """Social drive. Seeking=dissonant questioning, engaged=warm resolution."""
    if has_face:
        return _render_harmonic_chirp(300, 500, 'warm', duration_ms, volume=0.8)
    elif level > 0.6:
        return _render_harmonic_chirp(400, 300, 'dark', duration_ms, volume=0.7)
    else:
        return _render_chord(400, 'power', int(duration_ms * 0.5), volume=0.4)


def encode_curiosity(level, duration_ms=300):
    """Curiosity as rising arpeggio. Higher curiosity = wider leaps."""
    root = 400
    if level > 0.7:
        # Wide arpeggio — octave + fifth
        return _concat(
            _render_chord(root, 'major', 60, volume=0.6),
            _render_chord(root * 1.5, 'major', 60, volume=0.6),
            _render_chord(root * 2, 'major', 60, volume=0.7),
            _render_chord(root * 3, 'power', 80, volume=0.8),
        )
    elif level > 0.3:
        return _concat(
            _render_chord(root, 'sus4', 80, volume=0.5),
            _render_chord(root * 1.25, 'sus4', 80, volume=0.6),
        )
    else:
        return _render_chord(root, 'power', 100, volume=0.3)


def encode_distance(meters, duration_ms=250):
    """Distance as rhythmic density. Far=rapid pips, close=single note."""
    pips = min(7, max(1, int(meters / 3)))  # 1 pip per 3m, max 7
    root = 600
    pip_ms = max(30, duration_ms // (pips * 2))
    gap_ms = max(20, pip_ms // 2)
    parts = []
    for i in range(pips):
        freq = root + i * 50
        parts.append(_render_chord(freq, 'power', pip_ms, volume=0.5))
        if i < pips - 1:
            parts.append(_silence(gap_ms))
    return _concat(*parts)


def encode_cat_memory(seconds_since_cat, duration_ms=300):
    """Cat motif — tritone resolving to fifth. Only plays if cat seen recently."""
    if seconds_since_cat is None or seconds_since_cat > 3600:
        return []  # No cat memory
    root = 500
    # Tritone (unsettling) resolving to perfect fifth (recognition)
    return _concat(
        _render_chord(root, 'dim', int(duration_ms * 0.4), volume=0.6),
        _render_harmonic_chirp(root, root * 1.05, 'power', int(duration_ms * 0.6), volume=0.7),
    )


def compose_status_phrase(state):
    """Compose a full status phrase from rover state dict.

    state keys: battery_pct, wanderlust, social, curiosity, distance_m,
                has_face, seconds_since_cat
    Returns: list of float samples
    """
    # Randomize element order each time for variety
    elements = []

    elements.append(('social', lambda: encode_social(
        state.get('social', 0), state.get('has_face', False))))
    elements.append(('curiosity', lambda: encode_curiosity(state.get('curiosity', 0))))
    elements.append(('battery', lambda: encode_battery(state.get('battery_pct', 50))))
    elements.append(('wanderlust', lambda: encode_wanderlust(state.get('wanderlust', 0))))
    elements.append(('distance', lambda: encode_distance(state.get('distance_m', 0))))

    # Cat motif only if recently seen
    cat_secs = state.get('seconds_since_cat')
    if cat_secs is not None and cat_secs < 3600:
        elements.append(('cat', lambda: encode_cat_memory(cat_secs)))

    # Shuffle order — each status phrase has a different structure
    random.shuffle(elements)

    # Pick 3-4 elements (not all every time)
    count = random.randint(3, min(4, len(elements)))
    selected = elements[:count]

    parts = []
    for name, fn in selected:
        samples = fn()
        if samples:
            parts.append(samples)
            parts.append(_silence(random.randint(25, 60)))

    return _concat(*parts)


# =========================================================================
# MOOD CHORDS — harmonic versions of the original moods
# =========================================================================

def render_face_detect(face_size_pct=0.2):
    """Face detection: sharp detect trill then harmonic name flirtation.

    face_size_pct: how much of frame the face fills (0.0-1.0).
    Small face = distant = quieter, shorter flirtation.
    Big face = close = louder, richer, longer.
    """
    vol = 0.5 + face_size_pct * 0.5  # 0.5-1.0

    # 1. Detect trill — rapid ascending staccato chord burst (randomized)
    base = _humanize_freq(500, 50)
    trill = _concat(
        _render_chord(base, _humanize_chord('power'), _humanize_ms(40), vol),
        _silence(_humanize_ms(15, 0.5)),
        _render_chord(_humanize_freq(base * 1.3, 30), _humanize_chord('power'), _humanize_ms(40), vol),
        _silence(_humanize_ms(15, 0.5)),
        _render_chord(_humanize_freq(base * 1.6, 30), _humanize_chord('major'), _humanize_ms(50), vol),
    )

    # 2. Name flirtation — warm harmonic phrase that lingers
    #    Bigger face = more voices, wider chord, longer sustain
    if face_size_pct > 0.3:
        # Close — full warm flirtation
        flirt = _concat(
            _silence(30),
            _render_harmonic_chirp(400, 700, 'warm', 150, vol),
            _render_chord(700, 'bright', 200, vol * 0.9),
            _render_harmonic_chirp(700, 600, 'major7', 120, vol * 0.8),
        )
    elif face_size_pct > 0.1:
        # Medium distance
        flirt = _concat(
            _silence(30),
            _render_harmonic_chirp(400, 600, 'major', 120, vol),
            _render_chord(600, 'warm', 150, vol * 0.8),
        )
    else:
        # Far away — just a curious acknowledgment
        flirt = _concat(
            _silence(30),
            _render_chord(450, 'sus4', 100, vol * 0.7),
            _render_harmonic_chirp(450, 550, 'major', 80, vol * 0.6),
        )

    return _concat(trill, flirt)


def render_motion_detect(motion_area_pct=0.05):
    """Motion detection: low gloup that doubles in twiterpation with motion size.

    motion_area_pct: fraction of frame covered by motion (0.0-1.0).
    Tiny motion = single low gloup.
    Big motion = cascading doubled warble.
    """
    # Base "gloup" — low frequency bubble (randomized)
    root = _humanize_freq(150, 40)  # Deep, guttural with variation
    gloup = _concat(
        _render_harmonic_chirp(root, root * 1.5, 'power', 80, 0.7),
        _render_harmonic_chirp(root * 1.5, root * 0.8, 'minor', 60, 0.6),
    )

    # Doubling twiterpation — each layer adds a warble at increasing pitch
    layers = min(6, max(1, int(motion_area_pct * 60)))  # 1-6 layers

    twiterpation = []
    for i in range(layers):
        freq = root * (1.5 + i * 0.4)  # Each layer higher
        tremolo = 4 + i * 3  # Faster tremolo each layer
        duration = max(40, 100 - i * 10)  # Shorter each layer
        twiterpation.append(
            _render_tremolo_chord(freq, 'minor' if i % 2 == 0 else 'sus4',
                                  duration, tremolo, 0.5 + i * 0.08))
        if i < layers - 1:
            twiterpation.append(_silence(15))

    return _concat(gloup, _silence(20), *twiterpation)


def render_object_detect(class_name, confidence=0.5):
    """Object detection: deep thump then harmonic staccato spelling of the name.

    Each letter maps to a pitch and chord quality. The object's name
    becomes a unique melodic signature — 'chair' always sounds like 'chair'.
    """
    vol = 0.4 + confidence * 0.5  # 0.4-0.9 based on confidence

    # 1. Deep thump — low power chord with fast attack (randomized)
    thump_root = _humanize_freq(80, 20)
    thump = _concat(
        _render_chord(thump_root, 'power', _humanize_ms(60), vol * 1.2),
        _render_harmonic_chirp(thump_root, thump_root * 1.5, 'power', _humanize_ms(40), vol),
        _silence(_humanize_ms(30, 0.4)),
    )

    # 2. Letter-to-music mapping
    # Each letter gets a root frequency and chord quality
    LETTER_FREQ = {
        'a': 440, 'b': 494, 'c': 523, 'd': 587, 'e': 659,
        'f': 349, 'g': 392, 'h': 415, 'i': 466, 'j': 277,
        'k': 311, 'l': 330, 'm': 370, 'n': 294, 'o': 523,
        'p': 554, 'q': 233, 'r': 587, 's': 622, 't': 659,
        'u': 370, 'v': 415, 'w': 247, 'x': 277, 'y': 311,
        'z': 233,
    }
    # Vowels get major/warm, consonants get power/sus4
    VOWELS = set('aeiou')
    LETTER_CHORD = {
        True: ['major', 'warm', 'bright', 'major7'],   # vowels — open, resonant
        False: ['power', 'sus4', 'minor', 'dim'],       # consonants — percussive
    }

    # 3. Staccato spelling
    name = class_name.lower().replace(' ', '')[:8]  # Cap at 8 letters
    staccato = []
    for i, ch in enumerate(name):
        if ch not in LETTER_FREQ:
            continue
        freq = _humanize_freq(LETTER_FREQ[ch], 15)  # Slight pitch variation
        is_vowel = ch in VOWELS
        chords = LETTER_CHORD[is_vowel]
        chord_type = chords[i % len(chords)]

        # Vowels sustain slightly longer, consonants are snappy
        dur = _humanize_ms(70 if is_vowel else 45, 0.15)
        staccato.append(_render_chord(freq, chord_type, dur, vol * 0.8))
        staccato.append(_silence(20))

    return _concat(thump, *staccato)


def render_servo_sound(pan_from, pan_to, tilt_from, tilt_to):
    """Gimbal movement sound — pitch follows pan, chord follows tilt.

    Pan left = low, pan right = high (spatial audio mapping).
    Tilt up = bright/open, tilt down = dark/closed.
    Bigger movements = longer sweep, tiny movements = short pip.
    """
    # Movement magnitude
    pan_delta = abs(pan_to - pan_from)
    tilt_delta = abs(tilt_to - tilt_from)
    total_move = pan_delta + tilt_delta
    if total_move < 1:
        return []  # No audible movement

    # Duration: 30ms for tiny, 120ms for big sweeps
    dur = _humanize_ms(int(30 + min(total_move, 30) * 3), 0.15)

    # Pan → frequency (left=-180 → 250Hz, center=0 → 450Hz, right=+180 → 650Hz)
    freq_from = 250 + (pan_from + 180) / 360 * 400
    freq_to = 250 + (pan_to + 180) / 360 * 400

    # Tilt → chord quality (down=-30 → minor/dark, level=0 → sus4, up=+90 → major/bright)
    tilt_avg = (tilt_from + tilt_to) / 2
    if tilt_avg > 30:
        chord = random.choice(['major', 'bright', 'warm'])
    elif tilt_avg > -10:
        chord = random.choice(['sus4', 'power', 'major'])
    else:
        chord = random.choice(['minor', 'dark', 'dim'])

    # Volume: proportional to movement size, quiet overall
    vol = min(0.25, 0.08 + total_move * 0.005)

    if pan_delta > 3:
        # Sweep — pan is moving, chirp between frequencies
        return _render_harmonic_chirp(freq_from, freq_to, chord, dur, vol)
    else:
        # Pip — mostly tilt, single chord at current pan frequency
        return _render_chord(freq_to, chord, dur, vol)


HARMONIC_MOODS = {
    'greeting': [
        ('chord', 300, 'major', 100),
        ('silence', 40),
        ('harmonic_chirp', 300, 500, 'warm', 120),
        ('chord', 500, 'bright', 200),
    ],
    'greeting_known': [
        ('harmonic_chirp', 250, 600, 'major7', 150),
        ('silence', 30),
        ('chord', 600, 'warm', 200),
        ('chord', 600, 'bright', 150),
    ],
    'goodbye': [
        ('harmonic_chirp', 400, 250, 'minor', 200),
        ('silence', 80),
        ('harmonic_chirp', 300, 180, 'dark', 250),
    ],
    'curious': [
        ('chord', 400, 'sus4', 80),
        ('silence', 30),
        ('harmonic_chirp', 400, 600, 'major', 120),
        ('chord', 600, 'sus4', 100),
    ],
    'happy': [
        ('chord', 400, 'major', 80),
        ('chord', 500, 'major', 80),
        ('chord', 600, 'bright', 120),
        ('harmonic_chirp', 500, 800, 'warm', 100),
    ],
    'sad': [
        ('tremolo_chord', 300, 'minor', 400, 3),
        ('harmonic_chirp', 350, 250, 'dark', 300),
    ],
    'startled': [
        ('chord', 800, 'cluster', 60),
        ('silence', 30),
        ('chord', 600, 'dim', 60),
        ('harmonic_chirp', 600, 350, 'anxious', 150),
    ],
    'alert': [
        ('chord', 500, 'power', 80),
        ('silence', 60),
        ('chord', 500, 'power', 80),
        ('silence', 60),
        ('harmonic_chirp', 500, 700, 'major', 120),
    ],
    'frustrated': [
        ('tremolo_chord', 250, 'dim', 200, 6),
        ('chord', 200, 'cluster', 100),
        ('harmonic_chirp', 250, 200, 'dark', 200),
    ],
    'settled': [
        ('chord', 350, 'major', 300),
        ('harmonic_chirp', 350, 320, 'power', 200),
    ],
    'exploring': [
        ('harmonic_chirp', 300, 450, 'sus4', 100),
        ('silence', 30),
        ('harmonic_chirp', 400, 550, 'major', 100),
        ('chord', 500, 'open5ths', 150),
    ],
    'prowling': [
        ('chord', 250, 'minor', 120),
        ('silence', 50),
        ('chord', 280, 'minor', 120),
        ('harmonic_chirp', 280, 350, 'dark', 150),
    ],
    'cat_spotted': [
        ('chord', 600, 'aug', 60),
        ('harmonic_chirp', 600, 900, 'major', 80),
        ('harmonic_chirp', 900, 600, 'sus4', 80),
        ('chord', 700, 'bright', 120),
    ],
}


def _humanize_freq(freq, variance_cents=30):
    """Randomly shift frequency by up to variance_cents."""
    shift = random.uniform(-variance_cents, variance_cents)
    return freq * (SEMITONE ** (shift / 100))


def _humanize_ms(ms, variance_pct=0.2):
    """Randomly stretch/compress duration."""
    return max(20, int(ms * random.uniform(1 - variance_pct, 1 + variance_pct)))


def _humanize_chord(chord_type):
    """Occasionally substitute a related chord for variety."""
    substitutions = {
        'major': ['major', 'major', 'major7', 'warm', 'sus4'],
        'minor': ['minor', 'minor', 'minor7', 'dark', 'sus4'],
        'power': ['power', 'power', 'sus4', 'open5ths'],
        'warm': ['warm', 'warm', 'bright', 'major7'],
        'bright': ['bright', 'bright', 'warm', 'major7'],
        'dark': ['dark', 'dark', 'minor7', 'minor'],
        'sus4': ['sus4', 'sus4', 'power', 'major'],
    }
    options = substitutions.get(chord_type, [chord_type])
    return random.choice(options)


def render_harmonic_mood(mood, volume=1.0):
    """Render a harmonic mood sequence with random variation."""
    seq = HARMONIC_MOODS.get(mood, HARMONIC_MOODS.get('settled'))
    parts = []
    for step in seq:
        kind = step[0]
        if kind == 'chord':
            _, root, chord_type, ms = step
            root = _humanize_freq(root)
            ms = _humanize_ms(ms)
            chord_type = _humanize_chord(chord_type)
            parts.append(_render_chord(root, chord_type, ms, volume))
        elif kind == 'harmonic_chirp':
            _, start, end, chord_type, ms = step
            start = _humanize_freq(start)
            end = _humanize_freq(end)
            ms = _humanize_ms(ms)
            chord_type = _humanize_chord(chord_type)
            parts.append(_render_harmonic_chirp(start, end, chord_type, ms, volume))
        elif kind == 'tremolo_chord':
            _, root, chord_type, ms, trem_hz = step
            root = _humanize_freq(root)
            ms = _humanize_ms(ms)
            trem_hz *= random.uniform(0.8, 1.3)
            parts.append(_render_tremolo_chord(root, chord_type, ms, trem_hz, volume))
        elif kind == 'silence':
            _, ms = step
            parts.append(_silence(_humanize_ms(ms, 0.4)))
    return _concat(*parts)


# =========================================================================
# PLAYER — plays samples via aplay, non-blocking
# =========================================================================

class HarmonicPlayer:
    """Plays harmonic tones and status phrases via aplay."""

    def __init__(self, volume=0.5):
        self.volume = volume
        self._lock = threading.Lock()
        self._last_play = {}  # mood -> timestamp for cooldowns
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    def _samples_to_wav(self, samples, path):
        """Write samples to WAV file."""
        clamped = [max(-1.0, min(1.0, s * self.volume)) for s in samples]
        int_samples = [int(s * 32767) for s in clamped]
        data = struct.pack('<%dh' % len(int_samples), *int_samples)
        with wave.open(str(path), 'w') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(data)

    def _play_file(self, path):
        """Non-blocking aplay."""
        subprocess.Popen(
            ['aplay', '-D', DEVICE, '-q', str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def play_mood(self, mood, tick=None, cooldown_s=3.0):
        """Play a harmonic mood sound with cooldown."""
        now = time.time()
        with self._lock:
            last = self._last_play.get(mood, 0)
            if now - last < cooldown_s:
                return
            self._last_play[mood] = now

        samples = render_harmonic_mood(mood, self.volume)
        if not samples:
            return

        tick_str = f"tick_{tick:04d}" if tick else "notick"
        count = sum(1 for f in AUDIO_DIR.iterdir() if f.name.startswith(tick_str)) + 1
        fname = f"{tick_str}_sound_{count:02d}_{mood}.wav"
        path = AUDIO_DIR / fname
        self._samples_to_wav(samples, path)
        self._play_file(path)

        # Log to manifest
        try:
            with open(AUDIO_DIR / "manifest.jsonl", "a") as f:
                f.write(json.dumps({
                    "file": str(path), "filename": fname,
                    "tick": tick, "label": mood, "harmonic": True,
                    "duration_ms": int(len(samples) / SAMPLE_RATE * 1000),
                    "timestamp": datetime.now().isoformat(),
                }) + "\n")
        except Exception:
            pass

    def play_status(self, state, tick=None):
        """Play a full status phrase encoding rover state."""
        samples = compose_status_phrase(state)
        if not samples:
            return

        tick_str = f"tick_{tick:04d}" if tick else "notick"
        count = sum(1 for f in AUDIO_DIR.iterdir() if f.name.startswith(tick_str)) + 1
        fname = f"{tick_str}_status_{count:02d}.wav"
        path = AUDIO_DIR / fname
        self._samples_to_wav(samples, path)
        self._play_file(path)

        try:
            with open(AUDIO_DIR / "manifest.jsonl", "a") as f:
                f.write(json.dumps({
                    "file": str(path), "filename": fname,
                    "tick": tick, "label": "status_phrase",
                    "state": {k: round(v, 2) if isinstance(v, float) else v
                              for k, v in state.items()},
                    "harmonic": True,
                    "duration_ms": int(len(samples) / SAMPLE_RATE * 1000),
                    "timestamp": datetime.now().isoformat(),
                }) + "\n")
        except Exception:
            pass

    def play_self_talk(self, state, duration_s=5.0):
        """Play continuous status babble for duration_s seconds.

        Rapid succession of state-encoded phrases — the rover
        talking to itself about what it knows.
        """
        end_time = time.time() + duration_s
        iteration = 0
        while time.time() < end_time:
            # Vary the phrase each iteration
            phrase_parts = []

            if iteration % 3 == 0:
                # Battery + wanderlust
                phrase_parts.append(encode_battery(state.get('battery_pct', 50), 150))
                phrase_parts.append(_silence(20))
                phrase_parts.append(encode_wanderlust(state.get('wanderlust', 0), 200))
            elif iteration % 3 == 1:
                # Social + curiosity
                phrase_parts.append(encode_social(
                    state.get('social', 0), state.get('has_face', False), 180))
                phrase_parts.append(_silence(20))
                phrase_parts.append(encode_curiosity(state.get('curiosity', 0), 150))
            else:
                # Distance + cat + mood chord
                phrase_parts.append(encode_distance(state.get('distance_m', 0), 150))
                phrase_parts.append(_silence(20))
                cat = encode_cat_memory(state.get('seconds_since_cat'))
                if cat:
                    phrase_parts.append(cat)
                else:
                    phrase_parts.append(_render_chord(350, 'power', 100, 0.4))

            samples = _concat(*phrase_parts)
            # Write and play inline (blocking for phrase duration)
            import tempfile
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False, dir='/tmp') as f:
                tmp = f.name
            self._samples_to_wav(samples, tmp)
            proc = subprocess.run(
                ['aplay', '-D', DEVICE, '-q', tmp],
                timeout=3, capture_output=True,
            )
            try:
                os.unlink(tmp)
            except Exception:
                pass

            iteration += 1
            time.sleep(0.05)  # Tiny gap between phrases


import os  # needed for unlink in self_talk
