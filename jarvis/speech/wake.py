"""Wake word and listening modes.

"Hey JARVIS" is detected the honest way for a local assistant: the microphone is
gated by VAD, short utterances are transcribed locally, and the transcript is
compared against the configured wake phrases.  That costs a little CPU while
listening but needs no extra model download, and it works with whichever STT
engine is installed.

If the optional ``openwakeword`` package *is* installed it is used instead,
because a dedicated wake model is far cheaper to keep running than a
transcriber.  Either way :attr:`WakeWordListener.backend` says which one is
active, and the Voice page shows it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .audio_io import AudioCapture, UtteranceRecorder, VoiceActivityDetector, rms_level
from .stt import SpeechToText

log = logging.getLogger(__name__)

DEFAULT_WAKE_PHRASES = ("hey jarvis", "ok jarvis", "jarvis")

#: Words that must not be mistaken for the wake phrase.
_PUNCT = re.compile(r"[^a-z0-9\s]")


def normalise(text: str) -> str:
    return _PUNCT.sub(" ", (text or "").lower()).strip()


def contains_wake_phrase(text: str, phrases: Sequence[str]) -> Tuple[str, str]:
    """Return ``(matched_phrase, remainder_after_phrase)``."""
    haystack = normalise(text)
    if not haystack:
        return "", ""
    best = ""
    for phrase in phrases or ():
        candidate = normalise(str(phrase))
        if not candidate:
            continue
        if candidate in haystack and len(candidate) > len(best):
            best = candidate
    if not best:
        return "", ""
    index = haystack.find(best)
    remainder = haystack[index + len(best):].strip(" ,.!?")
    return best, remainder


@dataclass
class WakeEvent:
    phrase: str
    command: str
    transcript: str
    confidence: float = 0.0
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"phrase": self.phrase, "command": self.command,
                "transcript": self.transcript, "confidence": round(self.confidence, 3),
                "ts": self.ts}


class WakeWordListener:
    """Background listener.  Runs on its own daemon thread."""

    def __init__(self,
                 stt: SpeechToText,
                 settings_provider: Callable[[], Any],
                 on_wake: Optional[Callable[[WakeEvent], None]] = None,
                 on_level: Optional[Callable[[float], None]] = None,
                 guard: Any = None) -> None:
        self.stt = stt
        self._settings_provider = settings_provider
        self.on_wake = on_wake
        self.on_level = on_level
        self.guard = guard
        self.backend = "stt-phrase"
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self.capture: Optional[AudioCapture] = None
        self.recorder: Optional[UtteranceRecorder] = None
        self.vad: Optional[VoiceActivityDetector] = None
        self.last_error = ""
        self.wake_count = 0
        self.last_transcript = ""
        self._oww: Any = None
        self._oww_names: Tuple[str, ...] = ()
        self._oww_error = ""

    @property
    def settings(self) -> Any:
        return self._settings_provider()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    # ------------------------------------------------------------------
    def phrases(self) -> List[str]:
        configured = list(getattr(self.settings.speech, "wake_phrases", []) or [])
        return [p for p in configured if str(p).strip()] or list(DEFAULT_WAKE_PHRASES)

    def start(self) -> bool:
        if self.running:
            return True
        if not self.stt.available and self._oww is None:
            self.last_error = ("Wake word needs a speech-to-text engine. "
                               + self.stt.detail)
            return False
        sp = self.settings.speech
        self._stop.clear()
        self._paused.clear()
        self.capture = AudioCapture(sample_rate=int(sp.sample_rate or 16000),
                                    device=str(sp.microphone or ""),
                                    frame_ms=int(sp.vad_frame_ms or 30))
        if not self.capture.open():
            self.last_error = self.capture.detail
            self.capture = None
            return False
        self.vad = VoiceActivityDetector(aggressiveness=int(sp.vad_aggressiveness),
                                         frame_ms=int(sp.vad_frame_ms or 30),
                                         sample_rate=int(sp.sample_rate or 16000),
                                         rms_threshold=float(sp.wake_rms_threshold))
        self.recorder = UtteranceRecorder(self.capture, self.vad,
                                          min_speech_s=0.25, max_speech_s=12.0,
                                          hangover_s=0.6)
        loop = self._run
        if self._init_oww():
            self.backend = "openwakeword"
            loop = self._run_oww
        else:
            self.backend = "stt-phrase"
        self._thread = threading.Thread(target=loop, name="jarvis-wake",
                                        daemon=True)
        self._thread.start()
        log.info("Wake word listening (%s, phrases: %s)", self.backend,
                 ", ".join(self.phrases()))
        return True

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        if self.recorder is not None:
            self.recorder = None
        if self.capture is not None:
            self.capture.close()
            self.capture = None
        self._oww = None
        self._oww_names = ()

    # ------------------------------------------------------------------
    # optional dedicated wake model
    # ------------------------------------------------------------------
    def _init_oww(self) -> bool:
        """Use ``openwakeword`` when it is installed *and* a model is present.

        openwakeword's own bundled models are downloaded by its package on first
        use, which would be a network fetch without the user's approval, so
        JARVIS only uses model files that are already in its local models
        directory.
        """
        from .. import paths

        try:
            import numpy as np  # type: ignore  # noqa: F401
            import openwakeword  # type: ignore
        except Exception as exc:  # noqa: BLE001
            self._oww_error = f"openwakeword unavailable ({exc})"
            return False
        root = paths.models_dir() / "wake"
        models: List[str] = []
        if root.is_dir():
            for entry in sorted(root.iterdir()):
                if entry.suffix in (".onnx", ".tflite"):
                    models.append(str(entry))
        if not models:
            self._oww_error = (f"openwakeword is installed but no wake model is in "
                               f"{root}")
            return False
        try:
            self._oww = openwakeword.Model(wakeword_models=models,
                                           inference_framework="onnx")
            self._oww_names = tuple(self._oww.models.keys())
            self._oww_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self._oww_error = f"openwakeword failed to load ({exc})"
            self._oww = None
            return False

    def _run_oww(self) -> None:
        import array

        assert self.capture is not None
        threshold = 0.5
        frame_samples = int(self.capture.sample_rate * 0.08)     # 80 ms
        cooldown_until = 0.0
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(0.1)
                continue
            pcm = b""
            deadline = time.time() + 2.0
            while len(pcm) < frame_samples * 2 and time.time() < deadline:
                chunk = self.capture.read(max_wait_s=0.2)
                if chunk:
                    pcm += chunk
                    self._emit_level(rms_level(chunk))
            if not pcm:
                continue
            samples = array.array("h")
            try:
                samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
            except Exception:  # noqa: BLE001
                continue
            if not samples:
                continue
            try:
                import numpy as np  # type: ignore

                scores = self._oww.predict(np.asarray(samples, dtype=np.int16))
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.debug("openwakeword predict failed: %s", exc)
                self._oww = None
                self.backend = "stt-phrase"
                self._thread = threading.Thread(target=self._run, name="jarvis-wake",
                                                daemon=True)
                self._thread.start()
                return
            for name, score in (scores or {}).items():
                if float(score) < threshold or time.time() < cooldown_until:
                    continue
                cooldown_until = time.time() + 2.0
                self.wake_count += 1
                event = WakeEvent(phrase=name, command="", transcript="",
                                  confidence=float(score))
                log.info("Wake model fired: %s (%.2f)", name, float(score))
                if self.on_wake is not None:
                    try:
                        self.on_wake(event)
                    except Exception:  # noqa: BLE001
                        log.exception("wake callback failed")

    def pause(self) -> None:
        """Used while JARVIS is speaking or the user is typing."""
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    # ------------------------------------------------------------------
    def _run(self) -> None:
        assert self.recorder is not None
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(0.1)
                continue
            if self.guard is not None:
                try:
                    if not self.guard.can_allocate(0.4):
                        time.sleep(1.0)
                        continue
                except Exception:  # noqa: BLE001
                    pass
            try:
                utterance = self.recorder.record(
                    timeout_s=4.0, stop=self._stop,
                    on_level=self._emit_level)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.debug("wake capture failed: %s", exc)
                time.sleep(0.5)
                continue
            if utterance is None:
                continue
            self._handle_utterance(utterance)

    def _emit_level(self, level: float) -> None:
        if self.on_level is not None:
            try:
                self.on_level(level)
            except Exception:  # noqa: BLE001
                pass

    def _handle_utterance(self, utterance: Any) -> None:
        # Very short bursts are almost never a wake phrase; skip transcription
        # and save the CPU.
        if utterance.duration_s < 0.35:
            return
        transcript = self.stt.transcribe(utterance.pcm, utterance.sample_rate)
        self.last_transcript = transcript.text
        if not transcript.ok:
            return
        phrase, remainder = contains_wake_phrase(transcript.text, self.phrases())
        if not phrase:
            return
        self.wake_count += 1
        event = WakeEvent(phrase=phrase, command=remainder,
                          transcript=transcript.text,
                          confidence=transcript.confidence)
        log.info("Wake word '%s' heard (command: %r)", phrase, remainder)
        if self.on_wake is not None:
            try:
                self.on_wake(event)
            except Exception:  # noqa: BLE001
                log.exception("wake callback failed")

    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {"running": self.running, "paused": self.paused,
                "backend": self.backend, "phrases": self.phrases(),
                "vad": self.vad.name if self.vad else "",
                "wake_count": self.wake_count, "last_error": self.last_error,
                "wake_model_error": self._oww_error,
                "wake_models": list(self._oww_names),
                "last_transcript": self.last_transcript[:120]}
