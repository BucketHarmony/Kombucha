"""
audio_device.py — Auto-detect USB audio playback and capture devices.

Survives reboots where card numbers change due to USB enumeration order.
Finds devices by name/type rather than hardcoded card number.
"""

import subprocess
import logging
import re

log = logging.getLogger(__name__)

# Cached results
_playback_device = None
_capture_device = None


def find_playback_device() -> str:
    """Find the USB PnP Audio Device for playback. Returns ALSA device string."""
    global _playback_device
    if _playback_device:
        return _playback_device

    try:
        result = subprocess.run(
            ["aplay", "-l"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.split("\n"):
            # Look for USB audio device (not HDMI, not Pi built-in)
            if "USB" in line and ("PnP" in line or "Audio Device" in line):
                match = re.match(r"card (\d+):", line)
                if match:
                    card = match.group(1)
                    _playback_device = f"plughw:{card},0"
                    log.info(f"Audio playback device: {_playback_device} ({line.strip()})")
                    return _playback_device
    except Exception as e:
        log.warning(f"Audio device detection failed: {e}")

    # Fallback: try common card numbers
    for card in [3, 4, 2, 5]:
        dev = f"plughw:{card},0"
        try:
            result = subprocess.run(
                ["aplay", "-D", dev, "-d", "0", "/dev/null"],
                capture_output=True, timeout=2)
            if result.returncode == 0:
                _playback_device = dev
                log.info(f"Audio playback device (fallback): {dev}")
                return dev
        except Exception:
            continue

    log.warning("No USB audio playback device found, using plughw:3,0")
    _playback_device = "plughw:3,0"
    return _playback_device


def find_capture_device() -> str:
    """Find the USB audio capture device (microphone). Returns ALSA device string."""
    global _capture_device
    if _capture_device:
        return _capture_device

    try:
        result = subprocess.run(
            ["arecord", "-l"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.split("\n"):
            if "USB" in line and ("PnP" in line or "Audio Device" in line):
                match = re.match(r"card (\d+):", line)
                if match:
                    card = match.group(1)
                    _capture_device = f"plughw:{card},0"
                    log.info(f"Audio capture device: {_capture_device}")
                    return _capture_device
            # Camera mic as fallback
            if "USB" in line and "Camera" in line:
                match = re.match(r"card (\d+):", line)
                if match:
                    card = match.group(1)
                    _capture_device = f"plughw:{card},0"
                    log.info(f"Audio capture device (camera mic): {_capture_device}")
                    return _capture_device
    except Exception as e:
        log.warning(f"Audio capture detection failed: {e}")

    _capture_device = "plughw:3,0"
    return _capture_device


def reset():
    """Clear cached devices — call after USB changes."""
    global _playback_device, _capture_device
    _playback_device = None
    _capture_device = None
