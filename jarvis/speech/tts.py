"""Local neural text-to-speech.

Chain of preference:

1. **Piper** - the neural voice the design targets.  Runs on CPU, sounds
   natural, and synthesises fast enough to stream: JARVIS speaks the first
   sentence while the rest are still being generated.
2. **Windows SAPI** (``SAPI.SpVoice`` via pywin32) - always present on Windows.
   Not neural, but it means a fresh install still *talks* before the user has
   downloaded a voice.
3. **espeak-ng** - the cross-platform fallback used on Linux/macOS dev machines.

All three support barge-in: :meth:`TextToSpeech.stop` halts playback
immediately, which is what lets the user interrupt JARVIS mid-sentence.
Nothing is synthesised in the cloud and no audio leaves the machine.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import paths

log = logging.getLogger(__name__)

SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=\n)")


@dataclass
class VoiceInfo:
    id: str
    name: str
    engine: str
    language: str = "en"
    quality: str = ""
    path: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "engine": self.engine,
                "language": self.language, "quality": self.quality, "path": self.path}


def split_sentences(text: str, max_chars: int = 220) -> List[str]:
    """Split into speakable chunks.  The first chunk is deliberately short so
    speech starts as soon as possible."""
    raw = (text or "").strip()
    if not raw:
        return []
    pieces: List[str] = []
    for part in SENTENCE_END.split(raw):
        part = (part or "").strip()
        if not part:
            continue
        while len(part) > max_chars:
            cut = part.rfind(",", 0, max_chars)
            if cut < max_chars // 2:
                cut = max_chars
            pieces.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            pieces.append(part)
    if not pieces:
        return [raw[:max_chars]]
    # Start speaking sooner: if the first sentence is long, break it in half.
    if len(pieces[0]) > 90 and len(pieces) == 1:
        head, tail = pieces[0][:90], pieces[0][90:]
        split = head.rfind(" ")
        if split > 40:
            pieces = [head[:split], head[split:].strip() + tail]
    return [p for p in pieces if p]


# ---------------------------------------------------------------------------
class TextToSpeech:
    """Base class.  Speech is queued and rendered on a worker thread so callers
    (the agent loop, the UI) never block on audio."""

    name = "none"
    detail = "no text-to-speech engine available"

    def __init__(self, settings_provider: Callable[[], Any]) -> None:
        self._settings_provider = settings_provider
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stop = threading.Event()
        self._speaking = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._load_error = ""
        self.voice_id = ""
        self.load_ms = 0.0
        self.spoken_chunks = 0
        self.on_chunk: Optional[Callable[[str], None]] = None

    @property
    def settings(self) -> Any:
        return self._settings_provider()

    @property
    def available(self) -> bool:
        return False

    @property
    def is_speaking(self) -> bool:
        return self._speaking.is_set() or not self._queue.empty()

    # ------------------------------------------------------------------
    def load(self) -> bool:
        return False

    def unload(self) -> None:
        return None

    def voices(self) -> List[VoiceInfo]:
        return []

    def set_voice(self, voice_id: str) -> bool:
        self.voice_id = voice_id
        return True

    # ------------------------------------------------------------------
    def speak(self, text: str, interrupt: bool = True) -> bool:
        """Queue text for speaking.  Returns False when no engine is available."""
        text = (text or "").strip()
        if not text:
            return False
        if interrupt:
            self.stop()
        if not self.available:
            return False
        self._ensure_worker()
        if bool(self.settings.speech.tts_streaming):
            chunks = split_sentences(text, int(self.settings.speech.tts_max_chunk_chars))
        else:
            # Streaming off: synthesise the whole reply as one piece.  Slightly
            # slower to start, but the prosody across sentences is better.
            chunks = [text]
        for chunk in chunks:
            self._queue.put(chunk)
        return True

    #: Alias kept for tools that speak opportunistically.
    def speak_async(self, text: str, interrupt: bool = False) -> bool:
        return self.speak(text, interrupt=interrupt)

    def speak_blocking(self, text: str, timeout_s: float = 120.0) -> bool:
        """Speak and wait.  Used by the CLI and by tests."""
        if not self.speak(text, interrupt=True):
            return False
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if not self.is_speaking:
                return True
            time.sleep(0.02)
        return False

    def stop(self) -> None:
        """Barge-in: drop everything queued and silence what is playing now."""
        self._stop.set()
        with self._lock:
            dropped = 0
            while True:
                try:
                    self._queue.get_nowait()
                    dropped += 1
                except queue.Empty:
                    break
        self._halt_playback()
        if dropped:
            log.info("TTS interrupted (%d chunk(s) dropped)", dropped)

    def _halt_playback(self) -> None:
        """Subclasses override to kill the current output immediately."""
        return None

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="jarvis-tts",
                                            daemon=True)
            self._thread.start()

    def _worker(self) -> None:
        while True:
            try:
                chunk = self._queue.get(timeout=0.25)
            except queue.Empty:
                if self._stop.is_set():
                    self._stop.clear()
                    return
                continue
            if chunk is None:
                return
            if self._stop.is_set():
                self._stop.clear()
                return
            self._speaking.set()
            try:
                self._speak_chunk(chunk)
                self.spoken_chunks += 1
                if self.on_chunk is not None:
                    self.on_chunk(chunk)
            except Exception:  # noqa: BLE001
                log.exception("TTS chunk failed")
            finally:
                if self._queue.empty():
                    self._speaking.clear()

    def _speak_chunk(self, chunk: str) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {"engine": self.name, "available": self.available,
                "voice": self.voice_id, "detail": self.detail,
                "speaking": self.is_speaking, "error": self._load_error,
                "load_ms": round(self.load_ms, 1),
                "chunks_spoken": self.spoken_chunks}


# ---------------------------------------------------------------------------
class PiperTTS(TextToSpeech):
    """Piper neural voices, played through sounddevice (or the OS player)."""

    name = "piper"

    def __init__(self, settings_provider: Callable[[], Any]) -> None:
        super().__init__(settings_provider)
        self._voice: Any = None
        self._cli: str = ""
        self._player: Any = None
        self.detail = "Piper neural TTS"

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        if self._find_cli() or self._piper_module() is not None:
            return True
        self.detail = ("Piper is not installed. The Voice page can download a voice "
                       "once you approve it.")
        return False

    @staticmethod
    def _piper_module() -> Optional[Any]:
        try:
            import piper  # type: ignore

            return piper
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _find_cli() -> str:
        for candidate in ("piper", "piper-tts"):
            found = shutil.which(candidate)
            if found:
                return found
        local = paths.models_dir() / "piper"
        for name in ("piper.exe", "piper"):
            path = local / name
            if path.exists():
                return str(path)
        return ""

    def voices_dir(self) -> str:
        return str(paths.models_dir() / "piper-voices")

    def voices(self) -> List[VoiceInfo]:
        out: List[VoiceInfo] = []
        root = paths.models_dir() / "piper-voices"
        if not root.is_dir():
            return out
        for entry in sorted(root.iterdir()):
            if entry.suffix != ".onnx":
                continue
            config = entry.with_suffix(".onnx.json")
            language, quality = "en", ""
            if config.exists():
                try:
                    import json

                    meta = json.loads(config.read_text(encoding="utf-8"))
                    language = str((meta.get("audio") or {}).get("language")
                                   or (meta.get("language") or "en"))
                    quality = str(meta.get("quality") or "")
                except Exception:  # noqa: BLE001
                    pass
            out.append(VoiceInfo(id=entry.stem, name=entry.stem, engine=self.name,
                                 language=language, quality=quality, path=str(entry)))
        return out

    def load(self) -> bool:
        with self._lock:
            if self._loaded_voice() is not None or self._cli:
                return True
            if not self.available:
                return False
            wanted = str(self.settings.speech.tts_voice or "").strip()
            candidates: List[str] = []
            if wanted:
                candidates.append(wanted)
                if not os.path.isabs(wanted):
                    candidates.append(os.path.join(self.voices_dir(), wanted + ".onnx"))
            for voice in self.voices():
                candidates.append(voice.path)
            module = self._piper_module()
            t0 = time.time()
            for candidate in candidates:
                if not candidate or not os.path.exists(candidate):
                    continue
                if module is not None:
                    try:
                        self._voice = module.PiperVoice.load(candidate)
                        self.voice_id = os.path.basename(candidate)
                        self.load_ms = (time.time() - t0) * 1000
                        self.detail = f"Piper voice '{self.voice_id}'"
                        self._load_error = ""
                        return True
                    except Exception as exc:  # noqa: BLE001
                        self._load_error = str(exc)
                        log.debug("piper load failed for %s: %s", candidate, exc)
                self._cli = self._find_cli()
                if self._cli:
                    self.voice_id = os.path.basename(candidate)
                    self._cli_voice = candidate
                    self.load_ms = (time.time() - t0) * 1000
                    self.detail = f"Piper CLI voice '{self.voice_id}'"
                    return True
            self._load_error = (self._load_error or
                                "No Piper voice is installed yet. Download one from "
                                "the Voice page (needs your approval).")
            return False

    def _loaded_voice(self) -> Any:
        return self._voice

    def unload(self) -> None:
        with self._lock:
            self._voice = None

    # ------------------------------------------------------------------
    def _speak_chunk(self, chunk: str) -> None:
        sp = self.settings.speech
        rate = float(sp.tts_rate)
        volume = float(sp.tts_volume)
        if self._voice is not None:
            self._speak_with_module(chunk, rate, volume)
            return
        if self._cli:
            self._speak_with_cli(chunk, rate, volume)
            return
        raise RuntimeError("Piper voice is not loaded")

    def _speak_with_module(self, chunk: str, rate: float, volume: float) -> None:
        import numpy as np  # type: ignore

        frames: List[Any] = []
        synth_kwargs: Dict[str, Any] = {}
        if rate and abs(rate - 1.0) > 0.01:
            synth_kwargs["length_scale"] = 1.0 / max(0.25, rate)
        if volume and abs(volume - 1.0) > 0.01:
            synth_kwargs["volume"] = max(0.0, min(1.0, volume))
        try:
            for result in self._voice.synthesize_stream_raw(chunk, **synth_kwargs):
                frames.append(np.frombuffer(result, dtype=np.int16))
        except TypeError:
            # Older piper builds expose a different streaming API.
            audio = self._voice.synthesize(chunk)
            data = getattr(audio, "audio_float_array", None)
            if data is None:
                data = getattr(audio, "audio_int16_bytes", b"")
                frames.append(np.frombuffer(data, dtype=np.int16))
            else:
                frames.append((np.asarray(data) * 32767).astype(np.int16))
        if not frames:
            return
        pcm = np.concatenate(frames)
        rate_hz = int(getattr(self._voice, "config", {}).get("sample_rate", 22050)
                      if isinstance(getattr(self._voice, "config", None), dict)
                      else getattr(getattr(self._voice, "config", None), "sample_rate", 22050))
        self._play_int16(pcm.tobytes(), rate_hz)

    def _speak_with_cli(self, chunk: str, rate: float, volume: float) -> None:
        cmd = [self._cli, "--model", getattr(self, "_cli_voice", ""),
               "--output-raw"]
        if rate and abs(rate - 1.0) > 0.01:
            cmd += ["--length-scale", f"{1.0 / max(0.25, rate):.3f}"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        self._current_proc = proc
        try:
            out, _err = proc.communicate(input=chunk.encode("utf-8"), timeout=60)
        finally:
            self._current_proc = None
        if out:
            self._play_int16(out, 22050)

    def _play_int16(self, pcm: bytes, sample_rate: int) -> None:
        if not pcm or self._stop.is_set():
            return
        try:
            import sounddevice as sd  # type: ignore
            import numpy as np  # type: ignore

            data = np.frombuffer(pcm, dtype=np.int16)
            stream = sd.RawOutputStream(samplerate=sample_rate, channels=1,
                                        dtype="int16")
            self._player = stream
            try:
                stream.start()
                step = max(1024, sample_rate // 20)
                for i in range(0, len(data), step):
                    if self._stop.is_set():
                        break
                    stream.write(data[i:i + step].tobytes())
            finally:
                self._player = None
                try:
                    stream.stop()
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("sounddevice playback failed (%s); using a WAV file", exc)
        self._play_via_wav_file(pcm, sample_rate)

    def _play_via_wav_file(self, pcm: bytes, sample_rate: int) -> None:
        path = paths.temp_dir() / f"jarvis-tts-{int(time.time() * 1000)}.wav"
        try:
            with wave.open(str(path), "wb") as fh:
                fh.setnchannels(1)
                fh.setsampwidth(2)
                fh.setframerate(sample_rate)
                fh.writeframes(pcm)
            _play_wav_file(str(path))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _halt_playback(self) -> None:
        proc = getattr(self, "_current_proc", None)
        if proc is not None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        player = self._player
        if player is not None:
            try:
                player.stop()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
class SapiTTS(TextToSpeech):
    """Windows' built-in speech engine.  Not neural, but always available."""

    name = "sapi"

    def __init__(self, settings_provider: Callable[[], Any]) -> None:
        super().__init__(settings_provider)
        self._voice: Any = None
        self.detail = "Windows SAPI (SAPI.SpVoice)"

    @property
    def available(self) -> bool:
        if sys.platform != "win32":
            self.detail = "SAPI is only available on Windows"
            return False
        try:
            import win32com.client  # type: ignore  # noqa: F401

            return True
        except Exception as exc:  # noqa: BLE001
            self.detail = f"pywin32 is not installed ({exc})"
            return False

    def load(self) -> bool:
        with self._lock:
            if self._voice is not None:
                return True
            if not self.available:
                return False
            t0 = time.time()
            try:
                import pythoncom  # type: ignore
                import win32com.client  # type: ignore

                pythoncom.CoInitialize()
                self._voice = win32com.client.Dispatch("SAPI.SpVoice")
                wanted = str(self.settings.speech.tts_voice or "").strip().lower()
                tokens = self._voice.GetVoices()
                chosen = ""
                for i in range(int(tokens.Count)):
                    token = tokens.Item(i)
                    description = str(token.GetDescription())
                    if wanted and wanted in description.lower():
                        self._voice.Voice = token
                        chosen = description
                        break
                if not chosen and int(tokens.Count):
                    chosen = str(tokens.Item(0).GetDescription())
                self.voice_id = chosen
                self.load_ms = (time.time() - t0) * 1000
                self.detail = f"Windows SAPI voice '{chosen}'"
                self._load_error = ""
                return True
            except Exception as exc:  # noqa: BLE001
                self._load_error = str(exc)
                log.warning("SAPI load failed: %s", exc)
                return False

    def voices(self) -> List[VoiceInfo]:
        if not self.available:
            return []
        out: List[VoiceInfo] = []
        try:
            import win32com.client  # type: ignore

            voice = win32com.client.Dispatch("SAPI.SpVoice")
            tokens = voice.GetVoices()
            for i in range(int(tokens.Count)):
                token = tokens.Item(i)
                description = str(token.GetDescription())
                language = "en"
                try:
                    language = str(token.GetAttribute("Language"))
                except Exception:  # noqa: BLE001
                    pass
                out.append(VoiceInfo(id=description, name=description,
                                     engine=self.name, language=language))
        except Exception as exc:  # noqa: BLE001
            log.debug("SAPI voice enumeration failed: %s", exc)
        return out

    # ------------------------------------------------------------------
    def _speak_chunk(self, chunk: str) -> None:
        if self._voice is None and not self.load():
            raise RuntimeError("SAPI voice is not loaded")
        sp = self.settings.speech
        # SAPI rate is an integer -10..10; map the 0.5..2.0 setting onto it.
        rate = int(round((float(sp.tts_rate) - 1.0) * 10))
        self._voice.Rate = max(-10, min(10, rate))
        self._voice.Volume = int(round(max(0.0, min(1.0, float(sp.tts_volume))) * 100))
        # 1 = SVSFlagsAsync so stop() can interrupt mid-sentence.
        self._voice.Speak(chunk, 1)
        deadline = time.time() + 90.0
        while time.time() < deadline:
            if self._stop.is_set():
                return
            try:
                state = int(self._voice.Status.RunningState)
            except Exception:  # noqa: BLE001
                return
            if state == 0:            # SRSE_DONE
                return
            time.sleep(0.02)

    def _halt_playback(self) -> None:
        voice = self._voice
        if voice is None:
            return
        try:
            voice.Speak(" ", 1 | 2)   # flush + async: cuts the current utterance
        except Exception:  # noqa: BLE001
            log.debug("SAPI stop failed", exc_info=True)


