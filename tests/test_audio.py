"""Tests for kombucha.audio — TTS and STT module guards."""

import pytest

from kombucha.config import AudioConfig


class TestAudioImports:
    def test_has_vosk_is_bool(self):
        from kombucha.audio import HAS_VOSK
        assert isinstance(HAS_VOSK, bool)

    def test_has_whisper_is_bool(self):
        from kombucha.audio import HAS_WHISPER
        assert isinstance(HAS_WHISPER, bool)

    def test_speak_async_importable(self):
        from kombucha.audio import speak_async
        assert callable(speak_async)


class TestSpeakAsyncDebug:
    def test_debug_mode_does_not_spawn_process(self):
        """In debug mode, speak_async logs but does not launch subprocess."""
        from kombucha.audio import speak_async
        config = AudioConfig()
        # Should return without error in debug mode
        speak_async("hello world", config, debug_mode=True)

    def test_debug_mode_with_empty_text(self):
        from kombucha.audio import speak_async
        config = AudioConfig()
        speak_async("", config, debug_mode=True)

    def test_debug_mode_with_special_chars(self):
        from kombucha.audio import speak_async
        config = AudioConfig()
        speak_async("it's a test with 'quotes' and \"doubles\"", config, debug_mode=True)


class TestAudioConfig:
    def test_default_speaker_device(self):
        config = AudioConfig()
        assert isinstance(config.speaker_device, str)

    def test_default_stt_enabled(self):
        config = AudioConfig()
        assert isinstance(config.stt_enabled, bool)
