"""Microphone capture and voice-activity detection.

Two backends are tried in order:

* ``sounddevice`` (PortAudio) - the one JARVIS prefers, low latency, gives
  float32 frames straight into the transcriber.
* ``pyaudio`` - the fallback for machines where PortAudio wheels are awkward.

If neither is installed, capture reports itself unavailable and the UI says so
plainly ("install the voice pack") instead of pretending to listen.

VAD uses ``webrtcvad`` when present and falls back to an energy gate, which is
cruder but honest: it is labelled ``energy`` in :meth:`VoiceActivityDetector.name`
so the Voice page can tell the user which one is running.
"""

from __future__ import annotations

import array
import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

SAMPLE_WIDTH = 2          # 16-bit PCM
CHANNELS = 1


# ---------------------------------------------------------------------------
@dataclass
class AudioDevice:
    index: int
    name: str
    channels: int = 1
    default_rate: int = 16000
    is_default: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "name": self.name, "channels": self.channels,
                "default_rate": self.default_rate, "is_default": self.is_default}


def list_input_devices() -> Tuple[List[AudioDevice], str]:
    """Enumerate microphones.  Returns ``(devices, note)``."""
    devices: List[AudioDevice] = []
    try:
        import sounddevice as sd  # type: ignore

        for index, info in enumerate(sd.query_devices()):
            if int(info.get("max_input_channels", 0)) > 0:
                devices.append(AudioDevice(
                    index=index, name=str(info.get("name", f"device {index}")),
                    channels=int(info.get("max_input_channels", 1)),
                    default_rate=int(info.get("default_samplerate", 16000))))
        try:
            default_input = sd.default.device[0]
            for d in devices:
                d.is_default = (d.index == default_input)
        except Exception:  # noqa: BLE001
            pass
        if devices:
            return devices, "sounddevice (PortAudio)"
    except Exception as exc:  # noqa: BLE001
        log.debug("sounddevice enumeration failed: %s", exc)

    try:
        import pyaudio  # type: ignore

        pa = pyaudio.PyAudio()
        try:
            default_index = pa.get_default_input_device_info().get("index")
            for index in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(index)
                if int(info.get("maxInputChannels", 0)) > 0:
                    devices.append(AudioDevice(
                        index=index, name=str(info.get("name", f"device {index}")),
                        channels=int(info.get("maxInputChannels", 1)),
                        default_rate=int(info.get("defaultSampleRate", 16000)),
                        is_default=(index == default_index)))
        finally:
            pa.terminate()
        if devices:
            return devices, "pyaudio (PortAudio)"
    except Exception as exc:  # noqa: BLE001
        log.debug("pyaudio enumeration failed: %s", exc)

    return [], ("No audio backend is installed. Voice input needs the optional "
                "'sounddevice' package (installed with the voice pack).")


