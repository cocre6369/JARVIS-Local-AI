"""The speech facade the rest of JARVIS talks to.

Responsibilities:

* choose the local STT and TTS engines once, at start-up, and report honestly
  what is missing;
* speak text without blocking the caller (the agent keeps working while the
  voice plays, and can be interrupted);
* run the four listening modes - wake word, push-to-talk, mic button and
  continuous dictation - plus "disabled";
* never let JARVIS hear itself: the wake listener is paused while TTS is
  playing, and barge-in stops speech the instant the user starts talking.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..events import EventType
from .audio_io import (
    AudioCapture,
    UtteranceRecorder,
    VoiceActivityDetector,
    list_input_devices,
    rms_level,
)
from .stt import SpeechToText, Transcript, create_stt, describe_engines as stt_engines
from .tts import TextToSpeech, create_tts, describe_engines as tts_engines
from .wake import WakeEvent, WakeWordListener

log = logging.getLogger(__name__)

MODE_WAKEWORD = "wakeword"
MODE_PUSH_TO_TALK = "push_to_talk"
MODE_BUTTON = "button"
MODE_CONTINUOUS = "continuous"
MODE_DISABLED = "disabled"
MODES = (MODE_WAKEWORD, MODE_PUSH_TO_TALK, MODE_BUTTON, MODE_CONTINUOUS, MODE_DISABLED)


class SpeechService:
    """One object owning every microphone and speaker path."""

    def __init__(self,
                 settings_provider: Callable[[], Any],
                 bus: Any = None,
                 guard: Any = None,
                 on_command: Optional[Callable[[str, str], None]] = None) -> None:
        self._settings_provider = settings_provider
        self.bus = bus
        self.guard = guard
        #: Called with ``(text, source)`` when the user says something JARVIS
        #: should act on.  The UI/agent wires this to ``Agent.submit``.
        self.on_command = on_command

        sp = self.settings.speech
        self.tts, self.tts_note = create_tts(settings_provider,
                                             preference=str(sp.tts_engine or "auto"))
        self.stt, self.stt_note = create_stt(settings_provider, guard=guard,
                                             preference=str(sp.stt_engine or "auto"))
        self.wake = WakeWordListener(self.stt, settings_provider,
                                     on_wake=self._on_wake,
                                     on_level=self._on_level, guard=guard)
        self.mode = str(sp.activation_mode or MODE_PUSH_TO_TALK)
        if self.mode not in MODES:
            self.mode = MODE_PUSH_TO_TALK
        self._listen_thread: Optional[threading.Thread] = None
        self._stop_listen = threading.Event()
        self._lock = threading.RLock()
        self._listening = False
        self.last_transcript = ""
        self.last_error = ""
        self.tts.on_chunk = self._on_tts_chunk

    # ------------------------------------------------------------------
    @property
    def settings(self) -> Any:
        return self._settings_provider()

    @property
    def stt_enabled(self) -> bool:
        return bool(self.settings.speech.stt_enabled) and self.mode != MODE_DISABLED

    @property
    def tts_enabled(self) -> bool:
        return bool(self.settings.speech.tts_enabled) and self.tts.available

    # ------------------------------------------------------------------
    # speaking
    # ------------------------------------------------------------------
    def speak(self, text: str, interrupt: bool = True) -> bool:
        """Queue text to be spoken.  Returns False when there is no voice."""
        if not text or not self.tts_enabled:
            return False
        self.wake.pause()
        self._emit(EventType.TTS_STARTED, {"text": text[:200], "engine": self.tts.name})
        ok = self.tts.speak(text, interrupt=interrupt)
        if not ok:
            self.wake.resume()
            self._emit(EventType.TTS_FINISHED, {"ok": False, "engine": self.tts.name})
        else:
            threading.Thread(target=self._watch_speech, daemon=True).start()
        return ok

    #: Tools call this for short, non-urgent confirmations.
    def speak_now(self, text: str, interrupt: bool = False) -> bool:
        if not self.settings.speech.speak_progress_updates and not interrupt:
            return False
        return self.speak(text, interrupt=interrupt)

    def _watch_speech(self) -> None:
        deadline = time.time() + 180.0
        while time.time() < deadline:
            if not self.tts.is_speaking:
                break
            time.sleep(0.05)
        self.wake.resume()
        self._emit(EventType.TTS_FINISHED, {"ok": True, "engine": self.tts.name})

    def _on_tts_chunk(self, chunk: str) -> None:
        # A cheap audio-reactive pulse for the core visualiser.
        self._emit(EventType.TTS_LEVEL, {"level": min(1.0, len(chunk) / 120.0),
                                         "text": chunk[:60]})

    def stop_speaking(self) -> None:
        """Barge-in."""
        self.tts.stop()
        self.wake.resume()
        self._emit(EventType.TTS_FINISHED, {"ok": False, "interrupted": True})

    #: The agent calls ``speech.stop()`` when a task is cancelled.
    stop = stop_speaking

    # ------------------------------------------------------------------
    # listening
    # ------------------------------------------------------------------
    def set_mode(self, mode: str) -> Dict[str, Any]:
        mode = (mode or "").strip().lower()
        if mode not in MODES:
            return {"ok": False, "error": f"Unknown listening mode '{mode}'. "
                                          f"Valid modes: {', '.join(MODES)}"}
        previous = self.mode
        self.mode = mode
        if previous in (MODE_WAKEWORD, MODE_CONTINUOUS):
            self.stop_listening()
        if mode in (MODE_WAKEWORD, MODE_CONTINUOUS):
            started = self.start_listening()
            return {"ok": bool(started), "mode": mode, "previous": previous,
                    "error": "" if started else self.last_error}
        return {"ok": True, "mode": mode, "previous": previous, "error": ""}

    def start_listening(self) -> bool:
        """Start the background listener (wake word or continuous dictation)."""
        if not self.stt_enabled:
            self.last_error = ("Speech input is disabled in settings, so the "
                               "microphone is not being used.")
            return False
        if self.mode == MODE_CONTINUOUS:
            return self._start_continuous()
        if self._listening:
            return True
        if not self.stt.available:
            self.last_error = self.stt.detail
            self._emit(EventType.STT_ERROR, {"error": self.last_error})
            return False
        ok = self.wake.start()
        self._listening = bool(ok)
        if not ok:
            self.last_error = self.wake.last_error or "could not open the microphone"
            self._emit(EventType.STT_ERROR, {"error": self.last_error})
        else:
            self._emit(EventType.LISTENING_STARTED, {"mode": self.mode,
                                                     "backend": self.wake.backend})
        return ok

    def stop_listening(self) -> None:
        with self._lock:
            self._stop_listen.set()
            if self._listening:
                self.wake.stop()
                self._listening = False
                self._emit(EventType.LISTENING_STOPPED, {"mode": self.mode})

    def _start_continuous(self) -> bool:
        """Dictation mode: everything heard is treated as a command."""
        if self._listen_thread is not None and self._listen_thread.is_alive():
            return True
        if not self.stt.available:
            self.last_error = self.stt.detail
            return False
        self._stop_listen.clear()
        self._listening = True
        self._listen_thread = threading.Thread(target=self._continuous_loop,
                                               name="jarvis-dictation", daemon=True)
        self._listen_thread.start()
        self._emit(EventType.LISTENING_STARTED, {"mode": MODE_CONTINUOUS})
        return True

    def _continuous_loop(self) -> None:
        sp = self.settings.speech
        capture = AudioCapture(sample_rate=int(sp.sample_rate or 16000),
                               device=str(sp.microphone or ""),
                               frame_ms=int(sp.vad_frame_ms or 30))
        if not capture.open():
            self.last_error = capture.detail
            self._listening = False
            self._emit(EventType.STT_ERROR, {"error": self.last_error})
            return
        vad = VoiceActivityDetector(aggressiveness=int(sp.vad_aggressiveness),
                                    frame_ms=int(sp.vad_frame_ms or 30),
                                    sample_rate=int(sp.sample_rate or 16000),
                                    rms_threshold=float(sp.wake_rms_threshold))
        recorder = UtteranceRecorder(capture, vad, min_speech_s=0.35,
                                     max_speech_s=15.0, hangover_s=0.8)
        try:
            while not self._stop_listen.is_set():
                utterance = recorder.record(timeout_s=3.0, stop=self._stop_listen,
                                            on_level=self._on_level)
                if utterance is None:
                    continue
                transcript = self.stt.transcribe(utterance.pcm, utterance.sample_rate)
                self._emit(EventType.STT_FINAL, transcript.as_dict())
                if not transcript.ok:
                    continue
                self.last_transcript = transcript.text
                self._dispatch(transcript.text, MODE_CONTINUOUS)
        finally:
            capture.close()
            self._listening = False

    # ------------------------------------------------------------------
    def listen_once(self, timeout_s: float = 12.0, source: str = "button") -> Transcript:
        """Push-to-talk / mic button: capture one utterance and transcribe it."""
        if not self.stt.available:
            self.last_error = self.stt.detail
            self._emit(EventType.STT_ERROR, {"error": self.last_error})
            return Transcript(engine=self.stt.name)
        if self.tts_enabled and self.settings.speech.barge_in:
            self.stop_speaking()
        self.wake.pause()
        self._emit(EventType.LISTENING_STARTED, {"mode": source})
        sp = self.settings.speech
        capture = AudioCapture(sample_rate=int(sp.sample_rate or 16000),
                               device=str(sp.microphone or ""),
                               frame_ms=int(sp.vad_frame_ms or 30))
        transcript = Transcript(engine=self.stt.name)
        if not capture.open():
            self.last_error = capture.detail
            self._emit(EventType.STT_ERROR, {"error": self.last_error})
            self._emit(EventType.LISTENING_STOPPED, {"mode": source})
            self.wake.resume()
            return transcript
        try:
            vad = VoiceActivityDetector(aggressiveness=int(sp.vad_aggressiveness),
                                        frame_ms=int(sp.vad_frame_ms or 30),
                                        sample_rate=int(sp.sample_rate or 16000),
                                        rms_threshold=float(sp.wake_rms_threshold))
            recorder = UtteranceRecorder(capture, vad, min_speech_s=0.3,
                                         max_speech_s=max(4.0, timeout_s),
                                         hangover_s=0.7)
            utterance = recorder.record(timeout_s=timeout_s, on_level=self._on_level)
            if utterance is None:
                self.last_error = "No speech was detected."
                self._emit(EventType.STT_ERROR, {"error": self.last_error})
                return transcript
            transcript = self.stt.transcribe(utterance.pcm, utterance.sample_rate)
        finally:
            capture.close()
            self._emit(EventType.LISTENING_STOPPED, {"mode": source})
            self.wake.resume()
        self._emit(EventType.STT_FINAL, transcript.as_dict())
        if transcript.ok:
            self.last_transcript = transcript.text
        else:
            self.last_error = "Nothing was recognised."
        return transcript

    # ------------------------------------------------------------------
    def _on_wake(self, event: WakeEvent) -> None:
        self._emit(EventType.WAKEWORD_DETECTED, event.as_dict())
        if self.tts_enabled and self.settings.speech.barge_in:
            self.stop_speaking()
        if event.command.strip():
            # "Hey JARVIS, play something" - the command came with the wake word.
            self._dispatch(event.command.strip(), MODE_WAKEWORD)
            return
        self._emit(EventType.NOTIFICATION, {
            "title": "Listening",
            "message": f"{self.settings.general.assistant_name} is listening…"})
        transcript = self.listen_once(timeout_s=10.0, source=MODE_WAKEWORD)
        if transcript.ok:
            self._dispatch(transcript.text, MODE_WAKEWORD)

    def _dispatch(self, text: str, source: str) -> None:
        text = (text or "").strip()
        if not text or self.on_command is None:
            return
        try:
            self.on_command(text, source)
        except Exception:  # noqa: BLE001
            log.exception("speech command handler failed")

    def _on_level(self, level: float) -> None:
        self._emit(EventType.AUDIO_LEVEL, {"level": round(float(level), 4)})

    def _emit(self, event_type: EventType, data: Any) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(event_type, data, source="speech")
        except Exception:  # noqa: BLE001
            log.debug("speech event emit failed", exc_info=True)

    # ------------------------------------------------------------------
    def devices(self) -> Dict[str, Any]:
        found, note = list_input_devices()
        return {"devices": [d.as_dict() for d in found], "backend": note,
                "selected": str(self.settings.speech.microphone or "")}

    def voices(self) -> List[Dict[str, Any]]:
        out = [v.as_dict() for v in self.tts.voices()]
        try:
            from .tts import SapiTTS

            if self.tts.name != "sapi":
                sapi = SapiTTS(self._settings_provider)
                if sapi.available:
                    out += [v.as_dict() for v in sapi.voices()]
        except Exception:  # noqa: BLE001
            pass
        return out

    def status(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "modes": list(MODES),
            "listening": self._listening or self.wake.running,
            "stt_enabled": bool(self.settings.speech.stt_enabled),
            "tts_enabled": bool(self.settings.speech.tts_enabled),
            "stt": self.stt.status(),
            "tts": self.tts.status(),
            "wake": self.wake.status(),
            "speaking": self.tts.is_speaking,
            "last_transcript": self.last_transcript[:160],
            "last_error": self.last_error or self.wake.last_error,
            "notes": {"stt": self.stt_note, "tts": self.tts_note},
        }

    def engines(self) -> Dict[str, Any]:
        return {"stt": stt_engines(), "tts": tts_engines()}

    def preload(self) -> None:
        """Warm the engines at start-up so the first reply is immediate."""
        if self.settings.speech.preload_tts and self.tts.available:
            try:
                self.tts.load()
            except Exception:  # noqa: BLE001
                log.debug("TTS preload failed", exc_info=True)
        # The STT model is heavy: only pre-load it when the user actually wants
        # always-on listening, and only if the resource guard agrees.
        wants_always_on = self.mode in (MODE_WAKEWORD, MODE_CONTINUOUS)
        if wants_always_on and self.stt.available:
            if self.guard is None or self.guard.can_allocate(1.2):
                try:
                    self.stt.load()
                except Exception:  # noqa: BLE001
                    log.debug("STT preload failed", exc_info=True)

    def shutdown(self) -> None:
        self.stop_listening()
        self.stop_speaking()
        try:
            self.stt.unload()
            self.tts.unload()
        except Exception:  # noqa: BLE001
            log.debug("speech shutdown failed", exc_info=True)
