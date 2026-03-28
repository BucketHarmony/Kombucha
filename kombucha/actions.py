"""Action translation for Kombucha v2.

Translates high-level LLM output actions (display, oled, speak) into
validated ESP32 T-code commands. Motor commands (drive, look, lights)
are now handled directly via MotorCommand.
"""

import json
import logging
import time
from typing import Optional

from kombucha.serial_manager import SerialManager, validate_tcode

log = logging.getLogger("kombucha.actions")


def translate_action(action, state):
    """Translate a high-level LLM action into validated T-code dicts.

    Only handles display/oled/speak actions. Motor commands (drive, look,
    lights, stop) are now handled via MotorCommand directly.
    """
    if not isinstance(action, dict):
        log.warning(f"Action is not a dict: {action!r}")
        return []

    action_type = action.get("type", "")
    results = []

    if action_type == "display":
        lines = action.get("lines", ["", "", "", ""])
        for i, text in enumerate(lines[:4]):
            cmd = validate_tcode(3, {"lineNum": i, "Text": str(text)})
            if cmd:
                results.append(cmd)

    elif action_type == "oled":
        line = int(action.get("line", 0))
        text = str(action.get("text", ""))
        cmd = validate_tcode(3, {"lineNum": line, "Text": text})
        if cmd:
            results.append(cmd)

    elif action_type == "oled_reset":
        cmd = validate_tcode(-3, {})
        if cmd:
            results.append(cmd)

    elif action_type == "speak":
        # Speech is handled externally — just a no-op for T-codes
        pass

    else:
        log.warning(f"Unknown action type: {action_type!r}")

    return results


def execute_actions(serial: SerialManager, actions: list, state: dict,
                    max_actions: int = 5, speak_fn=None) -> str:
    """Translate and execute a list of high-level actions (display/oled/speak only)."""
    if not actions:
        return "no_actions"

    actions = actions[:max_actions]
    results = []

    for action in actions:
        # Handle speak separately
        if isinstance(action, dict) and action.get("type") == "speak":
            text = str(action.get("text", ""))
            if text and speak_fn:
                speak_fn(text)
            continue

        tcodes = translate_action(action, state)
        for cmd in tcodes:
            if serial.debug_mode:
                serial.send(cmd)
                results.append("debug_ok")
                continue
            if not serial.is_connected:
                results.append("no_serial")
                continue
            try:
                serial.send(cmd)
                results.append("ok")
            except Exception as e:
                log.error(f"Action execution error: {e}")
                results.append("error")

    return ", ".join(results) if results else "no_actions"