# ---------------------------------------------------------------------------
class AudioCapture:
    """Blocking-free microphone reader producing 16-bit mono PCM."""

    def __init__(self, sample_rate: int = 16000, device: str = "",
                 frame_ms: int = 30) -> None:
        self.sample_rate = int(sample_rate)
        self.device = device
        self.frame_ms = int(frame_ms)
        self.backend = "none"
        self.detail = "not opened"
        self._stream: Any = None
        self._pa: Any = None
        self._queue: "queue.Queue[bytes]" = queue.Queue(maxsize=400)
        self._lock = threading.RLock()
        self._open_error = ""

    # ------------------------------------------------------------------
    @property
    def frame_bytes(self) -> int:
        return int(self.sample_rate * self.frame_ms / 1000) * SAMPLE_WIDTH

    def open(self) -> bool:
        with self._lock:
            if self._stream is not None:
                return True
            device_index: Optional[int] = None
            if self.device not in ("", None):
                try:
                    device_index = int(self.device)
                except (TypeError, ValueError):
                    device_index = None
            if self._try_sounddevice(device_index):
                return True
            if self._try_pyaudio(device_index):
                return True
            self.backend = "none"
            self.detail = self._open_error or "no audio backend available"
            return False

    def _try_sounddevice(self, device_index: Optional[int]) -> bool:
        try:
            import sounddevice as sd  # type: ignore
        except Exception as exc:  # noqa: BLE001
            self._open_error = f"sounddevice unavailable ({exc})"
            return False
        try:
            def callback(indata, frames, _time_info, status) -> None:
                if status:
                    log.debug("audio status: %s", status)
                try:
                    pcm = (indata[:, 0] * 32767).astype("<i2").tobytes()
                except Exception:  # noqa: BLE001
                    pcm = bytes(indata)
                try:
                    self._queue.put_nowait(pcm)
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                        self._queue.put_nowait(pcm)
                    except Exception:  # noqa: BLE001
                        pass

            kwargs: Dict[str, Any] = {
                "samplerate": self.sample_rate, "channels": CHANNELS,
                "dtype": "float32", "blocksize": int(self.sample_rate * self.frame_ms / 1000),
                "callback": callback,
            }
            if device_index is not None:
                kwargs["device"] = device_index
            self._stream = sd.InputStream(**kwargs)
            self._stream.start()
            self.backend = "sounddevice"
            self.detail = f"PortAudio @ {self.sample_rate} Hz"
            return True
        except Exception as exc:  # noqa: BLE001
            self._open_error = f"sounddevice could not open the microphone ({exc})"
            self._stream = None
            return False

    def _try_pyaudio(self, device_index: Optional[int]) -> bool:
        try:
            import pyaudio  # type: ignore
        except Exception as exc:  # noqa: BLE001
            self._open_error = (self._open_error or "") + f"; pyaudio unavailable ({exc})"
            return False
        try:
            self._pa = pyaudio.PyAudio()
            kwargs: Dict[str, Any] = {
                "format": pyaudio.paInt16, "channels": CHANNELS,
                "rate": self.sample_rate, "input": True,
                "frames_per_buffer": int(self.sample_rate * self.frame_ms / 1000),
            }
            if device_index is not None:
                kwargs["input_device_index"] = device_index
            self._stream = self._pa.open(**kwargs)
            self.backend = "pyaudio"
            self.detail = f"PortAudio @ {self.sample_rate} Hz"
            return True
        except Exception as exc:  # noqa: BLE001
            self._open_error = f"pyaudio could not open the microphone ({exc})"
            self._stream = None
            self._terminate_pyaudio()
            return False

    def _terminate_pyaudio(self) -> None:
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._pa = None

    # ------------------------------------------------------------------
    def read(self, max_wait_s: float = 1.0) -> bytes:
        """One frame of PCM (possibly empty on timeout)."""
        if self.backend == "pyaudio" and self._stream is not None:
            try:
                return self._stream.read(
                    int(self.sample_rate * self.frame_ms / 1000),
                    exception_on_overflow=False)
            except Exception as exc:  # noqa: BLE001
                log.debug("pyaudio read failed: %s", exc)
                return b""
        try:
            return self._queue.get(timeout=max_wait_s)
        except queue.Empty:
            return b""

    def close(self) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
            if stream is not None:
                try:
                    if self.backend == "pyaudio":
                        stream.stop_stream()
                    stream.close()
                except Exception:  # noqa: BLE001
                    log.debug("audio stream close failed", exc_info=True)
            self._terminate_pyaudio()
            self.detail = "closed"

    def __enter__(self) -> "AudioCapture":
        self.open()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
def _audioop() -> Optional[Any]:
    """``audioop`` was removed from the stdlib in Python 3.13.

    Everything here has a pure-Python fallback, so JARVIS still runs on newer
    interpreters - it just does the level maths itself.
    """
    try:
        import audioop  # type: ignore

        return audioop
    except Exception:  # noqa: BLE001
        return None


def rms_level(pcm: bytes) -> float:
    """Normalised 0..1 loudness of a PCM16 frame."""
    if not pcm:
        return 0.0
    audioop = _audioop()
    if audioop is not None:
        try:
            return float(audioop.rms(pcm, SAMPLE_WIDTH)) / 32768.0
        except Exception:  # noqa: BLE001
            pass
    # Pure-Python fallback (also used on Python 3.13+, where audioop is gone).
    samples = array.array("h")
    try:
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    except Exception:  # noqa: BLE001
        return 0.0
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0


@dataclass
class VoiceActivityDetector:
    """Decide whether a frame contains speech."""

    aggressiveness: int = 2
    frame_ms: int = 30
    sample_rate: int = 16000
    rms_threshold: float = 0.006
    backend: str = "energy"
    _vad: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            import webrtcvad  # type: ignore

            self._vad = webrtcvad.Vad(int(self.aggressiveness))
            self.backend = "webrtcvad"
        except Exception as exc:  # noqa: BLE001
            self._vad = None
            self.backend = "energy"
            log.debug("webrtcvad unavailable (%s); using an energy gate", exc)

    @property
    def name(self) -> str:
        return self.backend

    def is_speech(self, pcm: bytes) -> bool:
        if not pcm:
            return False
        if self._vad is not None:
            try:
                return bool(self._vad.is_speech(pcm, self.sample_rate))
            except Exception:  # noqa: BLE001
                pass
        return rms_level(pcm) >= float(self.rms_threshold)


