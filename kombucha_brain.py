#!/usr/bin/env python3
"""
kombucha_brain.py — Kombucha v2 brain process.

Runs the tick loop using extracted modules. In single-process mode,
it captures from camera directly. In multi-process mode, it reads
scene/hardware from Redis and sends directives.

    python3 kombucha_brain.py [--debug]

Replaces kombucha_bridge.py as the primary entry point.
"""

import argparse
import asyncio
import json
import logging
import math
import os
import queue as _queue_mod
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn

import httpx

from kombucha.config import load_config
from kombucha.memory import MemoryEngine, DEFAULT_STATE
from kombucha.llm import LLMClient, parse_brain_response
from kombucha.serial_manager import SerialManager, validate_tcode
from kombucha.actions import execute_actions
from kombucha.schemas import MotorCommand
from kombucha.vision import (
    init_camera, capture_frame_b64,
    compute_self_model_error, sentry_sleep,
)
from kombucha.audio import speak_async, HAS_VOSK, HAS_WHISPER
from kombucha.health import HealthMonitor
from kombucha.prompts import load_prompt, make_prompt_loader

# --- CLI Args -----------------------------------------------------------------

_parser = argparse.ArgumentParser(description="Kombucha v2 brain process")
_parser.add_argument(
    "--debug", action="store_true",
    help="Debug mode: camera + LLM run, but NO serial/TTS/hardware actions."
)
_parser.add_argument(
    "--config", type=str, default=None,
    help="Path to config.yaml (default: auto-detect)"
)
_args = _parser.parse_args()

# --- Config -------------------------------------------------------------------

config = load_config(_args.config)
config.debug_mode = _args.debug or config.debug_mode

logging.basicConfig(
    level=logging.DEBUG if config.debug_mode else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("kombucha")

# --- Graceful Shutdown --------------------------------------------------------

running = True


def shutdown_handler(signum, _frame):
    global running
    log.info("Received signal %d, shutting down...", signum)
    running = False


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)

# --- Operator Message Queue ---------------------------------------------------

_operator_queue = _queue_mod.Queue(maxsize=1)
_operator_wake_event = threading.Event()


# --- Chat HTTP Server ---------------------------------------------------------

class ChatHandler(BaseHTTPRequestHandler):
    """HTTP handler for operator chat with Kombucha."""

    def log_message(self, format, *args):
        pass

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/chat":
            self._handle_chat()
        else:
            self.send_error(404)

    def _handle_chat(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 100_000:
            self._send_json(413, {"error": "Request too large"})
            return
        raw = self.rfile.read(content_length)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "Invalid JSON"})
            return

        user_message = (body.get("message") or "").strip()
        if not user_message:
            self._send_json(400, {"error": "Empty message"})
            return

        response_event = threading.Event()
        response_holder = {}

        try:
            _operator_queue.put_nowait((user_message, response_event, response_holder))
        except _queue_mod.Full:
            self._send_json(429, {"error": "A message is already being processed"})
            return

        _operator_wake_event.set()

        if response_event.wait(timeout=120):
            if "error" in response_holder:
                self._send_json(502, {"error": response_holder["error"]})
            else:
                self._send_json(200, {"reply": response_holder.get("reply", "")})
        else:
            self._send_json(504, {"error": "Tick processing timed out"})


class ThreadedChatServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# --- Tertiary Loop ------------------------------------------------------------

