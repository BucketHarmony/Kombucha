#!/usr/bin/env python3
"""
story_server.py — Kombucha Story Viewer

Live web dashboard showing Kombucha's adventures as they happen.
Syncs JSONL journal + frames from the Pi, serves a dark-themed story view with SSE.

Usage:
    python story_server.py [--port 8080] [--no-sync]
"""

import argparse
import json
import os
import queue
import re
import subprocess
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import URLError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PI_HOST = "bucket@kombucha.local"
PI_JOURNAL_DIR = "~/kombucha/data/journal/"
PI_FRAMES_DIR = "~/kombucha/frames/"
PI_STATE_FILE = "~/kombucha/state.json"

LOCAL_DIR = Path(__file__).parent
LOCAL_JOURNAL = LOCAL_DIR / "data" / "journal"
LOCAL_FRAMES = LOCAL_DIR / "frames"
LOCAL_STATE = LOCAL_DIR / "data" / "state.json"

SYNC_INTERVAL = 8  # seconds between syncs

PI_CHAT_URL = "http://kombucha.local:8090/api/chat"
CHAT_TIMEOUT = 120

# ---------------------------------------------------------------------------
# JSONL Journal Parser
# ---------------------------------------------------------------------------

def parse_journal_files(journal_dir):
    """Parse all JSONL journal files into a list of tick dicts, ordered by tick."""
    ticks = {}
    if not journal_dir.exists():
        return []

    for jsonl_file in sorted(journal_dir.glob("*.jsonl")):
        try:
            for line in jsonl_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    tick_num = entry.get("tick")
                    if tick_num is not None:
                        ticks[int(tick_num)] = entry
                except json.JSONDecodeError:
                    continue
        except Exception:
            continue

    return [ticks[k] for k in sorted(ticks.keys())]


# ---------------------------------------------------------------------------
# Legacy Log Parser (fallback if no JSONL available)
# ---------------------------------------------------------------------------

def parse_logs(text):
    """Parse journalctl/stdout log output into tick dicts."""
    ticks = {}
    current_tick = None
    current_tick_num = None

    for line in text.splitlines():
        m = re.search(r'Tick\s+(\d+)\s+\|\s+goal:\s+(.*)', line)
        if m:
            current_tick_num = int(m.group(1))
            ts_match = re.match(r'^(\d{4}-\d{2}-\d{2}T[\d:]+[+-]\d{4})\s', line)
            timestamp = ts_match.group(1) if ts_match else ""
            if not timestamp:
                ts_match2 = re.match(r'^(\d{4}-\d{2}-\d{2}\s+[\d:,]+)\s', line)
                timestamp = ts_match2.group(1) if ts_match2 else ""

            current_tick = {
                "tick": current_tick_num,
                "timestamp": timestamp,
                "goal": m.group(2).strip(),
                "observation": "",
                "reasoning": "",
                "thought": "",
                "mood": "",
                "actions": [],
                "tags": [],
                "outcome": "neutral",
                "lesson": None,
                "memory_note": None,
                "goal_changed": None,
                "model": None,
            }
            ticks[current_tick_num] = current_tick
            continue

        if current_tick is None:
            continue

        obs = re.search(r'OBS:\s+(.*)', line)
        if obs:
            current_tick["observation"] = obs.group(1).strip()
            continue
        goal_line = re.search(r'GOAL:\s+(.*)', line)
        if goal_line:
            current_tick["goal"] = goal_line.group(1).strip()
            continue
        reason = re.search(r'REASON:\s+(.*)', line)
        if reason:
            current_tick["reasoning"] = reason.group(1).strip()
            continue
        thought = re.search(r'THOUGHT:\s+(.*)', line)
        if thought:
            current_tick["thought"] = thought.group(1).strip()
            continue
        mood_m = re.search(r'MOOD:\s+(.*)', line)
        if mood_m:
            current_tick["mood"] = mood_m.group(1).strip()
            continue
        actions = re.search(r'ACTIONS:\s+(.*)', line)
        if actions:
            raw = actions.group(1).strip()
            try:
                current_tick["actions"] = json.loads(raw)
            except json.JSONDecodeError:
                current_tick["actions"] = raw
            continue
        tags_m = re.search(r'TAGS:\s+(.*)', line)
        if tags_m:
            raw = tags_m.group(1).strip()
            try:
                current_tick["tags"] = json.loads(raw)
            except json.JSONDecodeError:
                pass
            continue
        outcome_m = re.search(r'OUTCOME:\s+(.*)', line)
        if outcome_m:
            current_tick["outcome"] = outcome_m.group(1).strip()
            continue
        lesson_m = re.search(r'LESSON:\s+(.*)', line)
        if lesson_m:
            current_tick["lesson"] = lesson_m.group(1).strip()
            continue
        note_m = re.search(r'NOTE:\s+(.*)', line)
        if note_m:
            current_tick["memory_note"] = note_m.group(1).strip()
            continue
        gc = re.search(r"GOAL CHANGED:\s+'(.*)'\s+->\s+'(.*)'", line)
        if gc:
            current_tick["goal_changed"] = {"from": gc.group(1), "to": gc.group(2)}
            continue
        model_m = re.search(r'\(used\s+(.*)\)', line)
        if model_m:
            current_tick["model"] = model_m.group(1).strip()
            continue

    return [ticks[k] for k in sorted(ticks.keys())]


# ---------------------------------------------------------------------------
# Frame matcher
# ---------------------------------------------------------------------------

def find_frame(tick_num, frames_dir):
    """Find the JPEG frame for a given tick number (latest if duplicates)."""
    pattern = f"tick_{tick_num:05d}_*.jpg"
    matches = sorted(frames_dir.glob(pattern))
    if matches:
        return matches[-1].name  # latest timestamp
    return None


def attach_frames(ticks, frames_dir):
    """Attach frame filenames to tick entries."""
    for t in ticks:
        tick_num = t.get("tick", 0)
        t["frame"] = find_frame(tick_num, frames_dir)
    return ticks


# ---------------------------------------------------------------------------
# Sync thread
# ---------------------------------------------------------------------------

