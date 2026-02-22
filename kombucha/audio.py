"""Audio module for Kombucha v2.

Handles text-to-speech (gTTS, future Piper), speech-to-text listeners
(Vosk, faster-whisper), and audio device management.
"""

import json
import logging
import subprocess
import threading
from datetime import datetime
from typing import Optional

from kombucha.config import AudioConfig

log = logging.getLogger("kombucha.audio")

# Optional imports — guarded for testing
try:
    from vosk import Model as VoskModel, KaldiRecognizer
    import pyaudio
    HAS_VOSK = True
except ImportError:
    HAS_VOSK = False

try:
    from faster_whisper import WhisperModel
    HAS_WHISPER = True
except ImportError:
    HAS_WHISPER = False

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import piper
    HAS_PIPER = True
except ImportError:
    HAS_PIPER = False


def speak_async(text: str, config: AudioConfig, debug_mode: bool = False):
    """Fire-and-forget TTS to USB PnP speaker.

    Supports gTTS (cloud) and Piper (local) backends via config.tts_engine.
    """
    if debug_mode:
        log.info(f'  [DEBUG] WOULD SPEAK: "{text}"')
        return

    engine = getattr(config, "tts_engine", "gtts")

    if engine == "piper" and HAS_PIPER:
        _speak_piper(text, config)
    else:
        _speak_gtts(text, config)


def _speak_gtts(text: str, config: AudioConfig):
    """TTS via gTTS (cloud, requires internet)."""
    safe_text = text.replace("'", "'\\''")
    try:
        subprocess.Popen(
            [
                "bash", "-c",
                f"python3 -c 'from gtts import gTTS; "
                f"tts = gTTS(text=\"\"\"{safe_text}\"\"\", lang=\"en\"); "
                f"tts.save(\"/tmp/kombucha_tts.mp3\")' && "
                f"ffmpeg -y -i /tmp/kombucha_tts.mp3 -f wav -acodec pcm_s16le "
                f"/tmp/kombucha_tts.wav 2>/dev/null && "
                f"aplay -D {config.speaker_device} /tmp/kombucha_tts.wav 2>/dev/null"
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info(f'  Speaking (gTTS): "{text[:60]}"')
    except Exception as e:
        log.warning(f"gTTS failed: {e}")


def _speak_piper(text: str, config: AudioConfig):
    """TTS via Piper (local, fast, no internet needed)."""
    model_path = getattr(config, "piper_model_path",
                         str(__import__("pathlib").Path.home() / "kombucha" / "models" / "piper" / "en_US-lessac-medium.onnx"))
    try:
        subprocess.Popen(
            [
                "bash", "-c",
                f"echo '{text}' | "
                f"piper --model {model_path} --output_file /tmp/kombucha_tts.wav && "
                f"aplay -D {config.speaker_device} /tmp/kombucha_tts.wav 2>/dev/null"
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info(f'  Speaking (Piper): "{text[:60]}"')
    except Exception as e:
        log.warning(f"Piper TTS failed, falling back to gTTS: {e}")
        _speak_gtts(text, config)


class SpeechListener(threading.Thread):
    """Always-on background STT via Vosk."""

    def __init__(self, model_path, device_index=None, sample_rate=16000):
        super().__init__(daemon=True)
        self._model = VoskModel(str(model_path))
        self._recognizer = KaldiRecognizer(self._model, sample_rate)
        self._sample_rate = sample_rate
        self._device_index = device_index
        self._buffer = []
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def drain(self):
        """Return all transcripts since last drain, then clear."""
        with self._lock:
            items = self._buffer[:]
            self._buffer.clear()
        return items

    def stop(self):
        self._stop.set()

    def run(self):
        pa = pyaudio.PyAudio()
        chunk = self._sample_rate // 4
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self._sample_rate,
                input=True,
                input_device_index=self._device_index,
                frames_per_buffer=chunk,
            )
            while not self._stop.is_set():
                data = stream.read(chunk, exception_on_overflow=False)
                if self._recognizer.AcceptWaveform(data):
                    result = json.loads(self._recognizer.Result())
                    text = result.get("text", "").strip()
                    if text:
                        with self._lock:
                            self._buffer.append({
                                "time": datetime.now().strftime("%H:%M:%S"),
                                "text": text,
                            })
        except Exception as e:
            log.warning(f"STT listener error: {e}")
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            pa.terminate()


class WhisperSpeechListener(threading.Thread):
    """Always-on background STT via faster-whisper with Silero VAD."""

    WINDOW_SECONDS = 5

    def __init__(self, model_size="tiny", device_index=None, sample_rate=48000):
        super().__init__(daemon=True)
        self._model = WhisperModel(model_size, device="cpu", compute_type="int8")
        self._sample_rate = sample_rate
        self._device_index = device_index
        self._buffer = []
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def drain(self):
        """Return all transcripts since last drain, then clear."""
        with self._lock:
            items = self._buffer[:]
            self._buffer.clear()
        return items

    def stop(self):
        self._stop.set()

    def run(self):
        pa = pyaudio.PyAudio()
        chunk = self._sample_rate // 4
        chunks_per_window = self.WINDOW_SECONDS * 4

        channels = 1
        if self._device_index is not None:
            dev_info = pa.get_device_info_by_index(self._device_index)
            if dev_info.get("maxInputChannels", 1) >= 2:
                channels = 2
        self._channels = channels

        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=self._sample_rate,
                input=True,
                input_device_index=self._device_index,
                frames_per_buffer=chunk,
            )

            log.info(f"Whisper audio stream open: {channels}ch @ {self._sample_rate}Hz, device={self._device_index}")

            window_frames = []

            while not self._stop.is_set():
                data = stream.read(chunk, exception_on_overflow=False)

                audio_i16 = np.frombuffer(data, dtype=np.int16)
                if channels == 2:
                    audio_i16 = ((audio_i16[0::2].astype(np.float32)
                                  + audio_i16[1::2].astype(np.float32)) / 2).astype(np.int16)

                window_frames.append(audio_i16)

                if len(window_frames) >= chunks_per_window:
                    audio_i16_all = np.concatenate(window_frames)
                    if self._sample_rate == 48000:
                        audio_i16_all = audio_i16_all[::3]
                    elif self._sample_rate != 16000:
                        ratio = self._sample_rate / 16000
                        indices = np.arange(0, len(audio_i16_all), ratio).astype(int)
                        audio_i16_all = audio_i16_all[indices]
                    audio_f32 = audio_i16_all.astype(np.float32) / 32768.0
                    window_frames = []
                    self._transcribe(audio_f32)

        except Exception as e:
            log.warning(f"Whisper STT listener error: {e}")
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            pa.terminate()

    def _transcribe(self, audio_f32):
        """Transcribe float32 mono audio via faster-whisper with Silero VAD."""
        try:
            rms = float(np.sqrt(np.mean(audio_f32 ** 2)))
            peak = float(np.max(np.abs(audio_f32)))
            log.debug(f"Audio stats: RMS={rms:.6f} Peak={peak:.4f} len={len(audio_f32)}")
            segments, _ = self._model.transcribe(
                audio_f32,
                beam_size=1,
                language="en",
                vad_filter=True,
                vad_parameters=dict(
                    threshold=0.3,
                    min_silence_duration_ms=500,
                    speech_pad_ms=200,
                ),
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            if text:
                log.info(f"Whisper transcribed: {text}")
                with self._lock:
                    self._buffer.append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "text": text,
                    })
        except Exception as e:
            log.warning(f"Whisper transcription error: {e}")
