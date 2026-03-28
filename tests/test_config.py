"""Tests for kombucha.config — loading, validation, env overrides."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from kombucha.config import (
    KombuchaConfig, load_config,
    SerialConfig, CameraConfig, LLMConfig, AudioConfig,
    MemoryConfig, MotionConfig, RedisConfig, LoopConfig, PathsConfig,
)


class TestDefaultConfig:
    def test_default_config_creates(self):
        config = KombuchaConfig()
        assert config.serial.port == "/dev/ttyAMA0"
        assert config.serial.baud_rate == 115200

    def test_default_camera(self):
        config = KombuchaConfig()
        assert config.camera.resolution_w == 640
        assert config.camera.resolution_h == 480
        assert config.camera.jpeg_quality == 75

    def test_default_llm(self):
        config = KombuchaConfig()
        assert "sonnet" in config.llm.model_routine
        assert "opus" in config.llm.model_deep
        assert "haiku" in config.llm.model_compression
        assert config.llm.max_tokens == 2000

    def test_default_memory(self):
        config = KombuchaConfig()
        assert config.memory.working_size == 5
        assert config.memory.compress_interval == 10
        assert config.memory.retrieval_top_k == 5

    def test_default_motion(self):
        config = KombuchaConfig()
        assert config.motion.frame_delta_threshold == 0.015
        assert config.motion.sentry_wake_threshold == 0.03

    def test_default_redis(self):
        config = KombuchaConfig()
        assert config.redis.host == "localhost"
        assert config.redis.port == 6379
        assert config.redis.key_prefix == "kombucha:"

    def test_default_loop(self):
        config = KombuchaConfig()
        assert config.loop.default_interval_s == 3.0
        assert config.loop.max_actions == 5

    def test_debug_mode_default_false(self):
        config = KombuchaConfig()
        assert config.debug_mode is False

    def test_chat_port_default(self):
        config = KombuchaConfig()
        assert config.chat_port == 8090


class TestLoadFromYAML:
    def test_load_from_file(self, tmp_path):
        yaml_content = {
            "serial": {"port": "/dev/ttyUSB0", "baud_rate": 9600},
            "camera": {"jpeg_quality": 90},
            "chat_port": 9999,
        }
        yaml_path = tmp_path / "test_config.yaml"
        yaml_path.write_text(yaml.dump(yaml_content))

        config = load_config(yaml_path)
        assert config.serial.port == "/dev/ttyUSB0"
        assert config.serial.baud_rate == 9600
        assert config.camera.jpeg_quality == 90
        assert config.chat_port == 9999

    def test_load_partial_config(self, tmp_path):
        """Partial YAML uses defaults for unspecified fields."""
        yaml_content = {"serial": {"port": "/dev/ttyUSB1"}}
        yaml_path = tmp_path / "partial.yaml"
        yaml_path.write_text(yaml.dump(yaml_content))

        config = load_config(yaml_path)
        assert config.serial.port == "/dev/ttyUSB1"
        assert config.serial.baud_rate == 115200  # default
        assert config.camera.resolution_w == 640   # default

    def test_load_empty_yaml(self, tmp_path):
        yaml_path = tmp_path / "empty.yaml"
        yaml_path.write_text("")
        config = load_config(yaml_path)
        assert config.serial.port == "/dev/ttyAMA0"

    def test_load_nonexistent_path_uses_defaults(self, tmp_path):
        config = load_config(tmp_path / "nonexistent.yaml")
        assert config.serial.port == "/dev/ttyAMA0"


class TestEnvOverrides:
    def test_env_override_section_field(self, monkeypatch, tmp_path):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(yaml.dump({"serial": {"port": "/dev/ttyAMA0"}}))

        monkeypatch.setenv("KOMBUCHA_SERIAL_PORT", "/dev/ttyUSB99")
        config = load_config(yaml_path)
        assert config.serial.port == "/dev/ttyUSB99"

    def test_env_override_flat_field(self, monkeypatch, tmp_path):
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(yaml.dump({}))

        monkeypatch.setenv("KOMBUCHA_DEBUG_MODE", "true")
        config = load_config(yaml_path)
        assert config.debug_mode is True


class TestPathResolution:
    def test_tilde_expanded_in_memory_db(self):
        config = KombuchaConfig()
        # After load_config, paths with ~ should be expanded
        raw = load_config(None)
        assert "~" not in raw.memory.db_path

    def test_tilde_expanded_in_api_key_file(self):
        raw = load_config(None)
        assert "~" not in raw.paths.api_key_file


class TestValidation:
    def test_invalid_type_raises(self, tmp_path):
        yaml_content = {"camera": {"jpeg_quality": "not_a_number"}}
        yaml_path = tmp_path / "bad.yaml"
        yaml_path.write_text(yaml.dump(yaml_content))

        with pytest.raises(Exception):
            load_config(yaml_path)

    def test_nested_section_preserves_other_defaults(self, tmp_path):
        yaml_content = {"llm": {"max_tokens": 4000}}
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(yaml.dump(yaml_content))

        config = load_config(yaml_path)
        assert config.llm.max_tokens == 4000
        assert config.llm.timeout_s == 45.0  # default preserved


class TestConfigFromProjectRoot:
    def test_load_config_from_project_config_yaml(self):
        """Test loading the actual project config.yaml."""
        project_yaml = Path(__file__).parent.parent / "config.yaml"
        if project_yaml.exists():
            config = load_config(project_yaml)
            assert config.serial.port == "/dev/ttyAMA0"
            assert config.camera.resolution_w == 640
