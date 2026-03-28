"""Configuration management for Kombucha v2.

Loads from config.yaml, overridable with KOMBUCHA_* environment variables.
Validates at load time — fails loudly on bad config.
"""

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field
import yaml


class SerialConfig(BaseModel):
    port: str = "/dev/ttyAMA0"
    baud_rate: int = 115200
    cmd_delay_s: float = 0.05
    reconnect_attempts: int = 3
    wheel_base_m: float = 0.2


class CameraConfig(BaseModel):
    device_index: int = 0
    resolution_w: int = 640
    resolution_h: int = 480
    jpeg_quality: int = 75
    warmup_frames: int = 5
    drain_frames: int = 4
    frame_log_max: int = 500
    fps_target: int = 10


class LLMConfig(BaseModel):
    api_url: str = "https://api.anthropic.com/v1/messages"
    api_version: str = "2023-06-01"
    model_routine: str = "claude-sonnet-4-5-20250929"
    model_deep: str = "claude-opus-4-6"
    model_compression: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 2000
    timeout_s: float = 45.0
    compression_timeout_s: float = 30.0
    tertiary_timeout_s: float = 60.0
    deep_tick_interval: int = 20
    deep_error_threshold: int = 3


class AudioConfig(BaseModel):
    stt_backend: str = "whisper"
    stt_enabled: bool = True
    mic_device_index: int = 0
    sample_rate: int = 48000
    whisper_model_size: str = "tiny"
    vosk_model_path: str = "~/kombucha/models/vosk-model-small-en-us-0.15"
    tts_engine: str = "gtts"
    piper_model_path: str = "~/kombucha/models/piper/en_US-lessac-medium.onnx"
    speaker_device: str = "plughw:3,0"
    echo_gate_tail_s: float = 1.5
    vad_threshold: float = 0.3


class MemoryConfig(BaseModel):
    db_path: str = "~/kombucha/data/memory.db"
    journal_dir: str = "~/kombucha/data/journal"
    state_file: str = "~/kombucha/state.json"
    working_size: int = 5
    compress_interval: int = 10
    retrieval_top_k: int = 5
    tag_weight_overlap: float = 3.0
    tag_weight_success: float = 2.0
    tag_weight_failure: float = 2.0
    tag_weight_lesson: float = 2.5
    retrieval_scan_limit: int = 300


class MotionConfig(BaseModel):
    frame_delta_threshold: float = 0.015
    sentry_wake_threshold: float = 0.03
    sentry_entry_s: float = 10.0
    anomaly_threshold: float = 0.08


class RedisConfig(BaseModel):
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    key_prefix: str = "kombucha:"
    scene_ttl_s: int = 10
    hardware_ttl_s: int = 10
    motor_ttl_s: int = 30


class LoopConfig(BaseModel):
    default_interval_s: float = 3.0
    max_actions: int = 5
    min_tick_ms: int = 2000
    max_tick_ms: int = 60000
    tertiary_cooldown_s: int = 300


class PathsConfig(BaseModel):
    data_dir: str = "~/kombucha/data"
    frame_log_dir: str = "~/kombucha/frames"
    prompts_dir: str = "prompts"
    models_dir: str = "~/kombucha/models"
    api_key_file: str = "~/.config/kombucha/api_key"


class KombuchaConfig(BaseModel):
    serial: SerialConfig = Field(default_factory=SerialConfig)
    camera: CameraConfig = Field(default_factory=CameraConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    motion: MotionConfig = Field(default_factory=MotionConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    chat_port: int = 8090
    debug_mode: bool = False


def _apply_env_overrides(data: dict) -> dict:
    """Apply KOMBUCHA_* environment variable overrides.

    Env var naming: KOMBUCHA_SECTION_FIELD, e.g. KOMBUCHA_SERIAL_PORT,
    KOMBUCHA_LLM_MODEL_ROUTINE, KOMBUCHA_CAMERA_JPEG_QUALITY.

    Flat config fields use KOMBUCHA_FIELD, e.g. KOMBUCHA_CHAT_PORT.
    """
    prefix = "KOMBUCHA_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        parts = key[len(prefix):].lower().split("_", 1)
        if len(parts) == 2:
            section, field = parts
            if section in data and isinstance(data[section], dict):
                data[section][field] = value
            else:
                # Flat field with underscore, e.g. KOMBUCHA_DEBUG_MODE
                flat_key = "_".join(parts)
                data[flat_key] = value
        elif len(parts) == 1:
            data[parts[0]] = value
    return data


def _resolve_paths(config: KombuchaConfig) -> KombuchaConfig:
    """Expand ~ in all path fields."""
    config.memory.db_path = str(Path(config.memory.db_path).expanduser())
    config.memory.journal_dir = str(Path(config.memory.journal_dir).expanduser())
    config.memory.state_file = str(Path(config.memory.state_file).expanduser())
    config.paths.data_dir = str(Path(config.paths.data_dir).expanduser())
    config.paths.frame_log_dir = str(Path(config.paths.frame_log_dir).expanduser())
    config.paths.models_dir = str(Path(config.paths.models_dir).expanduser())
    config.paths.api_key_file = str(Path(config.paths.api_key_file).expanduser())
    config.audio.vosk_model_path = str(Path(config.audio.vosk_model_path).expanduser())
    return config


def load_config(path: str | Path | None = None) -> KombuchaConfig:
    """Load config from YAML file, apply env overrides, validate.

    Args:
        path: Path to config.yaml. If None, looks for config.yaml in the
              current directory and then in the directory of this file.

    Returns:
        Validated KombuchaConfig instance.

    Raises:
        FileNotFoundError: If no config file found and no default exists.
        pydantic.ValidationError: If config values are invalid.
    """
    data = {}
    if path is not None:
        path = Path(path)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
    else:
        # Search in current dir, then package parent dir
        for candidate in [Path("config.yaml"), Path(__file__).parent.parent / "config.yaml"]:
            if candidate.exists():
                with open(candidate, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                break

    data = _apply_env_overrides(data)
    config = KombuchaConfig(**data)
    config = _resolve_paths(config)
    return config
