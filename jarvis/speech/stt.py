"""Local speech-to-text.

Chain of preference:

1. ``faster-whisper`` (CTranslate2) - what the design targets: fast on CPU with
   ``int8``, better on the GPU, and the ``*.en`` models handle Australian
   English well when biased with an initial prompt.
2. ``vosk`` - small offline models, lower quality but very light; used when
   ctranslate2 cannot be installed.
3. nothing - the UI then says plainly that voice input needs the voice pack.

Nothing here touches the network.  Models are read from the local cache
(``models/`` inside JARVIS's data directory) and are only ever downloaded by the
Models page *after* the user approves the download.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .. import paths
from .audio_io import pcm_to_float32, resample

log = logging.getLogger(__name__)

#: Whisper's own sample rate.  Capturing at 16 kHz avoids a resample entirely.
WHISPER_RATE = 16000

#: Accent biasing used when the user has not written their own prompt.  Whisper
#: has no "en-AU" language code, so the accent is steered through the prompt.
_REGION_BIAS = {
    "au": ("Australian English. The speaker uses Australian spelling and "
           "vocabulary: colour, favourite, realise, mobile, service station."),
    "gb": ("British English. The speaker uses British spelling and vocabulary: "
           "colour, favourite, realise, lift, motorway."),
    "nz": ("New Zealand English. The speaker uses British spelling: colour, "
           "favourite, realise."),
    "us": ("American English. The speaker uses American spelling and vocabulary: "
           "color, favorite, realize, apartment, freeway."),
    "in": ("Indian English. The speaker uses British spelling: colour, favourite, "
           "realise."),
}


def region_bias_prompt(region: str) -> str:
    """Accent biasing text for a BCP-47 region such as ``en-AU``."""
    tag = (region or "").strip().lower()
    if not tag:
        return ""
    key = tag.split("-")[-1] if "-" in tag else tag
    return _REGION_BIAS.get(key, _REGION_BIAS.get(tag, ""))


@dataclass
class Transcript:
    text: str = ""
    language: str = ""
    duration_s: float = 0.0
    engine: str = ""
    confidence: float = 0.0
    segments: List[Dict[str, Any]] = field(default_factory=list)
    took_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())

    def as_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "language": self.language,
                "duration_s": round(self.duration_s, 2), "engine": self.engine,
                "confidence": round(self.confidence, 3),
                "took_ms": round(self.took_ms, 1),
                "segments": self.segments[:12]}


class SpeechToText:
    """Base class.  Subclasses must implement :meth:`transcribe`."""

    name = "none"
    detail = "no speech-to-text engine available"

    def __init__(self, settings_provider: Callable[[], Any], guard: Any = None) -> None:
        self._settings_provider = settings_provider
        self.guard = guard
        self._lock = threading.RLock()
        self._loaded = False
        self._load_error = ""
        self.model_id = ""
        self.load_ms = 0.0

    @property
    def settings(self) -> Any:
        return self._settings_provider()

    @property
    def available(self) -> bool:
        return False

    @property
    def loaded(self) -> bool:
        return self._loaded

    # ------------------------------------------------------------------
    def load(self) -> bool:
        return False

    def unload(self) -> None:
        with self._lock:
            self._loaded = False

    def transcribe(self, pcm: bytes, sample_rate: int = WHISPER_RATE) -> Transcript:
        raise NotImplementedError

    def status(self) -> Dict[str, Any]:
        return {"engine": self.name, "available": self.available,
                "loaded": self._loaded, "model": self.model_id,
                "load_ms": round(self.load_ms, 1), "detail": self.detail,
                "error": self._load_error}


# ---------------------------------------------------------------------------
class FasterWhisperSTT(SpeechToText):
    name = "faster-whisper"

    def __init__(self, settings_provider: Callable[[], Any], guard: Any = None) -> None:
        super().__init__(settings_provider, guard)
        self._model: Any = None
        self.detail = "faster-whisper (CTranslate2)"

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        try:
            import faster_whisper  # type: ignore  # noqa: F401

            return True
        except Exception as exc:  # noqa: BLE001
            self.detail = f"faster-whisper is not installed ({exc})"
            return False

    def _resolve_device(self) -> tuple:
        sp = self.settings.speech
        device = str(sp.stt_device or "cpu").lower()
        compute = str(sp.stt_compute_type or "int8")
        if device == "auto":
            device = "cuda" if self._cuda_available() else "cpu"
        if device == "cuda" and not self._cuda_available():
            log.info("CUDA requested for STT but not usable; falling back to CPU")
            device, compute = "cpu", "int8"
        if device == "cpu" and compute in ("float16", "int8_float16"):
            compute = "int8"
        return device, compute

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import ctranslate2  # type: ignore

            return bool(ctranslate2.get_cuda_device_count())
        except Exception:  # noqa: BLE001
            return False

    def load(self) -> bool:
        with self._lock:
            if self._loaded and self._model is not None:
                return True
            if not self.available:
                return False
            if self.guard is not None and not self.guard.can_allocate(1.2):
                self._load_error = ("Not enough free memory to load the speech model "
                                    "right now (resource ceiling in effect).")
                log.warning(self._load_error)
                return False
            sp = self.settings.speech
            size = str(sp.stt_model_size or "base.en")
            device, compute = self._resolve_device()
            t0 = time.time()
            try:
                from faster_whisper import WhisperModel  # type: ignore

                self._model = WhisperModel(
                    size, device=device, compute_type=compute,
                    download_root=str(paths.models_dir()),
                    local_files_only=not _allow_model_download())
                self.model_id = f"{size}@{device}/{compute}"
                self._loaded = True
                self.load_ms = (time.time() - t0) * 1000
                self._load_error = ""
                self.detail = f"faster-whisper {size} on {device} ({compute})"
                log.info("STT loaded: %s in %.0fms", self.model_id, self.load_ms)
                return True
            except Exception as exc:  # noqa: BLE001
                self._load_error = str(exc)
                self._loaded = False
                self._model = None
                log.warning("faster-whisper load failed: %s", exc)
                return False

    def unload(self) -> None:
        with self._lock:
            self._model = None
            self._loaded = False

    # ------------------------------------------------------------------
    def transcribe(self, pcm: bytes, sample_rate: int = WHISPER_RATE) -> Transcript:
        t0 = time.time()
        if not pcm:
            return Transcript(engine=self.name)
        if not self.load():
            return Transcript(engine=self.name,
                              text="",
                              duration_s=len(pcm) / max(1, sample_rate * 2))
        if sample_rate != WHISPER_RATE:
            pcm = resample(pcm, sample_rate, WHISPER_RATE)
        try:
            audio = pcm_to_float32(pcm)
        except Exception as exc:  # noqa: BLE001
            log.warning("audio conversion failed: %s", exc)
            return Transcript(engine=self.name)

        sp = self.settings.speech
        kwargs: Dict[str, Any] = {
            "beam_size": max(1, int(sp.beam_size)),
            "vad_filter": False,          # we already gated with our own VAD
            "language": (sp.stt_language or "en").split("-")[0] or "en",
        }
        prompt = (sp.initial_prompt or "").strip()
        if not prompt:
            # No biasing text configured: derive one from the region so an
            # Australian speaker still gets Australian spellings ("colour",
            # " favourite") rather than US ones.
            prompt = region_bias_prompt(getattr(sp, "stt_region", ""))
        if prompt:
            kwargs["initial_prompt"] = prompt[:600]
        try:
            segments, info = self._model.transcribe(audio, **kwargs)
            texts: List[str] = []
            out_segments: List[Dict[str, Any]] = []
            confidence = 0.0
            for seg in segments:
                text = str(getattr(seg, "text", "") or "").strip()
                if not text:
                    continue
                texts.append(text)
                out_segments.append({"start": round(float(seg.start), 2),
                                     "end": round(float(seg.end), 2), "text": text})
                confidence = max(confidence, float(getattr(seg, "avg_logprob", -1.0) + 1.0))
            text = " ".join(texts).strip()
            return Transcript(text=text,
                              language=str(getattr(info, "language", "") or ""),
                              duration_s=float(getattr(info, "duration", 0.0) or 0.0),
                              engine=self.name,
                              confidence=max(0.0, min(1.0, confidence)),
                              segments=out_segments,
                              took_ms=(time.time() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001
            log.warning("transcription failed: %s", exc)
            self._load_error = str(exc)
            return Transcript(engine=self.name, took_ms=(time.time() - t0) * 1000)


# ---------------------------------------------------------------------------
class VoskSTT(SpeechToText):
    name = "vosk"

    def __init__(self, settings_provider: Callable[[], Any], guard: Any = None) -> None:
        super().__init__(settings_provider, guard)
        self._recogniser: Any = None
        self._vosk: Any = None
        self.detail = "vosk (small offline model)"

    @property
    def model_path(self) -> str:
        candidate = os.environ.get("JARVIS_VOSK_MODEL", "")
        if candidate and os.path.isdir(candidate):
            return candidate
        root = paths.models_dir() / "vosk"
        if root.is_dir():
            for entry in sorted(root.iterdir()):
                if entry.is_dir():
                    return str(entry)
        return ""

    @property
    def available(self) -> bool:
        try:
            import vosk  # type: ignore

            self._vosk = vosk
            if not self.model_path:
                self.detail = ("vosk is installed but no model is present. Download "
                               "one from the Voice page (needs your approval).")
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            self.detail = f"vosk is not installed ({exc})"
            return False

    def load(self) -> bool:
        with self._lock:
            if self._loaded and self._recogniser is not None:
                return True
            if not self.available:
                return False
            t0 = time.time()
            try:
                self._vosk.SetLogLevel(-1)
                model = self._vosk.Model(self.model_path)
                rate = int(self.settings.speech.sample_rate or 16000)
                self._recogniser = self._vosk.KaldiRecognizer(model, rate)
                self._recogniser.SetWords(False)
                self._loaded = True
                self.model_id = os.path.basename(self.model_path)
                self.load_ms = (time.time() - t0) * 1000
                self._load_error = ""
                return True
            except Exception as exc:  # noqa: BLE001
                self._load_error = str(exc)
                self._loaded = False
                log.warning("vosk load failed: %s", exc)
                return False

    def unload(self) -> None:
        with self._lock:
            self._recogniser = None
            self._loaded = False

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> Transcript:
        t0 = time.time()
        if not pcm or not self.load():
            return Transcript(engine=self.name)
        import json as _json

        try:
            self._recogniser.Reset()
            chunk = max(1600, int(sample_rate * 0.2) * 2)
            for i in range(0, len(pcm), chunk):
                self._recogniser.AcceptWaveform(pcm[i:i + chunk])
            final = _json.loads(self._recogniser.Result() or "{}")
            text = str(final.get("text") or "").strip()
            if not text:
                final = _json.loads(self._recogniser.FinalResult() or "{}")
                text = str(final.get("text") or "").strip()
            return Transcript(text=text, language="en",
                              duration_s=len(pcm) / max(1, sample_rate * 2),
                              engine=self.name, took_ms=(time.time() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001
            log.warning("vosk transcription failed: %s", exc)
            self._load_error = str(exc)
            return Transcript(engine=self.name, took_ms=(time.time() - t0) * 1000)


# ---------------------------------------------------------------------------
class NullSTT(SpeechToText):
    name = "none"

    def __init__(self, reason: str = "", settings_provider: Optional[Callable[[], Any]] = None) -> None:
        super().__init__(settings_provider or (lambda: None))
        self.detail = reason or "speech-to-text is disabled"
        self._load_error = reason

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> Transcript:
        return Transcript(engine=self.name)


# ---------------------------------------------------------------------------
def _allow_model_download() -> bool:
    """Model files are only ever fetched with the user's explicit approval.

    The Models/Voice pages set this environment flag for the duration of an
    approved download; nothing else may.
    """
    return os.environ.get("JARVIS_ALLOW_MODEL_DOWNLOAD", "").lower() in ("1", "true", "yes")


def create_stt(settings_provider: Callable[[], Any],
               guard: Any = None,
               preference: str = "auto") -> tuple:
    """Pick the best available engine.  Returns ``(engine, note)``."""
    pref = (preference or "auto").lower()
    order: Sequence[str] = ("faster-whisper", "vosk") if pref == "auto" else (pref,)
    notes: List[str] = []
    for choice in order:
        if choice in ("none", "disabled", ""):
            return NullSTT("Speech-to-text is disabled in settings",
                           settings_provider), "disabled by user"
        engine: SpeechToText
        if choice == "faster-whisper":
            engine = FasterWhisperSTT(settings_provider, guard)
        elif choice == "vosk":
            engine = VoskSTT(settings_provider, guard)
        else:
            notes.append(f"unknown engine '{choice}'")
            continue
        if engine.available:
            return engine, engine.detail
        notes.append(engine.detail)
    return NullSTT("; ".join(notes), settings_provider), "; ".join(notes) or "unavailable"


def describe_engines() -> List[Dict[str, Any]]:
    """Static capability report for the Voice page."""
    out = []
    for engine_name, module_name, package in (
            ("faster-whisper", "faster_whisper", "faster-whisper"),
            ("vosk", "vosk", "vosk"),
            ("webrtcvad", "webrtcvad", "webrtcvad"),
            ("sounddevice", "sounddevice", "sounddevice")):
        try:
            __import__(module_name)
            installed, detail = True, "installed"
        except Exception as exc:  # noqa: BLE001
            installed, detail = False, str(exc).splitlines()[0][:120]
        out.append({"engine": engine_name, "installed": installed,
                    "package": package, "detail": detail})
    return out
