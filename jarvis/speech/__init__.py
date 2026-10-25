"""Local speech: microphone capture, transcription, wake word and voice output.

Everything in this package is optional at import time - a machine with no audio
hardware and no voice packs still starts JARVIS and gets a clear report of what
is missing from :meth:`SpeechService.status`.
"""

from __future__ import annotations

from .audio_io import (
    AudioCapture,
    AudioDevice,
    Utterance,
    UtteranceRecorder,
    VoiceActivityDetector,
    list_input_devices,
    rms_level,
)
from .service import (
    MODE_BUTTON,
    MODE_CONTINUOUS,
    MODE_DISABLED,
    MODE_PUSH_TO_TALK,
    MODE_WAKEWORD,
    MODES,
    SpeechService,
)
from .stt import (
    FasterWhisperSTT,
    NullSTT,
    SpeechToText,
    Transcript,
    VoskSTT,
    create_stt,
)
from .tts import (
    EspeakTTS,
    NullTTS,
    PiperTTS,
    SapiTTS,
    TextToSpeech,
    VoiceInfo,
    create_tts,
    split_sentences,
)
from .wake import WakeEvent, WakeWordListener, contains_wake_phrase

__all__ = [
    "AudioCapture", "AudioDevice", "EspeakTTS", "FasterWhisperSTT", "MODES",
    "MODE_BUTTON", "MODE_CONTINUOUS", "MODE_DISABLED", "MODE_PUSH_TO_TALK",
    "MODE_WAKEWORD", "NullSTT", "NullTTS", "PiperTTS", "SapiTTS", "SpeechService",
    "SpeechToText", "TextToSpeech", "Transcript", "Utterance", "UtteranceRecorder",
    "VoiceActivityDetector", "VoiceInfo", "VoskSTT", "WakeEvent", "WakeWordListener",
    "contains_wake_phrase", "create_stt", "create_tts", "list_input_devices",
    "rms_level", "split_sentences",
]
