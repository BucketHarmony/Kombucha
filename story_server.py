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

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PI_HOST = "bucket@192.168.4.226"
PI_JOURNAL_DIR = "~/kombucha/data/journal/"
PI_FRAMES_DIR = "~/kombucha/frames/"
PI_STATE_FILE = "~/kombucha/state.json"

LOCAL_DIR = Path(__file__).parent
LOCAL_JOURNAL = LOCAL_DIR / "data" / "journal"
LOCAL_FRAMES = LOCAL_DIR / "frames"
LOCAL_STATE = LOCAL_DIR / "data" / "state.json"

SYNC_INTERVAL = 8  # seconds between syncs

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
    """Find the JPEG frame for a given tick number."""
    pattern = f"tick_{tick_num:05d}_*.jpg"
    matches = list(frames_dir.glob(pattern))
    if matches:
        return matches[0].name
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
        # 1. Pull JSONL journal files
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

        # 2. Pull new frames
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
  }

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
    padding: 16px;
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
    flex: 0 0 280px;
    min-height: 210px;
    background: #0d1117;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
  }

  .tick-frame img {
    width: 100%;
    height: 100%;
    object-fit: cover;
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
</header>

<div id="feed"></div>
<div id="sentinel"></div>

<button class="auto-scroll-badge" id="scrollBtn" onclick="scrollToTop()">
  New ticks above
</button>

<script>
(function() {
  const feed = document.getElementById('feed');
  const sentinel = document.getElementById('sentinel');
  const statusDot = document.getElementById('statusDot');
  const statusText = document.getElementById('statusText');
  const tickCountEl = document.getElementById('tickCount');
  const scrollBtn = document.getElementById('scrollBtn');

  let allTicks = [];
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

    // Frame panel
    var framePanelHtml = hasFrame
      ? '<div class="tick-frame">' +
          '<span class="tick-number">#' + t.tick + '</span>' +
          '<img src="/frames/' + t.frame + '" alt="Tick ' + t.tick + '" loading="lazy">' +
        '</div>'
      : '';

    if (!hasFrame) card.classList.add('no-image');

    // Timestamp
    var timeStr = formatTime(t.timestamp);

    card.innerHTML =
      framePanelHtml +
      '<div class="tick-body">' +
        (!hasFrame ? '<span class="tick-number-inline">#' + t.tick + '</span>' : '') +
        '<div class="tick-header">' +
          '<div class="tick-header-left">' +
            '<div class="tick-goal">' + escHtml(t.goal) + '</div>' +
            moodHtml +
            outcomeHtml +
          '</div>' +
          '<div class="tick-time">' + escHtml(timeStr) + '</div>' +
        '</div>' +
        '<div class="tick-obs">' + escHtml(t.observation || t.obs || '') + '</div>' +
        (t.reasoning ? '<div class="tick-reasoning">' + escHtml(t.reasoning) + '</div>' : '') +
        (t.thought ? '<div class="tick-thought">' + escHtml(t.thought) + '</div>' : '') +
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