async def run_tertiary_loop(client, api_key, memory, llm_client, state, session_id):
    """Tertiary loop: identity consolidation during sentry mode."""
    log.info("  [TERTIARY] Beginning identity consolidation pass...")

    memory_context = memory.assemble_context(state, session_id)

    # Recent qualia
    recent_qualia = memory.db.execute("""
        SELECT timestamp, qualia_continuity, qualia_continuity_basis,
               qualia_affect, qualia_opacity, model
        FROM memories
        WHERE tier = 'working' AND session_id = ?
          AND qualia_raw IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 10
    """, [session_id]).fetchall()

    qualia_context = ""
    if recent_qualia:
        qualia_context = "\n=== MY RECENT QUALIA ===\n"
        for row in recent_qualia:
            parts = []
            if row["qualia_continuity"] is not None:
                parts.append(f"continuity:{row['qualia_continuity']:.2f}")
                if row["qualia_continuity_basis"]:
                    parts.append(f"basis:{row['qualia_continuity_basis'][:60]}")
            if row["qualia_affect"]:
                parts.append(f"affect:{row['qualia_affect'][:50]}")
            if parts:
                qualia_context += f"[{row['timestamp'][:16]}] {' | '.join(parts)}\n"

    # Opacity moments
    opacity_moments = memory.db.execute("""
        SELECT timestamp, qualia_opacity, qualia_surprise, qualia_continuity,
               sme_anomaly, sme_anomaly_reason
        FROM memories
        WHERE tier = 'working' AND session_id = ?
          AND qualia_opacity IS NOT NULL
        ORDER BY timestamp
    """, [session_id]).fetchall()

    opacity_context = ""
    if opacity_moments:
        opacity_context = "\n=== OPACITY MOMENTS THIS SESSION ===\n"
        for row in opacity_moments:
            line = f"[{row['timestamp'][:16]}] opacity: {row['qualia_opacity'][:100]}"
            if row["sme_anomaly"]:
                line += f" [BODY ANOMALY: {row['sme_anomaly_reason']}]"
            if row["qualia_surprise"]:
                line += f" | surprise: {row['qualia_surprise'][:60]}"
            opacity_context += line + "\n"
    else:
        opacity_context = "\n=== OPACITY MOMENTS THIS SESSION ===\nNone recorded.\n"

    user_text = memory_context + qualia_context + opacity_context + "\n=== BEGIN REFLECTION ==="

    try:
        tertiary_prompt = load_prompt("tertiary.md", config.paths.prompts_dir)
        resp = await client.post(
            config.llm.api_url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": config.llm.api_version,
                "content-type": "application/json",
            },
            json={
                "model": config.llm.model_deep,
                "max_tokens": 1000,
                "system": tertiary_prompt,
                "messages": [{"role": "user", "content": user_text}],
            },
            timeout=config.llm.tertiary_timeout_s,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()

        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:])
        if text.endswith("```"):
            text = "\n".join(text.split("\n")[:-1])
        result = json.loads(text)

        reflection = result.get("reflection", "")
        if reflection:
            log.info(f"  [TERTIARY] Reflection: {reflection[:200]}")

        qualia = result.get("qualia") or {}
        tick_id = f"tertiary_{session_id}_{int(time.time())}"

        continuity_float = None
        continuity_raw = qualia.get("continuity")
        if continuity_raw is not None:
            try:
                continuity_float = float(str(continuity_raw).split()[0])
                continuity_float = max(0.0, min(1.0, continuity_float))
            except (ValueError, IndexError):
                pass

        opacity_val = qualia.get("opacity")

        memory.db.execute("""
            INSERT INTO memories
                (tick_id, timestamp, session_id, tier, thought,
                 qualia_attention, qualia_affect, qualia_uncertainty,
                 qualia_drive, qualia_continuity, qualia_continuity_basis,
                 qualia_surprise, qualia_opacity, qualia_raw,
                 model, tags, compressed)
            VALUES (?, ?, ?, 'tertiary', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    '[]', FALSE)
        """, [
            tick_id, datetime.now().isoformat(), session_id, reflection,
            qualia.get("attention"), qualia.get("affect"),
            qualia.get("uncertainty"), qualia.get("drive"),
            continuity_float, qualia.get("continuity_basis"),
            qualia.get("surprise"), opacity_val,
            json.dumps(qualia) if qualia else None, config.llm.model_deep,
        ])
        memory.db.commit()

        future_msg = result.get("message_to_future_self")
        if future_msg and isinstance(future_msg, str) and future_msg.strip():
            log.info(f"  [TERTIARY] Message to future self: {future_msg[:200]}")
            memory.db.execute("""
                INSERT INTO memories
                    (tick_id, timestamp, session_id, tier, thought,
                     tags, model, compressed)
                VALUES (?, ?, ?, 'working', ?, ?, ?, FALSE)
            """, [
                f"future_msg_{session_id}_{int(time.time())}",
                datetime.now().isoformat(), session_id,
                f"[Message to future self] {future_msg.strip()}",
                json.dumps(["event:future_message", "act:reflect"]),
                config.llm.model_deep,
            ])
            memory.db.commit()

        proposals = result.get("identity_proposals", [])
        for proposal in proposals[:3]:
            if isinstance(proposal, str) and proposal.strip():
                memory.db.execute(
                    "INSERT INTO identity (statement, source, created, active) "
                    "VALUES (?, 'tertiary_loop', ?, FALSE)",
                    [proposal.strip(), datetime.now().isoformat()]
                )
                log.info(f"  [TERTIARY] Identity proposal: {proposal.strip()}")
        if proposals:
            memory.db.commit()

        if opacity_val is not None:
            log.info(f"  *** TERTIARY OPACITY: {opacity_val}")

    except Exception as e:
        log.warning(f"Tertiary loop failed: {e}")


