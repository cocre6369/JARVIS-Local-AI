"""Desktop platform abstraction.

:func:`create_platform` probes the machine once at start-up, picks the best
available backend for each subsystem and records *why* anything is missing.
That report is shown verbatim on the Automation and Voice settings pages, so a
user is never left guessing whether a feature works on their PC.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .base import (  # noqa: F401
    Capability,
    OcrResult,
    OcrWord,
    Point,
    ProcessInfo,
    Rect,
    Screenshot,
    UIElement,
    WindowInfo,
)
from .simulated import SimulatedDesktop

log = logging.getLogger(__name__)


class Platform:
    """Facade holding one backend per subsystem."""

    def __init__(self,
                 settings_provider: Callable[[], Any],
                 input_backend: Any,
                 screen_backend: Any,
                 window_backend: Any,
                 uia_backend: Any,
                 ocr_backend: Any,
                 app_backend: Any,
                 audio_backend: Any = None,
                 simulated: Optional[SimulatedDesktop] = None,
                 notes: Optional[Dict[str, str]] = None) -> None:
        self.settings_provider = settings_provider
        self.input = input_backend
        self.screen = screen_backend
        self.windows = window_backend
        self.uia = uia_backend
        self.ocr = ocr_backend
        self.apps = app_backend
        #: Optional system-volume backend (Windows Core Audio).  ``None`` means
        #: volume changes fall back to media keys, which cannot be read back.
        self.audio = audio_backend
        self.simulated = simulated
        self.notes: Dict[str, str] = notes or {}

    # ------------------------------------------------------------------
    @property
    def is_simulated(self) -> bool:
        return self.simulated is not None

    def capability_list(self) -> List[Capability]:
        def cap(name: str, backend: Any, ok: bool, detail: str = "") -> Capability:
            return Capability(name=name, available=ok,
                              backend=getattr(backend, "name", "unknown"),
                              detail=detail or self.notes.get(name, ""))

        input_ok = getattr(self.input, "name", "none") != "none"
        screen_ok = getattr(self.screen, "name", "none") != "none"
        win_ok = getattr(self.windows, "name", "none") != "none"
        uia_ok = getattr(self.uia, "name", "none") != "none"
        ocr_ok = getattr(self.ocr, "name", "none") != "none"
        app_ok = getattr(self.apps, "name", "none") != "none"
        audio_ok = self.audio is not None and getattr(self.audio, "name", "none") != "none"
        return [
            cap("input", self.input, input_ok),
            cap("screen_capture", self.screen, screen_ok),
            cap("window_control", self.windows, win_ok),
            cap("ui_elements", self.uia, uia_ok),
            cap("ocr", self.ocr, ocr_ok),
            cap("applications", self.apps, app_ok),
            cap("audio", self.audio, audio_ok),
            Capability(name="simulation", available=self.is_simulated,
                       backend="simulated-desktop" if self.is_simulated else "",
                       detail="Demo Mode is ACTIVE - no real desktop access"
                       if self.is_simulated else "Real desktop backends in use"),
        ]

    def capabilities(self) -> Dict[str, Any]:
        caps = self.capability_list()
        # "simulation" is a mode flag and "audio" is an optional extra (volume
        # falls back to media keys), so neither decides whether the desktop
        # stack as a whole is usable.
        essential = {c for c in caps if c.name not in ("simulation", "audio")}
        return {
            "items": [c.as_dict() for c in caps],
            "all_ok": all(c.available for c in essential),
            "simulated": self.is_simulated,
            "notes": dict(self.notes),
        }

    def summary_text(self) -> str:
        lines = []
        for c in self.capability_list():
            if c.name == "simulation" and not c.available:
                continue
            mark = "OK " if c.available else "-- "
            lines.append(f"{mark} {c.name:<16} {c.backend:<18} {c.detail}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
class _SimScreen:
    name = "simulated"

    def __init__(self, desk: SimulatedDesktop) -> None:
        self.desk = desk

    def capture(self, monitor: int = 0, region: Optional[Rect] = None):
        from .screen_backends import CapturedImage

        w, h, buf = self.desk.render(region)
        return CapturedImage(w, h, bytes(buf), "simulated", self.desk.now(), monitor)

    def monitors(self) -> List[Dict[str, Any]]:
        return self.desk.monitors()

    def size(self) -> Tuple[int, int]:
        return self.desk.size()


class _SimOCR:
    name = "simulated"

    def __init__(self, desk: SimulatedDesktop) -> None:
        self.desk = desk

    def read(self, image_path: str = "", language: str = "en-AU"):
        return self.desk.read_ocr(image_path, language)


class _SimApps:
    name = "simulated"

    def __init__(self, desk: SimulatedDesktop) -> None:
        self.desk = desk

    def installed(self, refresh: bool = False) -> List[Any]:
        from .app_backends import AppCandidate

        out = []
        seen = set()
        for spec in self.desk.apps.values():
            if spec.name in seen:
                continue
            seen.add(spec.name)
            out.append(AppCandidate(name=spec.name, target=spec.executable,
                                    kind="exe", source="simulated",
                                    aliases=list(spec.aliases)))
        return out

    def resolve(self, query: str, limit: int = 5) -> List[Any]:
        from .app_backends import AppCandidate

        spec = self.desk.find_app(query)
        if spec is None:
            scored = [(c.score(query), c) for c in self.installed()]
            scored = [(s, c) for s, c in scored if s >= 0.6]
            scored.sort(key=lambda t: -t[0])
            return [c for _s, c in scored[:limit]]
        return [AppCandidate(name=spec.name, target=spec.executable, kind="exe",
                             source="simulated", aliases=list(spec.aliases))]

    def launch(self, target: str, arguments: Sequence = ()) -> Dict[str, Any]:
        spec = self.desk.find_app(target) or self.desk.find_app(os.path.basename(target))
        if spec is None:
            return {"ok": False, "error": f"No simulated application matches '{target}'"}
        return self.desk.launch_app(spec.name)


class _SimWindows:
    name = "simulated"

    def __init__(self, desk: SimulatedDesktop) -> None:
        self.desk = desk

    def windows(self):
        return self.desk.windows()

    def active(self):
        return self.desk.active()

    def focus(self, win):
        # ``desk.focus`` is the *focused element*, not a method - window
        # activation lives in ``set_active``.  Resolve the handle first.
        handle = getattr(win, "handle", None)
        sim_win, _el = self.desk._find_by_handle(handle)
        if sim_win is None:
            return False
        self.desk.set_active(sim_win)
        return True

    def minimise(self, win):
        return self.desk.minimise(win)

    def maximise(self, win):
        return self.desk.maximise(win)

    def restore(self, win):
        return self.desk.restore(win)

    def close(self, win):
        return self.desk.close(win)

    def move_resize(self, win, rect):
        return self.desk.move_resize(win, rect)

    def processes(self):
        return self.desk.processes()

    def process_running(self, name: str):
        low = (name or "").lower()
        return [p for p in self.desk.processes() if low in p.name.lower()]


class _SimAudio:
    """Demo-Mode system volume: real read/write against the simulated state, so
    verification behaves the same way it does on Windows."""

    name = "simulated"

    def __init__(self, desk: SimulatedDesktop) -> None:
        self.desk = desk

    def get_volume(self) -> Optional[float]:
        return float(self.desk.system_state.get("volume", 42))

    def set_volume(self, percent: float) -> Optional[float]:
        value = max(0.0, min(100.0, float(percent)))
        self.desk.system_state["volume"] = value
        self.desk.system_state["muted"] = value == 0.0
        self.desk.log_action("set_volume", percent=value)
        return value

    def get_mute(self) -> Optional[bool]:
        return bool(self.desk.system_state.get("muted", False))

    def set_mute(self, mute: bool) -> Optional[bool]:
        self.desk.system_state["muted"] = bool(mute)
        self.desk.log_action("set_mute", muted=bool(mute))
        return bool(mute)

    def step(self, up: bool = True, steps: int = 2) -> Optional[float]:
        current = self.get_volume() or 0.0
        return self.set_volume(current + (2.0 * steps if up else -2.0 * steps))

    def close(self) -> None:
        return None


def create_simulated_platform(settings_provider: Callable[[], Any],
                              realtime: bool = True,
                              seed: int = 7) -> Platform:
    desk = SimulatedDesktop(realtime=realtime, seed=seed)
    return Platform(
        settings_provider=settings_provider,
        input_backend=desk,
        screen_backend=_SimScreen(desk),
        window_backend=_SimWindows(desk),
        uia_backend=desk,
        ocr_backend=_SimOCR(desk),
        app_backend=_SimApps(desk),
        audio_backend=_SimAudio(desk),
        simulated=desk,
        notes={"input": "Demo Mode: actions are applied to a simulated desktop",
               "screen_capture": "Demo Mode: rendered in-process",
               "window_control": "Demo Mode",
               "ui_elements": "Demo Mode: full control tree",
               "ocr": "Demo Mode: reads the simulated UI text",
               "applications": "Demo Mode: 10 simulated apps + generic factory",
               "audio": "Demo Mode: system volume is simulated"},
    )


def create_platform(settings_provider: Callable[[], Any],
                    force_simulated: Optional[bool] = None,
                    realtime: bool = True) -> Platform:
    """Build the real platform, falling back to Demo Mode only when the user
    asked for it or when nothing at all is available."""
    if force_simulated is None:
        force_simulated = os.environ.get("JARVIS_DEMO", "").lower() in ("1", "true", "yes")
    if force_simulated:
        log.warning("JARVIS is running in Demo Mode (simulated desktop).")
        return create_simulated_platform(settings_provider, realtime=realtime)

    notes: Dict[str, str] = {}
    from . import app_backends, input_backends, ocr_backends, screen_backends
    from . import uia_backend as uia_mod
    from . import window_backends

    try:
        s = settings_provider()
        input_pref = getattr(getattr(s, "automation", None), "input_backend", "auto")
        screen_pref = getattr(getattr(s, "vision", None), "capture_backend", "auto")
        ocr_pref = getattr(getattr(s, "vision", None), "ocr_backend", "auto")
        ocr_lang = getattr(getattr(s, "vision", None), "ocr_language", "en-AU")
        use_uia = bool(getattr(getattr(s, "vision", None), "use_uia", True))
    except Exception:  # noqa: BLE001
        input_pref = screen_pref = ocr_pref = "auto"
        ocr_lang, use_uia = "en-AU", True

    inp, inp_note = input_backends.create_input_backend(input_pref)
    scr, scr_note = screen_backends.create_screen_backend(screen_pref)
    win, win_note = window_backends.create_window_backend("auto")
    ocr, ocr_note = ocr_backends.create_ocr_backend(ocr_pref, ocr_lang)
    apps, apps_note = app_backends.create_app_backend("auto")

    if use_uia:
        uia, uia_note = uia_mod.create_uia_backend(
            "auto", ocr_backend=ocr, screen_backend=scr, input_backend=inp,
            settings_provider=settings_provider)
    else:
        uia, uia_note = uia_mod.NullUIA("UI Automation disabled in settings"), \
            "disabled by user"

    try:
        from .audio import create_audio_backend

        audio, audio_note = create_audio_backend()
    except Exception as exc:  # noqa: BLE001 - volume is an optional extra
        audio, audio_note = None, f"audio backend failed to load ({exc})"

    notes.update({"input": inp_note, "screen_capture": scr_note,
                  "window_control": win_note, "ui_elements": uia_note,
                  "ocr": ocr_note, "applications": apps_note, "audio": audio_note})

    platform = Platform(settings_provider, inp, scr, win, uia, ocr, apps,
                        audio_backend=audio, simulated=None, notes=notes)

    # If the three subsystems that computer control actually depends on are all
    # missing (headless CI container, no display server) fall back to the
    # simulated desktop so JARVIS still starts and stays usable - with a clear
    # DEMO MODE banner in the UI rather than silently pretending.
    core = {"input", "screen_capture", "window_control"}
    core_ok = sum(1 for c in platform.capability_list()
                  if c.name in core and c.available)
    if core_ok == 0:
        log.warning("No desktop backend is available on this machine "
                    "(%s). Falling back to Demo Mode so JARVIS remains usable.",
                    "; ".join(f"{k}={v}" for k, v in notes.items()))
        sim = create_simulated_platform(settings_provider, realtime=realtime)
        sim.notes["fallback_reason"] = "; ".join(f"{k}: {v}" for k, v in notes.items())
        return sim
    return platform


__all__ = [
    "Platform", "Capability", "OcrResult", "OcrWord", "Point", "ProcessInfo",
    "Rect", "Screenshot", "UIElement", "WindowInfo", "SimulatedDesktop",
    "create_platform", "create_simulated_platform",
]
