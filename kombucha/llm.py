"""LLM client for Kombucha v2.

Handles API calls to the Claude API, model selection, response parsing,
and JSON repair for truncated responses.
"""

import json
import logging
from datetime import datetime
from typing import Optional

from kombucha.config import LLMConfig

log = logging.getLogger("kombucha.llm")


def _repair_truncated_json(text):
    """Attempt to close truncated JSON so it parses.

    Strategy: chop back to the last comma or opening brace/bracket that
    precedes the truncation point, then close any open structures.
    """
    t = text.rstrip()

    last_cut = 0
    in_str = False
    escape = False
    for i, ch in enumerate(t):
        if escape:
            escape = False
            continue
        if ch == '\\' and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in (',', '{', '['):
            last_cut = i

    if last_cut > 0:
        t = t[:last_cut + 1]

    t = t.rstrip().rstrip(",")

    stack = []
    in_str = False
    escape = False
    for ch in t:
        if escape:
            escape = False
            continue
        if ch == '\\' and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            stack.append('}')
        elif ch == '[':
            stack.append(']')
        elif ch in ('}', ']') and stack and stack[-1] == ch:
            stack.pop()

    t += ''.join(reversed(stack))
    return t


def parse_brain_response(api_resp):
    """Parse the brain's JSON response, repairing truncation if needed."""
    text = api_resp["content"][0]["text"].strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        stop = api_resp.get("stop_reason", "")
        if stop == "max_tokens":
            log.warning("Response truncated (max_tokens), attempting JSON repair")
        else:
            log.warning("Malformed JSON from LLM, attempting repair")
        repaired = _repair_truncated_json(text)
        return json.loads(repaired)


class LLMClient:
    """Client for Claude API calls."""

    def __init__(self, config: LLMConfig):
        self.config = config

    def select_model(self, tick_number: int, consecutive_errors: int = 0,
                     wake_reason: str = None, has_speech: bool = False,
                     has_operator_message: bool = False) -> str:
        """Choose which model to use for this tick."""
        if has_operator_message:
            return self.config.model_deep
        if tick_number == 1:
            return self.config.model_deep
        if consecutive_errors >= self.config.deep_error_threshold:
            return self.config.model_deep
        if tick_number % self.config.deep_tick_interval == 0:
            return self.config.model_deep
        if wake_reason == "motion_detected":
            return self.config.model_deep
        return self.config.model_routine

    async def call_brain(self, client, api_key: str, frame_b64: str,
                         state: dict, memory_context: str,
                         system_prompt: str, model: str = None,
                         sme: dict = None, heard: list = None,
                         operator_message: str = None):
        """Call the mind with full memory context.

        Returns: (api_json, model_used, prompt_text, raw_response)
        """
        if model is None:
            model = self.config.model_routine

        tick_input = {
            "tick": state["tick_count"],
            "current_goal": state["goal"],
            "last_result": state.get("last_result", "none"),
            "pan_position": state.get("pan_position", 0),
            "tilt_position": state.get("tilt_position", 0),
            "wake_reason": state.get("wake_reason"),
            "time": datetime.now().strftime("%H:%M"),
        }

        prev_actions = state.get("last_actions") or []
        spoken = [a.get("text") for a in prev_actions
                  if isinstance(a, dict) and a.get("type") == "speak" and a.get("text")]
        if spoken:
            tick_input["last_spoken"] = spoken[-1]
        cmds_sent = [a for a in prev_actions
                     if isinstance(a, dict) and a.get("type") != "speak"]
        if cmds_sent:
            tick_input["last_commands_sent"] = cmds_sent

        if sme and sme.get("frame_delta") is not None:
            tick_input["self_model_error"] = sme
            if sme.get("anomaly"):
                tick_input["self_model_anomaly"] = sme["anomaly_reason"]

        if heard:
            tick_input["heard"] = heard

        if operator_message:
            tick_input["operator_message"] = operator_message

        text_parts = []
        if memory_context.strip():
            text_parts.append(memory_context)
        text_parts.append("=== CURRENT TICK ===")
        text_parts.append(json.dumps(tick_input, indent=2))

        user_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": frame_b64,
                }
            },
            {
                "type": "text",
                "text": "\n".join(text_parts),
            }
        ]

        prompt_text = "\n".join(text_parts)

        resp = await client.post(
            self.config.api_url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": self.config.api_version,
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": self.config.max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_content}],
            },
            timeout=self.config.timeout_s,
        )
        resp.raise_for_status()
        api_json = resp.json()
        raw_response = api_json.get("content", [{}])[0].get("text", "")
        return api_json, model, prompt_text, raw_response

    def parse_response(self, api_resp):
        """Parse the brain's JSON response."""
        return parse_brain_response(api_resp)
