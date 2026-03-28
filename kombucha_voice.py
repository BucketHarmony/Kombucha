#!/usr/bin/env python3
"""
kombucha_voice.py — Kombucha v2 voice layer.

Owns: microphone, speaker, STT, TTS, echo gate.
Publishes: speech utterances to Redis.
Reads: speech output queue from brain.

    python3 kombucha_voice.py [--debug] [--config config.yaml]
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from datetime import datetime

from kombucha.config import load_config
from kombucha.redis_bus import RedisBus
from kombucha.schemas import SpeechUtterance, Event, MotorCommand
from kombucha.audio import speak_async, HAS_WHISPER, HAS_VOSK
from kombucha.health import HealthMonitor

# --- CLI Args -----------------------------------------------------------------

_parser = argparse.ArgumentParser(description="Kombucha v2 voice layer")
_parser.add_argument("--debug", action="store_true")
_parser.add_argument("--config", type=str, default=None)
_args = _parser.parse_args()

# --- Config -------------------------------------------------------------------

config = load_config(_args.config)
config.debug_mode = _args.debug or config.debug_mode

logging.basicConfig(
    level=logging.DEBUG if config.debug_mode else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("kombucha.voice")

# --- Graceful Shutdown --------------------------------------------------------

running = True


def shutdown_handler(signum, _frame):
    global running
    log.info("Received signal %d, shutting down...", signum)
    running = False


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)


# --- Echo Gate ----------------------------------------------------------------

class EchoGate:
    """Prevents the mic from picking up TTS output as new speech.

    While speaking, all mic input is discarded. After speaking finishes,
    there's a configurable tail period where input is still discarded.
    """

    def __init__(self, tail_s: float = 1.5):
        self._speaking = False
        self._tail_s = tail_s
        self._stop_time = 0.0

    @property
    def is_active(self) -> bool:
        if self._speaking:
            return True
        if time.time() < self._stop_time:
            return True
        return False

    def start_speaking(self):
        self._speaking = True

    def stop_speaking(self):
        self._speaking = False
        self._stop_time = time.time() + self._tail_s


# --- Safety Reflexes ----------------------------------------------------------

STOP_COMMANDS = {"stop", "halt", "freeze", "emergency stop", "kombucha stop"}


def is_stop_command(text: str) -> bool:
    """Check if the text is a safety stop command."""
    return text.strip().lower() in STOP_COMMANDS


# ==============================================================================
# MAIN LOOP
# ==============================================================================

async def main():
    bus = RedisBus(config.redis)
    echo_gate = EchoGate(tail_s=config.audio.echo_gate_tail_s)
    health = HealthMonitor()
    last_human_speech_time = None

    log.info("Voice layer starting...")

    # Initialize STT listener
    stt_listener = None
    if config.audio.stt_backend == "whisper" and HAS_WHISPER:
        try:
            from kombucha.audio import WhisperSpeechListener
            stt_listener = WhisperSpeechListener(
                model_size=config.audio.whisper_model_size,
                device_index=config.audio.mic_device_index,
                sample_rate=config.audio.sample_rate,
            )
            stt_listener.start()
            log.info(f"Whisper STT started (model: {config.audio.whisper_model_size})")
        except Exception as e:
            log.warning(f"Whisper STT init failed: {e}")
    elif HAS_VOSK and config.audio.stt_enabled:
        from pathlib import Path
        vosk_path = Path(config.audio.vosk_model_path)
        if vosk_path.exists():
            try:
                from kombucha.audio import SpeechListener
                stt_listener = SpeechListener(
                    vosk_path,
                    device_index=config.audio.mic_device_index,
                    sample_rate=config.audio.sample_rate,
                )
                stt_listener.start()
                log.info("Vosk STT started")
            except Exception as e:
                log.warning(f"Vosk STT init failed: {e}")

    if stt_listener is None:
        log.warning("No STT backend available, voice layer will only handle TTS")

    log.info("Voice layer started.")

    try:
        while running:
            # --- Drain STT transcripts ---
            if stt_listener:
                transcripts = stt_listener.drain()

                for t in transcripts:
                    text = t.get("text", "").strip()
                    if not text:
                        continue

                    # Echo gate: discard if TTS is playing
                    if echo_gate.is_active:
                        log.debug(f"Echo gate active, discarding: {text[:50]}")
                        continue

                    log.info(f"HEARD: {text}")

                    # Safety reflex: emergency stop
                    if is_stop_command(text):
                        log.warning(f"STOP COMMAND DETECTED: {text}")
                        bus.publish_event(Event(
                            event_type="emergency_stop",
                            source="voice",
                            data={"text": text},
                        ))
                        bus.set_motor(MotorCommand())  # zero drive + zero turn = stop
                        echo_gate.start_speaking()
                        speak_async("Stopping.", config.audio, config.debug_mode)
                        echo_gate.stop_speaking()
                        continue

                    # Publish to brain
                    utterance = SpeechUtterance(
                        text=text,
                        confidence=0.9,  # Whisper doesn't provide per-segment confidence easily
                        time_short=t.get("time", datetime.now().strftime("%H:%M:%S")),
                    )
                    bus.append_speech(utterance)
                    bus.publish_wake("human_speech")
                    last_human_speech_time = time.time()

            # --- TTS output (read from brain's queue) ---
            speech_out = bus.pop_speech_out()
            if speech_out:
                log.info(f"Speaking: {speech_out[:60]}")
                echo_gate.start_speaking()
                speak_async(speech_out, config.audio, config.debug_mode)
                # Estimate speaking time (~150 words/min, ~5 chars/word)
                est_duration = max(1.0, len(speech_out) / 150 * 60 / 5)
                await asyncio.sleep(est_duration)
                echo_gate.stop_speaking()

            # --- Health reporting ---
            health_report = {
                "audio": health.check_audio(
                    stt_listener=stt_listener,
                    is_speaking=echo_gate.is_active,
                ),
            }
            bus.set_status("voice", health_report)

            await asyncio.sleep(0.1)  # 100ms poll interval

    finally:
        log.info("Voice layer shutting down...")
        if stt_listener:
            stt_listener.stop()
            log.info("STT listener stopped")
        log.info("Voice layer stopped.")


if __name__ == "__main__":
    asyncio.run(main())