# ---------------------------------------------------------------------------
#: espeak-ng voice variants we know it ships with, keyed by BCP-47 region.
_ESPEAK_VOICES = {"au": "en-au", "gb": "en-gb", "us": "en-us", "in": "en-in",
                  "ie": "en-gb", "nz": "en-gb", "za": "en-gb"}


def espeak_voice_for(settings: Any) -> str:
    """Pick an espeak-ng accent from ``speech.stt_region`` / ``general.language``.

    JARVIS is Australian by default, and the region the user configured for
    *recognition* is the accent they most likely want for *speech* too.
    """
    candidates = []
    try:
        candidates.append(str(settings.speech.stt_region or ""))
        candidates.append(str(settings.general.language or ""))
    except Exception:  # noqa: BLE001
        pass
    for tag in candidates:
        tag = tag.strip().lower()
        if not tag:
            continue
        region = tag.split("-")[-1] if "-" in tag else tag
        if region in _ESPEAK_VOICES:
            return _ESPEAK_VOICES[region]
    return "en-au"


class EspeakTTS(TextToSpeech):
    """espeak-ng: robotic but genuinely offline, and present on most Linux boxes."""

    name = "espeak-ng"

    def __init__(self, settings_provider: Callable[[], Any]) -> None:
        super().__init__(settings_provider)
        self._exe = ""
        self.detail = "espeak-ng"

    @property
    def available(self) -> bool:
        self._exe = shutil.which("espeak-ng") or shutil.which("espeak") or ""
        if self._exe:
            self.name = os.path.basename(self._exe)
            return True
        self.detail = "espeak-ng is not installed"
        return False

    def _speak_chunk(self, chunk: str) -> None:
        sp = self.settings.speech
        rate = int(max(80, min(450, 175 * float(sp.tts_rate))))
        volume = int(max(0, min(20, 20 * float(sp.tts_volume))))
        # espeak takes an integer pitch 0..99 (50 is neutral); our setting is a
        # multiplier around 1.0, so map 0.5..1.5 onto 0..100 and clamp.
        pitch = int(max(0, min(99, round(50 * float(getattr(sp, "tts_pitch", 1.0))))))
        cmd = [self._exe, "-v", espeak_voice_for(self.settings),
               "-s", str(rate), "-a", str(volume), "-p", str(pitch),
               "-w", "-", chunk]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._current_proc = proc
        try:
            wav_bytes, _ = proc.communicate(timeout=60)
        finally:
            self._current_proc = None
        if not wav_bytes:
            return
        path = paths.temp_dir() / f"jarvis-espeak-{int(time.time() * 1000)}.wav"
        try:
            path.write_bytes(wav_bytes)
            _play_wav_file(str(path))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _halt_playback(self) -> None:
        proc = getattr(self, "_current_proc", None)
        if proc is not None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