class SyncThread(threading.Thread):
    def __init__(self, sse_broker):
        super().__init__(daemon=True)
        self.sse_broker = sse_broker
        self.known_ticks = set()
        self.all_ticks = []
        self.rover_state = {}
        self.lock = threading.Lock()
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        LOCAL_FRAMES.mkdir(parents=True, exist_ok=True)
        LOCAL_JOURNAL.mkdir(parents=True, exist_ok=True)

        # Parse any existing local data
        self._parse_and_diff()

        while not self._stop_event.is_set():
            try:
                self._sync_once()
            except Exception as e:
                print(f"[sync] Error: {e}")
            self._stop_event.wait(SYNC_INTERVAL)

    def _sync_once(self):
        # 1. Pull new frames FIRST (so they're available when journal is parsed)
        try:
            try:
                subprocess.run(
                    [
                        "rsync", "-az", "--ignore-existing",
                        f"{PI_HOST}:{PI_FRAMES_DIR}",
                        str(LOCAL_FRAMES) + "/",
                    ],
                    capture_output=True, timeout=30,
                )
            except FileNotFoundError:
                subprocess.run(
                    [
                        "scp", "-q",
                        f"{PI_HOST}:{PI_FRAMES_DIR}tick_*.jpg",
                        str(LOCAL_FRAMES) + "/",
                    ],
                    capture_output=True, timeout=30,
                )
        except subprocess.TimeoutExpired:
            print("[sync] Frame sync timed out")
        except Exception as e:
            print(f"[sync] Frame sync error: {e}")

        # 2. Pull JSONL journal files (after frames, so attach_frames finds them)
        try:
            try:
                subprocess.run(
                    [
                        "rsync", "-az", "--ignore-existing",
                        f"{PI_HOST}:{PI_JOURNAL_DIR}",
                        str(LOCAL_JOURNAL) + "/",
                    ],
                    capture_output=True, timeout=15,
                )
            except FileNotFoundError:
                # rsync not available (Windows), use scp
                subprocess.run(
                    [
                        "scp", "-q",
                        f"{PI_HOST}:{PI_JOURNAL_DIR}*.jsonl",
                        str(LOCAL_JOURNAL) + "/",
                    ],
                    capture_output=True, timeout=15,
                )
        except subprocess.TimeoutExpired:
            print("[sync] Journal sync timed out")
        except Exception as e:
            print(f"[sync] Journal sync error: {e}")

        # 3. Pull rover state
        try:
            result = subprocess.run(
                ["scp", "-q", f"{PI_HOST}:{PI_STATE_FILE}", str(LOCAL_STATE)],
                capture_output=True, timeout=10,
            )
            if result.returncode == 0 and LOCAL_STATE.exists():
                try:
                    self.rover_state = json.loads(LOCAL_STATE.read_text())
                except Exception:
                    pass
        except Exception:
            pass

        # 4. Parse and diff
        self._parse_and_diff()

    def _parse_and_diff(self):
        # Prefer JSONL if available
        ticks = parse_journal_files(LOCAL_JOURNAL)

        if not ticks:
            # Fallback: no JSONL found
            return

        attach_frames(ticks, LOCAL_FRAMES)

        with self.lock:
            self.all_ticks = ticks
            new_ticks = []
            for t in ticks:
                tick_num = t.get("tick", 0)
                if tick_num not in self.known_ticks:
                    self.known_ticks.add(tick_num)
                    new_ticks.append(t)

        for t in new_ticks:
            self.sse_broker.broadcast(t)

    def get_ticks(self, offset=0, limit=50):
        """Return ticks in reverse chronological order (newest first)."""
        with self.lock:
            reversed_ticks = list(reversed(self.all_ticks))
            return reversed_ticks[offset:offset + limit]

    def get_total(self):
        with self.lock:
            return len(self.all_ticks)

    def get_state(self):
        return self.rover_state.copy()


# ---------------------------------------------------------------------------
# SSE broker
# ---------------------------------------------------------------------------

