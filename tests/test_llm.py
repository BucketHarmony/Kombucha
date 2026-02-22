"""Tests for kombucha.llm — LLMClient, response parsing, JSON repair."""

import json

import pytest

from kombucha.config import LLMConfig
from kombucha.llm import LLMClient, parse_brain_response, _repair_truncated_json


@pytest.fixture
def llm_config():
    return LLMConfig()


@pytest.fixture
def client(llm_config):
    return LLMClient(llm_config)


# ===========================================================================
# Model Selection
# ===========================================================================

class TestModelSelection:
    def test_first_tick_uses_deep(self, client):
        model = client.select_model(tick_number=1)
        assert model == client.config.model_deep

    def test_regular_tick_uses_routine(self, client):
        model = client.select_model(tick_number=5)
        assert model == client.config.model_routine

    def test_every_20th_uses_deep(self, client):
        model = client.select_model(tick_number=20)
        assert model == client.config.model_deep
        model = client.select_model(tick_number=40)
        assert model == client.config.model_deep

    def test_errors_use_deep(self, client):
        model = client.select_model(tick_number=5, consecutive_errors=3)
        assert model == client.config.model_deep

    def test_motion_wake_uses_deep(self, client):
        model = client.select_model(tick_number=5, wake_reason="motion_detected")
        assert model == client.config.model_deep

    def test_operator_message_uses_deep(self, client):
        model = client.select_model(tick_number=5, has_operator_message=True)
        assert model == client.config.model_deep

    def test_no_speech_no_upgrade(self, client):
        model = client.select_model(tick_number=5, has_speech=True)
        # Speech alone doesn't upgrade to deep in current logic
        assert model == client.config.model_routine


# ===========================================================================
# Response Parsing
# ===========================================================================

class TestResponseParsing:
    def test_parse_clean_json(self):
        api_resp = {
            "content": [{"text": '{"observation": "test", "mood": "ok"}'}]
        }
        result = parse_brain_response(api_resp)
        assert result["observation"] == "test"

    def test_parse_with_markdown_fences(self):
        api_resp = {
            "content": [{"text": '```json\n{"observation": "test"}\n```'}]
        }
        result = parse_brain_response(api_resp)
        assert result["observation"] == "test"

    def test_parse_with_just_backticks(self):
        api_resp = {
            "content": [{"text": '```\n{"observation": "test"}\n```'}]
        }
        result = parse_brain_response(api_resp)
        assert result["observation"] == "test"


# ===========================================================================
# JSON Repair
# ===========================================================================

class TestJSONRepair:
    def test_repair_truncated_single_field(self):
        # When truncated mid-value after a completed field + comma,
        # the repair chops back to the comma and produces valid JSON.
        text = '{"observation": "test", "goal": "explore the ha'
        repaired = _repair_truncated_json(text)
        result = json.loads(repaired)
        assert result["observation"] == "test"

    def test_repair_truncated_after_comma(self):
        text = '{"observation": "test", "mood": "cur'
        repaired = _repair_truncated_json(text)
        result = json.loads(repaired)
        assert result["observation"] == "test"

    def test_repair_truncated_array(self):
        text = '{"actions": [{"type": "drive"}, {"type": "lo'
        repaired = _repair_truncated_json(text)
        result = json.loads(repaired)
        assert len(result["actions"]) >= 1

    def test_repair_valid_json_unchanged(self):
        text = '{"observation": "test"}'
        repaired = _repair_truncated_json(text)
        result = json.loads(repaired)
        assert result["observation"] == "test"

    def test_repair_nested_braces(self):
        text = '{"qualia": {"continuity": 0.5, "affect": "wa'
        repaired = _repair_truncated_json(text)
        result = json.loads(repaired)
        assert "qualia" in result

    def test_repair_handles_escaped_quotes(self):
        text = '{"thought": "She said \\"hello\\"", "mood": "ha'
        repaired = _repair_truncated_json(text)
        result = json.loads(repaired)
        assert "hello" in result["thought"]