# ---------------------------------------------------------------------------
@dataclass
class Utterance:
    """A captured span of speech, ready for transcription."""

    pcm: bytes
    sample_rate: int
    started_at: float
    duration_s: float
    peak_level: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"bytes": len(self.pcm), "sample_rate": self.sample_rate,
                "duration_s": round(self.duration_s, 2),
                "peak_level": round(self.peak_level, 4)}


class UtteranceRecorder:
    """Collect frames while speech is present, with hangover and limits.

    This is what makes push-to-talk and wake-word listening behave: it stops
    when the user stops talking instead of recording a fixed window.
    """

    def __init__(self, capture: AudioCapture, vad: VoiceActivityDetector,
                 min_speech_s: float = 0.25, max_speech_s: float = 20.0,
                 hangover_s: float = 0.7, preroll_s: float = 0.35) -> None:
        self.capture = capture
        self.vad = vad
        self.min_speech_s = float(min_speech_s)
        self.max_speech_s = float(max_speech_s)
        self.hangover_s = float(hangover_s)
        self.preroll_s = float(preroll_s)

    def record(self, timeout_s: float = 12.0,
               stop: Optional[threading.Event] = None,
               on_level: Optional[Callable[[float], None]] = None) -> Optional[Utterance]:
        """Record one utterance.  ``None`` means nothing was said in time."""
        frame_s = self.capture.frame_ms / 1000.0
        preroll_frames = max(1, int(self.preroll_s / frame_s))
        preroll: List[bytes] = []
        speech: List[bytes] = []
        peak = 0.0
        started = 0.0
        silence_run = 0.0
        deadline = time.time() + max(0.5, timeout_s)
        hangover_frames = max(1, int(self.hangover_s / frame_s))

        while time.time() < deadline:
            if stop is not None and stop.is_set():
                break
            pcm = self.capture.read(max_wait_s=frame_s * 2)
            if not pcm:
                continue
            level = rms_level(pcm)
            peak = max(peak, level)
            if on_level is not None:
                try:
                    on_level(level)
                except Exception:  # noqa: BLE001
                    pass
            voiced = self.vad.is_speech(pcm)
            if not speech:
                preroll.append(pcm)
                if len(preroll) > preroll_frames:
                    preroll.pop(0)
                if voiced:
                    speech.extend(preroll)
                    started = time.time()
                    silence_run = 0.0
                continue

            speech.append(pcm)
            if voiced:
                silence_run = 0.0
            else:
                silence_run += frame_s
            duration = time.time() - started
            if duration >= self.max_speech_s:
                break
            if silence_run >= self.hangover_s and duration >= self.min_speech_s:
                break
            if silence_run >= hangover_frames * frame_s * 3:
                break

        if not speech:
            return None
        duration = time.time() - (started or time.time())
        if duration < self.min_speech_s:
            return None
        return Utterance(pcm=b"".join(speech), sample_rate=self.capture.sample_rate,
                         started_at=started or time.time(), duration_s=duration,
                         peak_level=peak)


def pcm_to_float32(pcm: bytes) -> Any:
    """PCM16 -> float32 numpy array (what faster-whisper wants)."""
    import numpy as np  # type: ignore

    data = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return data


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not pcm:
        return pcm
    audioop = _audioop()
    if audioop is not None:
        try:
            converted, _state = audioop.ratecv(pcm, SAMPLE_WIDTH, CHANNELS,
                                               src_rate, dst_rate, None)
            return converted
        except Exception as exc:  # noqa: BLE001
            log.debug("audioop resample failed (%s)", exc)
    try:
        # Linear interpolation: good enough for a 16 kHz -> 16 kHz voice path
        # and used only when audioop is unavailable.
        src = array.array("h")
        src.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if not src:
            return pcm
        ratio = float(dst_rate) / float(src_rate)
        out = array.array("h")
        count = int(len(src) * ratio)
        for i in range(count):
            pos = i / ratio
            left = int(pos)
            right = min(left + 1, len(src) - 1)
            frac = pos - left
            out.append(int(src[left] * (1 - frac) + src[right] * frac))
        return out.tobytes()
    except Exception as exc:  # noqa: BLE001
        log.debug("resample failed (%s)", exc)
        return pcm