class SSEBroker:
    """Fan-out new ticks to all connected SSE clients."""
    def __init__(self):
        self.subscribers = []
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            try:
                self.subscribers.remove(q)
            except ValueError:
                pass

    def broadcast(self, tick):
        with self.lock:
            dead = []
            for q in self.subscribers:
                try:
                    q.put_nowait(tick)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    self.subscribers.remove(q)
                except ValueError:
                    pass


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class StoryHandler(BaseHTTPRequestHandler):
    sync_thread = None
    sse_broker = None

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_html()
        elif path == "/api/ticks":
            self._serve_ticks(parsed.query)
        elif path == "/api/stream":
            self._serve_sse()
        elif path == "/api/state":
            self._serve_state()
        elif path.startswith("/frames/"):
            self._serve_frame(path[8:])
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/chat":
            self._proxy_chat()
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _proxy_chat(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            req = Request(
                PI_CHAT_URL,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=CHAT_TIMEOUT) as resp:
                result = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(result)
        except URLError as e:
            err = json.dumps({"error": f"Cannot reach rover: {e}"}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(err)
        except Exception as e:
            err = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(err)

    def _serve_html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode("utf-8"))

    def _serve_ticks(self, query_string):
        params = parse_qs(query_string)
        offset = int(params.get("offset", [0])[0])
        limit = int(params.get("limit", [30])[0])
        limit = min(limit, 100)

        ticks = self.sync_thread.get_ticks(offset, limit)
        total = self.sync_thread.get_total()

        body = json.dumps({"ticks": ticks, "total": total})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _serve_state(self):
        state = self.sync_thread.get_state()
        body = json.dumps(state)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = self.sse_broker.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()

            while True:
                try:
                    tick = q.get(timeout=15)
                    data = json.dumps(tick)
                    self.wfile.write(f"event: new_tick\ndata: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionError):
                        break
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            self.sse_broker.unsubscribe(q)

    def _serve_frame(self, filename):
        filename = Path(filename).name
        filepath = LOCAL_FRAMES / filename

        if not filepath.exists() or not filepath.suffix == ".jpg":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(filepath.read_bytes())


# ---------------------------------------------------------------------------
# HTML page (embedded)
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kombucha Story Viewer</title>
<style>
  :root {
    --bg: #0d1117;
    --card-bg: #161b22;
    --card-border: #30363d;
    --text: #c9d1d9;
    --text-dim: #8b949e;
    --text-bright: #f0f6fc;
    --accent: #58a6ff;
    --accent-dim: #1f6feb;
    --goal-bg: #1c2333;
    --goal-change: #f0883e;
    --chip-bg: #21262d;
    --chip-text: #7ee787;
    --thought-text: #d2a8ff;
    --scrollbar-bg: #161b22;
    --scrollbar-thumb: #30363d;
    --lesson-bg: #2d2a1a;
    --lesson-border: #d29922;
    --lesson-text: #e3b341;
    --note-text: #8b949e;
    --tag-bg: #1c2333;
    --tag-text: #79c0ff;
    --outcome-success: #238636;
    --outcome-failure: #da3633;
    --outcome-partial: #9e6a03;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    overflow-y: auto;
  }

  ::-webkit-scrollbar { width: 8px; }
  ::-webkit-scrollbar-track { background: var(--scrollbar-bg); }
  ::-webkit-scrollbar-thumb { background: var(--scrollbar-thumb); border-radius: 4px; }

  #chat-bar {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    background: var(--bg);
    border-top: 1px solid var(--card-border);
    padding: 10px 16px;
    display: flex;
    gap: 8px;
    align-items: flex-end;
    z-index: 100;
    max-width: 900px;
    margin: 0 auto;
  }

  #chat-input {
    flex: 1;
    background: var(--card-bg);
    color: var(--text);
    border: 1px solid var(--card-border);
    border-radius: 6px;
    padding: 8px 10px;
    font-family: inherit;
    font-size: 13px;
    line-height: 1.4;
    resize: none;
    min-height: 36px;
    max-height: 120px;
    outline: none;
  }

  #chat-input:focus {
    border-color: var(--accent);
  }

  #chat-send {
    background: var(--accent-dim);
    color: var(--text-bright);
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }

  #chat-send:hover { background: var(--accent); }
  #chat-send:disabled { opacity: 0.5; cursor: not-allowed; }

  #chat-status {
    font-size: 12px;
    color: var(--text-dim);
    font-style: italic;
    white-space: nowrap;
    align-self: center;
  }

  header {
    position: sticky;
    top: 0;
    z-index: 100;
    background: var(--bg);
    border-bottom: 1px solid var(--card-border);
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
  }

  .telemetry {
    display: flex;
    gap: 12px;
    font-size: 12px;
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
  }
  .telemetry .telem-item {
    display: flex;
    align-items: center;
    gap: 4px;
    background: var(--card-bg);
    padding: 3px 8px;
    border-radius: 4px;
    border: 1px solid var(--card-border);
  }
  .telemetry .telem-label { color: var(--text-dim); }
  .telemetry .telem-value { color: var(--text-bright); font-weight: 600; }
  .telemetry .telem-value.warn { color: var(--lesson-text); }
  .telemetry .telem-value.danger { color: #da3633; }

  header h1 {
    font-size: 18px;
    font-weight: 600;
    color: var(--text-bright);
  }

  header h1 span { color: var(--accent); }

  .status {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--text-dim);
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #3fb950;
    animation: pulse 2s ease-in-out infinite;
  }

  .status-dot.disconnected { background: #f85149; animation: none; }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  #feed {
    max-width: 900px;
    margin: 0 auto;
    padding: 16px 16px 70px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .tick-card {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 8px;
    display: flex;
    overflow: hidden;
    animation: slideIn 0.3s ease-out;
  }

  @keyframes slideIn {
    from { opacity: 0; transform: translateY(-10px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .tick-frame {
    flex: 0 0 320px;
    min-height: 240px;
    background: #0d1117;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
    cursor: pointer;
  }

  .tick-frame img {
    width: 100%;
    height: 100%;
    object-fit: contain;
  }

  .tick-frame .frame-expand {
    position: absolute;
    bottom: 6px;
    right: 6px;
    background: rgba(0,0,0,0.7);
    color: #ccc;
    font-size: 11px;
    padding: 2px 6px;
    border-radius: 3px;
    pointer-events: none;
  }

  /* Fullscreen image overlay */
  .frame-overlay {
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.92);
    z-index: 9999;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    flex-direction: column;
    padding: 20px;
  }
  .frame-overlay.active { display: flex; }
  .frame-overlay img {
    max-width: 95vw;
    max-height: 85vh;
    object-fit: contain;
    border-radius: 4px;
  }
  .frame-overlay .overlay-info {
    color: #aaa;
    font-size: 13px;
    margin-top: 12px;
    text-align: center;
    max-width: 800px;
    line-height: 1.5;
  }
  .frame-overlay .overlay-close {
    position: absolute;
    top: 16px;
    right: 24px;
    color: #888;
    font-size: 28px;
    cursor: pointer;
  }

  .tick-frame .no-frame {
    color: var(--text-dim);
    font-size: 12px;
    font-style: italic;
  }

  .tick-number {
    position: absolute;
    top: 6px;
    left: 6px;
    background: rgba(0,0,0,0.7);
    color: var(--accent);
    font-size: 11px;
    font-weight: 600;
    padding: 2px 6px;
    border-radius: 4px;
    font-variant-numeric: tabular-nums;
  }

  .tick-body {
    flex: 1;
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }

  .tick-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 8px;
  }

  .tick-header-left {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    flex: 1;
    flex-wrap: wrap;
  }

  .tick-goal {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--accent);
    background: var(--goal-bg);
    padding: 2px 8px;
    border-radius: 3px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 50%;
  }

  .mood-badge {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 2px 8px;
    border-radius: 10px;
    white-space: nowrap;
  }

  .mood-curious       { background: #1a3a5c; color: #58a6ff; }
  .mood-contemplative  { background: #2d1f4e; color: #d2a8ff; }
  .mood-lonely         { background: #4a1a1a; color: #f85149; }
  .mood-excited        { background: #1a3d1a; color: #3fb950; }
  .mood-cautious       { background: #3d3a1a; color: #d29922; }
  .mood-amused         { background: #1a3d3d; color: #56d4dd; }
  .mood-wondering      { background: #2d1f4e; color: #bc8cff; }
  .mood-serene         { background: #1a2d3d; color: #79c0ff; }
  .mood-awakening      { background: #3d2d1a; color: #f0883e; }
  .mood-anxious        { background: #4a1a1a; color: #f47067; }
  .mood-determined     { background: #1a3a5c; color: #79c0ff; }
  .mood-playful        { background: #1a3d3d; color: #56d4dd; }
  .mood-peaceful       { background: #1a2d3d; color: #79c0ff; }
  .mood-default        { background: #21262d; color: #8b949e; }

  .outcome-badge {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 2px 8px;
    border-radius: 10px;
    white-space: nowrap;
  }

  .outcome-success { background: #1a3d1a; color: #3fb950; }
  .outcome-failure { background: #4a1a1a; color: #f85149; }
  .outcome-partial { background: #3d3a1a; color: #d29922; }

  .tick-time {
    font-size: 11px;
    color: var(--text-dim);
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }

  .tick-obs {
    color: var(--text-bright);
    font-size: 14px;
    line-height: 1.4;
  }

  .tick-reasoning {
    color: var(--text-dim);
    font-size: 12px;
    line-height: 1.4;
  }

  .tick-thought {
    color: var(--thought-text);
    font-style: italic;
    font-size: 13px;
    line-height: 1.4;
    border-left: 2px solid var(--thought-text);
    padding-left: 10px;
    opacity: 0.85;
  }

  .tick-lesson {
    font-size: 12px;
    color: var(--lesson-text);
    background: var(--lesson-bg);
    border-left: 2px solid var(--lesson-border);
    padding: 4px 10px;
    border-radius: 0 4px 4px 0;
  }

  .tick-lesson::before {
    content: "Lesson: ";
    font-weight: 600;
  }

  .tick-note {
    font-size: 11px;
    color: var(--note-text);
    font-style: italic;
  }

  .tick-note::before {
    content: "Note: ";
    font-weight: 600;
    font-style: normal;
  }

  .tick-operator {
    font-size: 13px;
    color: var(--accent);
    background: var(--goal-bg);
    border-left: 2px solid var(--accent);
    padding: 4px 10px;
    border-radius: 0 4px 4px 0;
  }

  .tick-operator::before {
    content: "Bucket: ";
    font-weight: 600;
  }

  .tick-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  .action-chip {
    background: var(--chip-bg);
    color: var(--chip-text);
    font-size: 11px;
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    padding: 2px 8px;
    border-radius: 3px;
    border: 1px solid #30363d;
  }

  .action-chip.drive   { color: #79c0ff; }
  .action-chip.look    { color: #d2a8ff; }
  .action-chip.oled    { color: #f0883e; }
  .action-chip.display { color: #f0883e; }
  .action-chip.lights  { color: #d29922; }
  .action-chip.light   { color: #d29922; }
  .action-chip.speak   { color: #56d4dd; }
  .action-chip.stop    { color: #f85149; }

  .tick-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 3px;
  }

  .tag-chip {
    background: var(--tag-bg);
    color: var(--tag-text);
    font-size: 10px;
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    padding: 1px 6px;
    border-radius: 3px;
    opacity: 0.7;
  }

  /* Qualia instrumentation markers */
  .tick-qualia {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    font-size: 12px;
    padding: 4px 0;
  }
  .qualia-continuity {
    display: flex;
    align-items: center;
    gap: 4px;
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
  }
  .qualia-continuity .bar {
    width: 60px;
    height: 6px;
    background: #21262d;
    border-radius: 3px;
    overflow: hidden;
    display: inline-block;
  }
  .qualia-continuity .bar-fill {
    height: 100%;
    border-radius: 3px;
    background: #58a6ff;
  }
  .qualia-field {
    color: var(--text-dim);
    font-size: 11px;
  }
  .qualia-field strong { color: var(--text); font-weight: 500; }

  .tick-card.has-opacity {
    border-left: 3px solid #d2a8ff;
  }
  .opacity-marker {
    background: #2d1f4e;
    border-left: 2px solid #d2a8ff;
    color: #d2a8ff;
    font-size: 12px;
    padding: 4px 10px;
    border-radius: 0 4px 4px 0;
  }
  .opacity-marker::before {
    content: "Opacity: ";
    font-weight: 600;
  }

  .tick-card.has-anomaly {
    border-left: 3px solid #f47067;
  }
  .anomaly-marker {
    background: #3d1a1a;
    border-left: 2px solid #f47067;
    color: #f47067;
    font-size: 12px;
    padding: 4px 10px;
    border-radius: 0 4px 4px 0;
  }
  .anomaly-marker::before {
    content: "Body anomaly: ";
    font-weight: 600;
  }

  .tick-card.has-opacity.has-anomaly {
    border-left: 3px solid #f0883e;
  }

  .goal-change {
    font-size: 11px;
    color: var(--goal-change);
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .goal-change::before {
    content: "\2192";
    font-weight: bold;
  }

  .tick-model {
    font-size: 10px;
    color: var(--text-dim);
    opacity: 0.6;
  }

  .tick-footer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-top: auto;
  }

  #sentinel {
    height: 40px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .loading-spinner {
    width: 20px;
    height: 20px;
    border: 2px solid var(--card-border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  .empty-state {
    text-align: center;
    padding: 60px 20px;
    color: var(--text-dim);
  }

  .empty-state h2 { font-size: 20px; margin-bottom: 8px; color: var(--text); }

  .auto-scroll-badge {
    position: fixed;
    bottom: 20px;
    right: 20px;
    background: var(--accent-dim);
    color: var(--text-bright);
    font-size: 12px;
    padding: 6px 14px;
    border-radius: 20px;
    cursor: pointer;
    opacity: 0;
    transition: opacity 0.2s;
    z-index: 100;
    border: none;
  }

  .auto-scroll-badge.visible { opacity: 1; }
  .auto-scroll-badge:hover { background: var(--accent); }

  .tick-card.no-image {
    border-left: 3px solid var(--accent-dim);
  }

  .tick-number-inline {
    color: var(--accent);
    font-size: 11px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    opacity: 0.7;
  }

  /* Prompt/Response inspect buttons */
  .tick-inspect-icons {
    position: absolute;
    top: 6px;
    right: 6px;
    display: flex;
    gap: 4px;
    z-index: 2;
  }
  .tick-body { position: relative; }
  .inspect-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    font-size: 11px;
    font-weight: 700;
    font-family: "SFMono-Regular", Consolas, monospace;
    background: var(--chip-bg);
    color: var(--text-dim);
    border: 1px solid var(--card-border);
    border-radius: 4px;
    cursor: pointer;
    opacity: 0.6;
    transition: opacity 0.15s, color 0.15s;
  }
  .inspect-btn:hover {
    opacity: 1;
    color: var(--accent);
  }

  /* Text overlay modal (prompt/response viewer) */
  .text-overlay {
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.92);
    z-index: 10000;
    flex-direction: column;
    padding: 20px;
    cursor: pointer;
  }
  .text-overlay.active { display: flex; }
  .text-overlay-title {
    color: var(--accent);
    font-size: 16px;
    font-weight: 600;
    margin-bottom: 12px;
    flex-shrink: 0;
  }
  .text-overlay-content {
    flex: 1;
    overflow: auto;
    background: #0d1117;
    border: 1px solid var(--card-border);
    border-radius: 6px;
    padding: 16px;
    cursor: text;
  }
  .text-overlay-content pre {
    margin: 0;
    white-space: pre-wrap;
    word-wrap: break-word;
    color: var(--text);
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    font-size: 12px;
    line-height: 1.5;
  }
  .text-overlay .overlay-close {
    position: absolute;
    top: 16px;
    right: 24px;
    color: #888;
    font-size: 28px;
    cursor: pointer;
  }

  @media (max-width: 700px) {
    .tick-card { flex-direction: column; }
    .tick-frame { flex: 0 0 auto; max-height: 250px; }
  }
</style>
</head>
<body>
<header>
  <h1><span>Kombucha</span> Story Viewer</h1>
  <div class="status">
    <div class="status-dot" id="statusDot"></div>
    <span id="statusText">Connecting...</span>
    <span id="tickCount"></span>
  </div>
  <div class="telemetry" id="telemetry"></div>
</header>

<div id="feed"></div>
<div id="sentinel"></div>

<div id="chat-bar">
  <textarea id="chat-input" rows="1" placeholder="Say something to Kombucha..."></textarea>
  <span id="chat-status"></span>
  <button id="chat-send" onclick="sendChat()">Send</button>
</div>

<button class="auto-scroll-badge" id="scrollBtn" onclick="scrollToTop()">
  New ticks above
</button>

<div class="frame-overlay" id="frameOverlay" onclick="closeOverlay(event)">
  <span class="overlay-close">&times;</span>
  <img id="overlayImg" src="" alt="">
  <div class="overlay-info" id="overlayInfo"></div>
</div>

<div class="text-overlay" id="textOverlay" onclick="closeTextOverlay(event)">
  <span class="overlay-close">&times;</span>
  <h3 class="text-overlay-title" id="textOverlayTitle"></h3>
  <div class="text-overlay-content"><pre id="textOverlayPre"></pre></div>
</div>

<script>
(function() {
  const feed = document.getElementById('feed');
  const sentinel = document.getElementById('sentinel');
  const statusDot = document.getElementById('statusDot');
  const statusText = document.getElementById('statusText');
  const tickCountEl = document.getElementById('tickCount');
  const scrollBtn = document.getElementById('scrollBtn');
  const chatInput = document.getElementById('chat-input');
  const chatSendBtn = document.getElementById('chat-send');
  const chatStatus = document.getElementById('chat-status');

  // --- Full-frame overlay ---
  function showFullFrame(src, tickJson) {
    var overlay = document.getElementById('frameOverlay');
    document.getElementById('overlayImg').src = src;
    try {
      var t = JSON.parse(tickJson);
      var info = '<strong>Tick #' + t.tick + '</strong>';
      if (t.timestamp) info += ' &mdash; ' + t.timestamp;
      if (t.mood) info += ' &mdash; mood: <em>' + escHtml(t.mood) + '</em>';
      if (t.goal) info += '<br><strong>Goal:</strong> ' + escHtml(t.goal);
      if (t.observation || t.obs) info += '<br><strong>Observation:</strong> ' + escHtml(t.observation || t.obs);
      if (t.reasoning) info += '<br><strong>Reasoning:</strong> ' + escHtml(t.reasoning);
      if (t.thought) info += '<br><strong>Thought:</strong> ' + escHtml(t.thought);
      var actions = t.actions;
      if (typeof actions === 'string') try { actions = JSON.parse(actions); } catch(e) {}
      if (Array.isArray(actions) && actions.length) {
        info += '<br><strong>Actions:</strong> ' + escHtml(JSON.stringify(actions));
      }
      var tags = t.tags;
      if (typeof tags === 'string') try { tags = JSON.parse(tags); } catch(e) {}
      if (Array.isArray(tags) && tags.length) {
        info += '<br><strong>Tags:</strong> ' + tags.map(function(tg){ return escHtml(tg); }).join(', ');
      }
      if (t.outcome) info += '<br><strong>Outcome:</strong> ' + escHtml(t.outcome);
      if (t.lesson) info += '<br><strong>Lesson:</strong> ' + escHtml(t.lesson);
      if (t.memory_note) info += '<br><strong>Memory note:</strong> ' + escHtml(t.memory_note);
      if (t.model) info += '<br><strong>Model:</strong> ' + escHtml(t.model);
      var q = t.qualia || {};
      if (q.continuity !== undefined && q.continuity !== null) info += '<br><strong>Continuity:</strong> ' + q.continuity.toFixed(2) + (q.continuity_basis ? ' — ' + escHtml(q.continuity_basis) : '');
      if (q.attention) info += '<br><strong>Attention:</strong> ' + escHtml(q.attention);
      if (q.affect) info += '<br><strong>Affect:</strong> ' + escHtml(q.affect);
      if (q.uncertainty) info += '<br><strong>Uncertainty:</strong> ' + escHtml(q.uncertainty);
      if (q.drive) info += '<br><strong>Drive:</strong> ' + escHtml(q.drive);
      if (q.surprise) info += '<br><strong>Surprise:</strong> ' + escHtml(q.surprise);
      if (q.opacity !== undefined && q.opacity !== null) info += '<br><strong style="color:#d2a8ff">Opacity:</strong> ' + escHtml(q.opacity);
      var sm = t.sme || {};
      if (sm.frame_delta !== undefined && sm.frame_delta !== null) info += '<br><strong>Frame delta:</strong> ' + sm.frame_delta.toFixed(4) + (sm.anomaly ? ' <strong style="color:#f47067">[ANOMALY: ' + escHtml(sm.anomaly_reason) + ']</strong>' : '');
      document.getElementById('overlayInfo').innerHTML = info;
    } catch(e) {
      document.getElementById('overlayInfo').innerHTML = '';
    }
    overlay.classList.add('active');
  }

  function closeOverlay(e) {
    if (e.target.tagName === 'IMG') return; // don't close when clicking image
    document.getElementById('frameOverlay').classList.remove('active');
  }

  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      document.getElementById('frameOverlay').classList.remove('active');
      document.getElementById('textOverlay').classList.remove('active');
    }
  });

  window.showInspect = function(tickNum, field) {
    var data = tickInspectData[tickNum];
    if (!data) return;
    var label = field === 'prompt' ? 'Prompt' : 'Response';
    showTextOverlay(label + ' \u2014 Tick #' + tickNum, data[field] || '');
  };

  function showTextOverlay(title, text) {
    document.getElementById('textOverlayTitle').textContent = title;
    document.getElementById('textOverlayPre').textContent = text;
    document.getElementById('textOverlay').classList.add('active');
  }

  function closeTextOverlay(e) {
    if (e.target.closest('.text-overlay-content')) return;
    document.getElementById('textOverlay').classList.remove('active');
  }

  let allTicks = [];
  let tickInspectData = {};  // tick_num -> {prompt, raw_response}
  let loadedTickIds = new Set();
  let loading = false;
  let totalTicks = 0;
  let offset = 0;
  let autoScroll = true;
  let newTicksPending = false;

  // --- Mood color mapping ---
  const MOOD_CLASSES = {
    'curious': 'mood-curious',
    'contemplative': 'mood-contemplative',
    'lonely': 'mood-lonely',
    'excited': 'mood-excited',
    'cautious': 'mood-cautious',
    'amused': 'mood-amused',
    'wondering': 'mood-wondering',
    'serene': 'mood-serene',
    'awakening': 'mood-awakening',
    'anxious': 'mood-anxious',
    'determined': 'mood-determined',
    'playful': 'mood-playful',
    'peaceful': 'mood-peaceful',
  };

  function moodClass(mood) {
    if (!mood) return 'mood-default';
    const m = mood.toLowerCase().trim();
    return MOOD_CLASSES[m] || 'mood-default';
  }

  // --- Format action objects as readable chips ---
  function formatAction(a) {
    if (typeof a === 'string') return { text: a, type: '' };
    if (!a || typeof a !== 'object') return { text: String(a), type: '' };

    const t = a.type || '';
    let text = t;
    let cls = t;

    switch (t) {
      case 'drive':
        text = 'drive L:' + (a.left||0).toFixed(1) + ' R:' + (a.right||0).toFixed(1);
        if (a.duration_ms) text += ' ' + a.duration_ms + 'ms';
        break;
      case 'stop':
        text = 'STOP';
        break;
      case 'look':
        text = 'look pan:' + (a.pan||0) + ' tilt:' + (a.tilt||0);
        break;
      case 'display':
        var lines = a.lines || [];
        text = 'display "' + lines.filter(function(l){return l;}).join(' | ') + '"';
        cls = 'oled';
        break;
      case 'oled':
        text = 'oled[' + (a.line||0) + '] "' + (a.text||'') + '"';
        break;
      case 'oled_reset':
        text = 'oled reset';
        cls = 'oled';
        break;
      case 'lights':
      case 'light':
        text = 'lights base:' + (a.base||0) + ' head:' + (a.head||0);
        cls = 'lights';
        break;
      case 'speak':
        text = 'speak "' + (a.text||'') + '"';
        break;
      default:
        text = JSON.stringify(a);
    }

    return { text: text, type: cls };
  }

  // --- Parse actions ---
  function parseActions(raw) {
    if (!raw) return [];
    if (Array.isArray(raw)) return raw;
    if (typeof raw !== 'string') return [];

    try {
      var parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) return parsed;
    } catch(e) {}

    try {
      var fixed = raw.replace(/'/g, '"');
      var parsed2 = JSON.parse(fixed);
      if (Array.isArray(parsed2)) return parsed2;
    } catch(e) {}

    return raw.replace(/[\[\]]/g, '').split(',').map(function(s){return s.trim();}).filter(Boolean);
  }

  // --- Format timestamp ---
  function formatTime(ts) {
    if (!ts) return '';
    // ISO format: take just HH:MM:SS
    var m = ts.match(/(\d{2}:\d{2}:\d{2})/);
    if (m) return m[1];
    // Fallback
    var m2 = ts.match(/(\d{2}:\d{2})/);
    return m2 ? m2[1] : ts;
  }

  // --- Render a tick card ---
  function renderTick(t) {
    var card = document.createElement('div');
    card.className = 'tick-card';
    card.dataset.tick = t.tick;

    var actionsArr = parseActions(t.actions);
    var hasFrame = !!t.frame;
    var tags = t.tags || [];
    if (typeof tags === 'string') {
      try { tags = JSON.parse(tags); } catch(e) { tags = []; }
    }

    // Goal change
    var goalChangeHtml = t.goal_changed
      ? '<div class="goal-change">' + escHtml(t.goal_changed.from) + ' &rarr; ' + escHtml(t.goal_changed.to) + '</div>'
      : '';

    // Model
    var modelHtml = t.model
      ? '<div class="tick-model">' + escHtml(t.model) + '</div>'
      : '';

    // Mood badge
    var moodHtml = t.mood
      ? '<span class="mood-badge ' + moodClass(t.mood) + '">' + escHtml(t.mood) + '</span>'
      : '';

    // Outcome badge (only for non-neutral)
    var outcomeHtml = '';
    var outcome = t.outcome || 'neutral';
    if (outcome !== 'neutral') {
      var outcomeClass = 'outcome-' + outcome;
      outcomeHtml = '<span class="outcome-badge ' + outcomeClass + '">' + escHtml(outcome) + '</span>';
    }

    // Actions
    var actionsHtml = actionsArr.length
      ? actionsArr.map(function(a) {
          var f = formatAction(a);
          var cls = f.type ? 'action-chip ' + f.type : 'action-chip';
          return '<span class="' + cls + '">' + escHtml(f.text) + '</span>';
        }).join('')
      : '';

    // Tags
    var tagsHtml = '';
    if (tags.length > 0) {
      tagsHtml = '<div class="tick-tags">' +
        tags.map(function(tag) {
          return '<span class="tag-chip">' + escHtml(tag) + '</span>';
        }).join('') +
        '</div>';
    }

    // Lesson
    var lessonHtml = t.lesson
      ? '<div class="tick-lesson">' + escHtml(t.lesson) + '</div>'
      : '';

    // Memory note
    var noteHtml = t.memory_note
      ? '<div class="tick-note">' + escHtml(t.memory_note) + '</div>'
      : '';

    // Operator message
    var operatorHtml = t.operator_message
      ? '<div class="tick-operator">' + escHtml(t.operator_message) + '</div>'
      : '';

    // Prompt/Response inspect icons — store data in JS map, reference by tick num
    var inspectHtml = '';
    if (t.prompt || t.raw_response) {
      tickInspectData[t.tick] = { prompt: t.prompt || null, raw_response: t.raw_response || null };
      inspectHtml = '<div class="tick-inspect-icons">';
      if (t.prompt) inspectHtml += '<span class="inspect-btn" onclick="event.stopPropagation();showInspect(' + t.tick + ',\'prompt\')" title="View prompt">P</span>';
      if (t.raw_response) inspectHtml += '<span class="inspect-btn" onclick="event.stopPropagation();showInspect(' + t.tick + ',\'raw_response\')" title="View response">R</span>';
      inspectHtml += '</div>';
    }

    // Qualia instrumentation
    var qualia = t.qualia || {};
    var qualiaHtml = '';
    var opacityHtml = '';
    var anomalyHtml = '';
    var hasOpacity = qualia.opacity !== undefined && qualia.opacity !== null;
    var sme = t.sme || {};
    var hasAnomaly = sme.anomaly === true;

    if (qualia.continuity !== undefined && qualia.continuity !== null) {
      var pct = Math.round(qualia.continuity * 100);
      qualiaHtml += '<div class="qualia-continuity">' +
        '<span>C:' + qualia.continuity.toFixed(2) + '</span>' +
        '<span class="bar"><span class="bar-fill" style="width:' + pct + '%"></span></span>' +
        '</div>';
    }
    if (qualia.affect) qualiaHtml += '<span class="qualia-field"><strong>Affect:</strong> ' + escHtml(qualia.affect) + '</span>';
    if (qualia.surprise) qualiaHtml += '<span class="qualia-field"><strong>Surprise:</strong> ' + escHtml(qualia.surprise) + '</span>';
    if (qualiaHtml) qualiaHtml = '<div class="tick-qualia">' + qualiaHtml + '</div>';

    if (hasOpacity) {
      opacityHtml = '<div class="opacity-marker">' + escHtml(qualia.opacity) + '</div>';
    }
    if (hasAnomaly) {
      anomalyHtml = '<div class="anomaly-marker">' + escHtml(sme.anomaly_reason || 'unknown') + '</div>';
    }

    // Frame panel
    var framePanelHtml = hasFrame
      ? '<div class="tick-frame" onclick="showFullFrame(\'/frames/' + t.frame + '\', ' + JSON.stringify(JSON.stringify(t)) + ')">' +
          '<span class="tick-number">#' + t.tick + '</span>' +
          '<img src="/frames/' + t.frame + '" alt="Tick ' + t.tick + '" loading="lazy">' +
          '<span class="frame-expand">click to expand</span>' +
        '</div>'
      : '';

    if (!hasFrame) card.classList.add('no-image');
    if (hasOpacity) card.classList.add('has-opacity');
    if (hasAnomaly) card.classList.add('has-anomaly');

    // Timestamp
    var timeStr = formatTime(t.timestamp);

    card.innerHTML =
      framePanelHtml +
      '<div class="tick-body">' +
        inspectHtml +
        (!hasFrame ? '<span class="tick-number-inline">#' + t.tick + '</span>' : '') +
        '<div class="tick-header">' +
          '<div class="tick-header-left">' +
            '<div class="tick-goal">' + escHtml(t.goal) + '</div>' +
            moodHtml +
            outcomeHtml +
          '</div>' +
          '<div class="tick-time">' + escHtml(timeStr) + '</div>' +
        '</div>' +
        operatorHtml +
        '<div class="tick-obs">' + escHtml(t.observation || t.obs || '') + '</div>' +
        (t.reasoning ? '<div class="tick-reasoning">' + escHtml(t.reasoning) + '</div>' : '') +
        (t.thought ? '<div class="tick-thought">' + escHtml(t.thought) + '</div>' : '') +
        qualiaHtml +
        opacityHtml +
        anomalyHtml +
        lessonHtml +
        noteHtml +
        goalChangeHtml +
        '<div class="tick-footer">' +
          '<div class="tick-actions">' + actionsHtml + '</div>' +
          modelHtml +
        '</div>' +
        tagsHtml +
      '</div>';
    return card;
  }

  function escHtml(s) {
    if (!s) return '';
    var d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  // --- Load initial + older ticks ---
  async function loadTicks(append) {
    if (loading) return;
    loading = true;

    if (!append) {
      sentinel.innerHTML = '<div class="loading-spinner"></div>';
    }

    try {
      var resp = await fetch('/api/ticks?offset=' + offset + '&limit=30');
      var data = await resp.json();
      totalTicks = data.total;
      tickCountEl.textContent = totalTicks + ' ticks';

      for (var i = 0; i < data.ticks.length; i++) {
        var t = data.ticks[i];
        if (loadedTickIds.has(t.tick)) continue;
        loadedTickIds.add(t.tick);
        allTicks.push(t);
        feed.appendChild(renderTick(t));
      }
      offset += data.ticks.length;

      if (offset >= totalTicks) {
        sentinel.innerHTML = '';
      } else {
        sentinel.innerHTML = '<div class="loading-spinner"></div>';
      }
    } catch(e) {
      console.error('Failed to load ticks:', e);
    }
    loading = false;
  }

  // --- Infinite scroll ---
  var observer = new IntersectionObserver(function(entries) {
    if (entries[0].isIntersecting && offset < totalTicks) {
      loadTicks(true);
    }
  }, { rootMargin: '200px' });
  observer.observe(sentinel);

  // --- SSE for real-time new ticks ---
  function connectSSE() {
    var es = new EventSource('/api/stream');

    es.onopen = function() {
      statusDot.className = 'status-dot';
      statusText.textContent = 'Connected';
    };

    es.addEventListener('new_tick', function(e) {
      try {
        var t = JSON.parse(e.data);
        if (loadedTickIds.has(t.tick)) return;
        loadedTickIds.add(t.tick);
        allTicks.unshift(t);
        totalTicks++;
        offset++;
        tickCountEl.textContent = totalTicks + ' ticks';

        var card = renderTick(t);
        feed.insertBefore(card, feed.firstChild);

        if (autoScroll) {
          window.scrollTo({ top: 0, behavior: 'smooth' });
        } else {
          newTicksPending = true;
          scrollBtn.classList.add('visible');
        }
      } catch(err) {
        console.error('SSE parse error:', err);
      }
    });

    es.onerror = function() {
      statusDot.className = 'status-dot disconnected';
      statusText.textContent = 'Reconnecting...';
    };

    return es;
  }

  // --- Auto-scroll detection ---
  var scrollTimer;
  window.addEventListener('scroll', function() {
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(function() {
      autoScroll = window.scrollY < 100;
      if (autoScroll && newTicksPending) {
        newTicksPending = false;
        scrollBtn.classList.remove('visible');
      }
    }, 100);
  });

  window.scrollToTop = function() {
    window.scrollTo({ top: 0, behavior: 'smooth' });
    autoScroll = true;
    newTicksPending = false;
    scrollBtn.classList.remove('visible');
  };

  // --- Empty state ---
  function showEmpty() {
    feed.innerHTML =
      '<div class="empty-state">' +
        '<h2>Waiting for Kombucha...</h2>' +
        '<p>No ticks yet. Start the bridge on the Pi and wait for JSONL journal files to sync.</p>' +
      '</div>';
  }

  // --- Telemetry polling ---
  var telemEl = document.getElementById('telemetry');

  function updateTelemetry() {
    fetch('/api/state').then(function(r) { return r.json(); }).then(function(s) {
      var items = [];

      // Battery
      var bv = s.battery_v;
      if (bv) {
        var bclass = 'telem-value';
        if (bv < 10.5) bclass += ' danger';
        else if (bv < 11.0) bclass += ' warn';
        items.push('<div class="telem-item"><span class="telem-label">BAT</span><span class="' + bclass + '">' + bv.toFixed(2) + 'V</span></div>');
      }

      // CPU Temp
      var ct = s.cpu_temp_c;
      if (ct) {
        var tclass = 'telem-value';
        if (ct > 80) tclass += ' danger';
        else if (ct > 70) tclass += ' warn';
        items.push('<div class="telem-item"><span class="telem-label">CPU</span><span class="' + tclass + '">' + ct.toFixed(1) + '&deg;C</span></div>');
      }

      // Current tick duration
      var dur = s.last_tick_duration_s;
      if (dur) {
        items.push('<div class="telem-item"><span class="telem-label">TICK</span><span class="telem-value">' + dur.toFixed(1) + 's</span></div>');
      }

      // Next tick rate
      var ntm = s.next_tick_ms;
      if (ntm) {
        items.push('<div class="telem-item"><span class="telem-label">NEXT</span><span class="telem-value">' + (ntm / 1000).toFixed(1) + 's</span></div>');
      }

      // Mood
      if (s.mood) {
        items.push('<div class="telem-item"><span class="telem-label">MOOD</span><span class="telem-value">' + escHtml(s.mood) + '</span></div>');
      }

      // Errors
      if (s.consecutive_errors > 0) {
        items.push('<div class="telem-item"><span class="telem-label">ERR</span><span class="telem-value danger">' + s.consecutive_errors + '</span></div>');
      }

      // Tick count
      if (s.tick_count) {
        items.push('<div class="telem-item"><span class="telem-label">TICKS</span><span class="telem-value">' + s.tick_count + '</span></div>');
      }

      telemEl.innerHTML = items.join('');
    }).catch(function() {});
  }

  updateTelemetry();
  setInterval(updateTelemetry, 5000);

  // --- Chat bar (triggers a full tick with operator message) ---
  var chatBusy = false;

  window.sendChat = function() {
    var msg = chatInput.value.trim();
    if (!msg || chatBusy) return;

    chatInput.value = '';
    chatInput.style.height = 'auto';
    chatBusy = true;
    chatSendBtn.disabled = true;
    chatStatus.textContent = 'Sending to Kombucha...';

    fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg }),
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        chatStatus.textContent = 'Error: ' + data.error;
      } else {
        chatStatus.textContent = 'Tick complete';
        setTimeout(function() { chatStatus.textContent = ''; }, 3000);
      }
    })
    .catch(function(err) {
      chatStatus.textContent = 'Connection error';
    })
    .finally(function() {
      chatBusy = false;
      chatSendBtn.disabled = false;
      chatInput.focus();
    });
  };

  chatInput.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
  });

  chatInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendChat();
    }
  });

  // --- Init ---
  loadTicks(false).then(function() {
    if (allTicks.length === 0) showEmpty();
  });
  connectSSE();
})();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Kombucha Story Viewer")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default 8080)")
    parser.add_argument("--no-sync", action="store_true", help="Disable Pi sync (use local data only)")
    args = parser.parse_args()

    LOCAL_FRAMES.mkdir(parents=True, exist_ok=True)
    LOCAL_JOURNAL.mkdir(parents=True, exist_ok=True)
    LOCAL_STATE.parent.mkdir(parents=True, exist_ok=True)

    sse_broker = SSEBroker()

    sync = SyncThread(sse_broker)
    StoryHandler.sync_thread = sync
    StoryHandler.sse_broker = sse_broker

    if not args.no_sync:
        sync.start()
        print(f"[sync] Background sync started (every {SYNC_INTERVAL}s)")
        print(f"[sync] Journal: {PI_HOST}:{PI_JOURNAL_DIR} -> {LOCAL_JOURNAL}")
        print(f"[sync] Frames:  {PI_HOST}:{PI_FRAMES_DIR} -> {LOCAL_FRAMES}")
    else:
        print("[sync] Sync disabled -- using local data only")
        sync._parse_and_diff()

    server = ThreadedHTTPServer(("", args.port), StoryHandler)
    print(f"[server] Kombucha Story Viewer running at http://localhost:{args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] Shutting down...")
        sync.stop()
        server.server_close()


if __name__ == "__main__":
    main()