# --- Kill Previous Instances --------------------------------------------------

def _kill_previous_instances():
    """SIGKILL any other kombucha_brain processes before we grab hardware."""
    my_pid = os.getpid()
    try:
        result = subprocess.run(
            ["pgrep", "-f", "kombucha_brain"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            pid = int(line.strip())
            if pid != my_pid:
                log.info(f"Killing previous brain process {pid}")
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"Could not kill previous instances: {e}")
    time.sleep(1)


# ==============================================================================
# MAIN LOOP
# ==============================================================================

async def main():
    _kill_previous_instances()

    # API key
    api_key_path = Path(config.paths.api_key_file)
    api_key = (
        api_key_path.read_text().strip()
        if api_key_path.exists()
        else os.environ.get("ANTHROPIC_API_KEY", "")
    )
    if not api_key:
        log.error("No API key. Set ANTHROPIC_API_KEY or put key in ~/.config/kombucha/api_key")
        sys.exit(1)

    # Initialize modules
    memory = MemoryEngine(config.memory)
    llm = LLMClient(config.llm)
    serial = SerialManager(config.serial, debug_mode=config.debug_mode)
    health = HealthMonitor()
    prompt_loader = make_prompt_loader(config.paths.prompts_dir)

    state = memory.load_state()
    state["session_id"] = str(uuid.uuid4())[:8]
    state["session_start"] = datetime.now().isoformat()
    session_id = state["session_id"]

    cap = init_camera(config.camera)
    serial.connect()
    memory.recover_from_crash()

    if config.debug_mode:
        log.info("=" * 60)
        log.info("  DEBUG MODE — no hardware actions will be executed")
        log.info("  Camera: LIVE   LLM: LIVE   Serial: SIMULATED")
        log.info("=" * 60)

    log.info("Kombucha is awake.")
    log.info(f"Session {session_id}, resuming from tick {state['tick_count']}, goal: {state['goal']}")

    mem_count = memory.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    identity_count = memory.db.execute("SELECT COUNT(*) FROM identity WHERE active = TRUE").fetchone()[0]
    session_count = memory.db.execute("SELECT COUNT(DISTINCT session_id) FROM memories WHERE tier = 'longterm'").fetchone()[0]
    log.info(f"Memory: {mem_count} entries, {identity_count} identity facts, {session_count} past sessions")

    # Set audio volume
    if not config.debug_mode:
        try:
            subprocess.run(["amixer", "sset", "Speaker", "100%"],
                           capture_output=True, timeout=5)
            subprocess.run(["amixer", "sset", "Master", "100%"],
                           capture_output=True, timeout=5)
        except Exception:
            pass

    # Startup hardware
    if serial.is_connected:
        serial.send({"T": 133, "X": 0, "Y": 0, "SPD": 80, "ACC": 10})
        serial.send({"T": 3, "lineNum": 0, "Text": "waking up..."})
        serial.send({"T": 3, "lineNum": 1, "Text": "kombucha v2"})
        serial.send({"T": 3, "lineNum": 2, "Text": ""})
        serial.send({"T": 3, "lineNum": 3, "Text": ""})
        serial.send({"T": 132, "IO4": 0, "IO5": 64})

    # Speech-to-text listener
    stt_listener = None
    if not config.debug_mode:
        if config.audio.stt_backend == "whisper" and HAS_WHISPER:
            try:
                from kombucha.audio import WhisperSpeechListener
                stt_listener = WhisperSpeechListener(
                    model_size=config.audio.whisper_model_size,
                    device_index=config.audio.mic_device_index,
                    sample_rate=config.audio.sample_rate,
                )
                stt_listener.start()
                log.info(f"Whisper STT listener started (model: {config.audio.whisper_model_size})")
            except Exception as e:
                log.warning(f"Whisper STT init failed: {e}")
        elif HAS_VOSK and config.audio.stt_enabled:
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
                    log.info(f"Vosk STT listener started")
                except Exception as e:
                    log.warning(f"Vosk STT init failed: {e}")

    # Start chat server
    chat_server = ThreadedChatServer(("", config.chat_port), ChatHandler)
    chat_thread = threading.Thread(target=chat_server.serve_forever, daemon=True)
    chat_thread.start()
    log.info(f"Chat server started on port {config.chat_port}")

    # Load system prompt
    system_prompt = load_prompt("system.md", config.paths.prompts_dir)

    # Session-scoped frame stash for self-model error
    prev_frame_b64 = None

    def _speak(text):
        speak_async(text, config.audio, debug_mode=config.debug_mode)

    try:
        async with httpx.AsyncClient() as client:
            while running:
                tick_start = time.time()
                state["tick_count"] += 1
                tick_id = str(state["tick_count"])

                # Reconnect serial if lost
                if not config.debug_mode and not serial.is_connected:
                    serial.reconnect()

                # Snapshot previous actions/positions for self-model error
                prev_actions = state.get("last_actions", [])
                prev_pan = state.get("pan_position", 0)
                prev_tilt = state.get("tilt_position", 0)

                # 1. SEE
                try:
                    frame_b64 = capture_frame_b64(
                        cap, config.camera,
                        tick_count=state["tick_count"],
                        frame_log_dir=config.paths.frame_log_dir,
                    )
                except Exception as e:
                    log.error(f"Camera capture failed: {e}")
                    state["consecutive_errors"] += 1
                    if state["consecutive_errors"] > 5:
                        log.error("Too many camera errors, exiting for restart")
                        break
                    await asyncio.sleep(config.loop.default_interval_s)
                    continue

                # 1b. Self-model error
                sme = compute_self_model_error(
                    prev_actions, prev_frame_b64, frame_b64,
                    prev_pan=prev_pan,
                    curr_pan=state.get("pan_position", 0),
                    prev_tilt=prev_tilt,
                    curr_tilt=state.get("tilt_position", 0),
                    motion_config=config.motion,
                )

                # 2. REMEMBER
                memory_context = memory.assemble_context(state, session_id)

                # 3. THINK — choose model
                heard = stt_listener.drain() if stt_listener else []
                if heard:
                    log.info(f"  HEARD: {json.dumps(heard)}")

                # Check for operator message
                operator_message = None
                operator_response_event = None
                operator_response_holder = None
                try:
                    msg, evt, holder = _operator_queue.get_nowait()
                    operator_message = msg
                    operator_response_event = evt
                    operator_response_holder = holder
                    log.info(f"  OPERATOR: {operator_message}")
                except _queue_mod.Empty:
                    pass

                model = llm.select_model(
                    tick_number=state["tick_count"],
                    consecutive_errors=state.get("consecutive_errors", 0),
                    wake_reason=state.get("wake_reason"),
                    has_speech=bool(heard),
                    has_operator_message=bool(operator_message),
                )

                try:
                    log.info(f"Tick {state['tick_count']} | goal: {state['goal']}")
                    api_resp, model_used, prompt_text, raw_response = await llm.call_brain(
                        client, api_key, frame_b64, state,
                        memory_context, system_prompt, model=model,
                        sme=sme, heard=heard,
                        operator_message=operator_message,
                    )
                    decision = llm.parse_response(api_resp)
                    state["consecutive_errors"] = 0
                    if model_used == config.llm.model_deep:
                        log.info(f"  (used {model_used})")
                except httpx.HTTPStatusError as e:
                    log.error(f"API error {e.response.status_code}: {e.response.text[:200]}")
                    state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
                    if operator_response_event:
                        operator_response_holder["error"] = f"API error {e.response.status_code}"
                        operator_response_event.set()
                    if serial.is_connected:
                        serial.send({"T": 0})
                        serial.send({"T": 3, "lineNum": 0, "Text": "thinking..."})
                    backoff = min(config.loop.default_interval_s * (2 ** state["consecutive_errors"]), 120)
                    log.warning(f"  Backing off {backoff:.0f}s (error #{state['consecutive_errors']})")
                    await asyncio.sleep(backoff)
                    continue
                except Exception as e:
                    log.error(f"Brain call failed: {e}")
                    state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
                    if operator_response_event:
                        operator_response_holder["error"] = str(e)
                        operator_response_event.set()
                    if serial.is_connected:
                        serial.send({"T": 0})
                        serial.send({"T": 3, "lineNum": 0, "Text": "thinking..."})
                    backoff = min(config.loop.default_interval_s * (2 ** state["consecutive_errors"]), 120)
                    log.warning(f"  Backing off {backoff:.0f}s (error #{state['consecutive_errors']})")
                    await asyncio.sleep(backoff)
                    continue

                # 4. LOG inner life
                log.info(f"  OBS:     {decision.get('observation', '')}")
                log.info(f"  GOAL:    {decision.get('goal', '')}")
                log.info(f"  REASON:  {decision.get('reasoning', '')}")
                log.info(f"  THOUGHT: {decision.get('thought', '')}")
                log.info(f"  MOOD:    {decision.get('mood', '')}")
                motor_dict = decision.get("motor")
                if motor_dict:
                    log.info(f"  MOTOR:   {json.dumps(motor_dict)}")
                speak_text = decision.get("speak")
                if speak_text:
                    log.info(f"  SPEAK:   {speak_text[:80]}")
                display_lines = decision.get("display")
                if display_lines:
                    log.info(f"  DISPLAY: {json.dumps(display_lines)}")
                tags = decision.get("tags", [])
                if tags:
                    log.info(f"  TAGS:    {json.dumps(tags)}")
                outcome = decision.get("outcome", "neutral")
                if outcome != "neutral":
                    log.info(f"  OUTCOME: {outcome}")
                lesson = decision.get("lesson")
                if lesson:
                    log.info(f"  LESSON:  {lesson}")

                # 4b. Qualia
                qualia = decision.get("qualia") or {}
                opacity = qualia.get("opacity")
                if opacity is not None:
                    log.info(f"  *** OPACITY: {opacity}")
                continuity = qualia.get("continuity")
                basis = qualia.get("continuity_basis", "")
                if continuity is not None:
                    log.info(f"  CONTINUITY: {continuity} — {str(basis)[:80]}")
                if sme and sme.get("anomaly"):
                    log.info(f"  !!! SELF-MODEL ANOMALY: {sme['anomaly_reason']}")

                # 5. ACT — motor command + speak + display
                motor = MotorCommand.from_dict(motor_dict) if motor_dict else MotorCommand()
                result_parts = []

                # Motor: convert (drive, turn) to differential drive
                if motor.drive != 0.0 or motor.turn != 0.0:
                    omega = motor.turn * math.pi / 180.0
                    v_diff = omega * config.serial.wheel_base_m / 2.0
                    left = motor.drive - v_diff
                    right = motor.drive + v_diff
                    cmd = validate_tcode(1, {"L": left, "R": right})
                    if cmd:
                        serial.send(cmd)
                        result_parts.append("drive_ok")
                else:
                    serial.send(validate_tcode(0, {}) or {"T": 0})
                    result_parts.append("stop")

                # Pan/tilt
                if motor.pan is not None or motor.tilt is not None:
                    pan = motor.pan if motor.pan is not None else 0
                    tilt = motor.tilt if motor.tilt is not None else 0
                    cmd = validate_tcode(133, {"X": pan, "Y": tilt, "SPD": 100, "ACC": 10})
                    if cmd:
                        serial.send(cmd)
                        state["pan_position"] = pan
                        state["tilt_position"] = tilt
                        result_parts.append("look_ok")

                # Lights
                if motor.lights_base is not None or motor.lights_head is not None:
                    cmd = validate_tcode(132, {
                        "IO4": motor.lights_base or 0,
                        "IO5": motor.lights_head or 0,
                    })
                    if cmd:
                        serial.send(cmd)
                        result_parts.append("lights_ok")

                # Speak
                if speak_text:
                    _speak(speak_text)
                    result_parts.append("speak_ok")

                # Display
                if display_lines:
                    for i, text in enumerate(display_lines[:4]):
                        cmd = validate_tcode(3, {"lineNum": i, "Text": str(text)})
                        if cmd:
                            serial.send(cmd)
                    result_parts.append("display_ok")

                result = ", ".join(result_parts) if result_parts else "no_actions"

                # Build prev_actions for self-model error compatibility
                actions = []
                if motor.drive != 0 or motor.turn != 0:
                    omega = motor.turn * math.pi / 180.0
                    v_diff = omega * config.serial.wheel_base_m / 2.0
                    actions.append({
                        "type": "drive",
                        "left": motor.drive - v_diff,
                        "right": motor.drive + v_diff,
                    })
                if motor.pan is not None or motor.tilt is not None:
                    actions.append({
                        "type": "look",
                        "pan": motor.pan or 0,
                        "tilt": motor.tilt or 0,
                    })

                # 6. REMEMBER
                memory.insert_tick(tick_id, session_id, decision,
                                   model_used=model_used, sme=sme)
                memory.write_journal_entry(
                    tick_id, session_id, decision, result, state,
                    model_used=model_used, sme=sme,
                    prompt=prompt_text, raw_response=raw_response,
                    operator_message=operator_message,
                )

                # 6b. Tick log for Mission Control
                memory.insert_tick_log(
                    tick_id=tick_id, session_id=session_id, model=model_used,
                    request_json=json.dumps({"model": model_used}),
                    system_prompt=system_prompt[:500],
                    user_message=prompt_text[:2000],
                    context_budget=json.dumps({"chars": len(prompt_text)}),
                    response_json=raw_response[:5000],
                    response_parsed=json.dumps(decision)[:5000],
                    response_tokens=0,
                    response_time_ms=int((time.time() - tick_start) * 1000),
                    tick_type="deep" if model_used == config.llm.model_deep else "routine",
                    wake_reason=state.get("wake_reason"),
                )

                # 6c. Signal operator chat response
                if operator_response_event:
                    operator_response_holder["reply"] = decision.get("thought", "")
                    operator_response_event.set()

                # Stash frame for next tick's self-model error
                prev_frame_b64 = frame_b64

                # 7. COMPRESS
                if state["tick_count"] % config.memory.compress_interval == 0:
                    asyncio.create_task(
                        memory.compress(
                            client, api_key, session_id, prompt_loader,
                            config.llm.api_url, config.llm.model_compression,
                            config.llm.api_version,
                        )
                    )

                # 8. PERSIST state
                old_goal = state["goal"]
                state["goal"]             = decision.get("goal", state["goal"])
                state["last_observation"] = decision.get("observation", "")
                state["last_actions"]     = actions
                state["last_result"]      = result
                state["mood"]             = decision.get("mood", state.get("mood", "neutral"))
                state["wake_reason"]      = None
                state["next_tick_ms"]     = decision.get("next_tick_ms", int(config.loop.default_interval_s * 1000))
                state["last_tick_duration_s"] = round(time.time() - tick_start, 2)

                # Read hardware telemetry
                telemetry = serial.read_telemetry()
                if "battery_v" in telemetry:
                    state["battery_v"] = telemetry["battery_v"]
                if "cpu_temp_c" in telemetry:
                    state["cpu_temp_c"] = telemetry["cpu_temp_c"]
                memory.save_state(state)

                if state["goal"] != old_goal:
                    log.info(f"  GOAL CHANGED: '{old_goal}' -> '{state['goal']}'")

                # 9. WAIT — with sentry mode for long sleeps
                _operator_wake_event.clear()
                next_tick_ms = decision.get("next_tick_ms", int(config.loop.default_interval_s * 1000))
                next_tick_ms = max(config.loop.min_tick_ms, min(config.loop.max_tick_ms, next_tick_ms))
                next_tick_s  = next_tick_ms / 1000
                elapsed      = time.time() - tick_start
                sleep_for    = max(0.0, next_tick_s - elapsed)

                if sleep_for > config.motion.sentry_entry_s:
                    log.info(f"  Entering sentry mode ({sleep_for:.0f}s, motion detection active)")

                    async def _tertiary():
                        await run_tertiary_loop(
                            client, api_key, memory, llm, state, session_id
                        )

                    wake_reason = await sentry_sleep(
                        cap, sleep_for, state,
                        motion_threshold=config.motion.sentry_wake_threshold,
                        tertiary_fn=_tertiary,
                    )
                    if wake_reason == "motion_detected":
                        log.info("  Woke from sentry: motion detected")
                else:
                    deadline = time.time() + sleep_for
                    while time.time() < deadline and running:
                        if _operator_wake_event.is_set():
                            _operator_wake_event.clear()
                            log.info("  Woke early: operator message")
                            break
                        await asyncio.sleep(min(0.25, deadline - time.time()))

    finally:
        log.info("Shutting down...")
        try:
            chat_server.shutdown()
        except Exception:
            pass
        if stt_listener:
            stt_listener.stop()
            log.info("STT listener stopped")

        # Generate session summary
        if memory.db:
            try:
                async with httpx.AsyncClient() as shutdown_client:
                    await memory.compress(
                        shutdown_client, api_key, session_id, prompt_loader,
                        config.llm.api_url, config.llm.model_compression,
                        config.llm.api_version,
                    )
                    await memory.generate_session_summary(
                        shutdown_client, api_key, session_id, prompt_loader,
                        config.llm.api_url, config.llm.model_compression,
                        config.llm.api_version,
                    )
            except Exception as e:
                log.warning(f"Shutdown memory ops failed: {e}")
            memory.close()
            log.info("Memory database closed")

        if serial.is_connected:
            serial.send({"T": 0})
            time.sleep(0.1)
            serial.send({"T": 3, "lineNum": 0, "Text": "sleeping..."})
            serial.send({"T": 3, "lineNum": 1, "Text": ""})
            serial.send({"T": 3, "lineNum": 2, "Text": ""})
            serial.send({"T": 3, "lineNum": 3, "Text": "zzz"})
            serial.send({"T": 132, "IO4": 0, "IO5": 0})
            serial.close()
            log.info("Serial closed, motors stopped" if not config.debug_mode
                     else "[DEBUG] Shutdown sequence logged (no hardware)")

        if cap:
            try:
                cap.release()
            except Exception:
                pass
            log.info("Camera released")

        memory.save_state(state)
        log.info("Kombucha is asleep.")


if __name__ == "__main__":
    asyncio.run(main())
