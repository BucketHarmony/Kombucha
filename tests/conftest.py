"""Shared test fixtures for Kombucha v2 tests."""

import os
import tempfile
from pathlib import Path

import pytest

from kombucha.config import KombuchaConfig, load_config, RedisConfig
from kombucha.redis_bus import RedisBus, FakeRedis


@pytest.fixture
def tmp_config(tmp_path):
    """KombuchaConfig with all paths pointing to tmp_path."""
    return KombuchaConfig(
        memory=KombuchaConfig.model_fields["memory"].default_factory().model_copy(update={
            "db_path": str(tmp_path / "memory.db"),
            "journal_dir": str(tmp_path / "journal"),
            "state_file": str(tmp_path / "state.json"),
        }),
        paths=KombuchaConfig.model_fields["paths"].default_factory().model_copy(update={
            "data_dir": str(tmp_path),
            "frame_log_dir": str(tmp_path / "frames"),
        }),
    )


@pytest.fixture
def fake_redis():
    """A FakeRedis instance for testing."""
    return FakeRedis()


@pytest.fixture
def redis_bus(fake_redis):
    """A RedisBus backed by FakeRedis."""
    config = RedisConfig()
    return RedisBus(config, client=fake_redis)