class NullTTS(TextToSpeech):
    name = "none"

    def __init__(self, reason: str = "",
                 settings_provider: Optional[Callable[[], Any]] = None) -> None:
        super().__init__(settings_provider or (lambda: None))
        self.detail = reason or "text-to-speech is disabled"
        self._load_error = reason

    def speak(self, text: str, interrupt: bool = True) -> bool:
        return False

    def _speak_chunk(self, chunk: str) -> None:
        return None


# ---------------------------------------------------------------------------
def _play_wav_file(path: str) -> None:
    """Last-resort playback: hand the file to the operating system."""
    if sys.platform == "win32":
        try:
            import winsound  # type: ignore

            winsound.PlaySound(path, winsound.SND_FILENAME)
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("winsound playback failed: %s", exc)
    for player in ("aplay", "ffplay", "paplay", "afplay"):
        exe = shutil.which(player)
        if not exe:
            continue
        args = [exe, path] if player != "ffplay" else \
            [exe, "-nodisp", "-autoexit", "-loglevel", "quiet", path]
        try:
            subprocess.run(args, check=False, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("%s playback failed: %s", player, exc)
    log.warning("No way to play audio on this machine; speech is silent.")


def create_tts(settings_provider: Callable[[], Any],
               preference: str = "auto") -> Tuple[TextToSpeech, str]:
    """Pick the best available voice engine.  Returns ``(engine, note)``.

    An engine that is installed but has no voice downloaded yet is still worth
    returning: the Voice page can offer "download a voice" for it.  It only wins
    if nothing else can speak right now.
    """
    pref = (preference or "auto").lower()
    order: Sequence[str] = ("piper", "sapi", "espeak-ng") if pref == "auto" else (pref,)
    notes: List[str] = []
    installed_but_unvoiced: List[Tuple[TextToSpeech, str]] = []

    def build(choice: str) -> Optional[TextToSpeech]:
        if choice == "piper":
            return PiperTTS(settings_provider)
        if choice in ("sapi", "windows"):
            return SapiTTS(settings_provider)
        if choice in ("espeak-ng", "espeak"):
            return EspeakTTS(settings_provider)
        return None

    for choice in order:
        if choice in ("none", "disabled", ""):
            return NullTTS("Text-to-speech is disabled in settings",
                           settings_provider), "disabled by user"
        engine = build(choice)
        if engine is None:
            notes.append(f"unknown engine '{choice}'")
            continue
        if not engine.available:
            notes.append(engine.detail)
            continue
        if engine.load():
            return engine, engine.detail
        why = engine._load_error or "installed but not usable yet"
        notes.append(f"{engine.name}: {why}")
        installed_but_unvoiced.append((engine, why))

    if installed_but_unvoiced:
        engine, why = installed_but_unvoiced[0]
        engine.detail = why
        return engine, why
    return NullTTS("; ".join(notes), settings_provider), "; ".join(notes) or "unavailable"


def describe_engines() -> List[Dict[str, Any]]:
    """Static capability report for the Voice page."""
    out: List[Dict[str, Any]] = []
    for label, probe in (
            ("piper (python)", lambda: __import__("piper")),
            ("piper (cli)", lambda: shutil.which("piper") or shutil.which("piper-tts")),
            ("sapi", lambda: __import__("win32com.client") if sys.platform == "win32"
             else (_ for _ in ()).throw(RuntimeError("Windows only"))),
            ("espeak-ng", lambda: shutil.which("espeak-ng")),
            ("sounddevice", lambda: __import__("sounddevice"))):
        try:
            found = bool(probe())
            detail = "found" if found else "not found"
        except Exception as exc:  # noqa: BLE001
            found, detail = False, str(exc).splitlines()[0][:120]
        out.append({"engine": label, "installed": found, "detail": detail})
    return out
