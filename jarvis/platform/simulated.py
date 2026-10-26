"""An in-process simulated desktop.

Why this exists
---------------
JARVIS ships for Windows, but its *brain* - the plan / act / observe / verify
loop, the permission engine, the observation cache, the loop guards - is
platform independent.  This module provides a fully stateful fake desktop so
that:

* the automated test-suite can drive a complete multi-step task
  ("open Spotify, search, play, verify") end to end, and
* the application can be launched on any OS in a clearly-labelled **Demo
  Mode** (the status bar shows a SIMULATION badge) for evaluation.

It is *not* used on Windows unless the user explicitly enables Demo Mode or
the real backends are unavailable.  Nothing here pretends to be a real screen:
it produces genuine PNG screenshots of its own state so the OCR and vision code
paths are exercised against real pixel data.

The simulated apps include a deliberately *generic* application factory, which
is what lets the tests prove that JARVIS generalises to software it has never
been programmed for.
"""

from __future__ import annotations

import difflib
import logging
import math
import os
import random
import re
import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .base import (
    OcrResult,
    OcrWord,
    Point,
    ProcessInfo,
    Rect,
    Screenshot,
    UIElement,
    WindowInfo,
)

log = logging.getLogger(__name__)

SCREEN_W, SCREEN_H = 1920, 1080


# ---------------------------------------------------------------------------
# elements / windows / apps
# ---------------------------------------------------------------------------
@dataclass
class SimElement:
    name: str
    control_type: str = "Button"
    rect: Rect = Rect(0, 0, 0, 0)
    automation_id: str = ""
    value: str = ""
    patterns: List[str] = field(default_factory=lambda: ["invoke"])
    is_password: bool = False
    is_enabled: bool = True
    on_invoke: Optional[Callable[["SimWindow", "SimElement"], str]] = None
    on_set_value: Optional[Callable[["SimWindow", "SimElement", str], str]] = None
    #: set when the element only appears after some state change
    visible_if: Optional[Callable[["SimWindow"], bool]] = None

    def visible(self, win: "SimWindow") -> bool:
        if not self.is_enabled:
            return True
        if self.visible_if is None:
            return True
        try:
            return bool(self.visible_if(win))
        except Exception:  # noqa: BLE001
            return False

    def to_ui(self) -> UIElement:
        return UIElement(
            name=self.name, control_type=self.control_type, rect=self.rect,
            automation_id=self.automation_id, class_name="SimControl",
            value=self.value, is_enabled=self.is_enabled, is_visible=True,
            is_focused=False, is_password=self.is_password, source="simulated",
            handle=id(self), patterns=list(self.patterns),
        )


@dataclass
class SimWindow:
    title: str
    app_name: str
    process_name: str
    rect: Rect = Rect(160, 100, 1600, 900)
    elements: List[SimElement] = field(default_factory=list)
    state: Dict[str, Any] = field(default_factory=dict)
    minimised: bool = False
    maximised: bool = False
    handle: int = 0
    pid: int = 0
    opened_at: float = field(default_factory=time.time)
    #: seconds the app needs before its UI is usable (loading screens)
    ready_after: float = 0.0

    @property
    def ready(self) -> bool:
        return (time.time() - self.opened_at) >= self.ready_after

    def element(self, name: str) -> Optional[SimElement]:
        low = name.lower()
        for el in self.elements:
            if el.name.lower() == low:
                return el
        for el in self.elements:
            if low in el.name.lower():
                return el
        return None

    def visible_elements(self) -> List[SimElement]:
        return [e for e in self.elements if e.visible(self)]

    def info(self, active: bool = False) -> WindowInfo:
        return WindowInfo(
            title=self.title, handle=self.handle, process_name=self.process_name,
            pid=self.pid, rect=self.rect, is_active=active,
            is_minimised=self.minimised, is_maximised=self.maximised,
            class_name="SimWindow",
        )

    def set_title(self, title: str) -> None:
        self.title = title


@dataclass
class SimAppSpec:
    name: str
    process_name: str
    executable: str
    ready_after: float = 0.6
    builder: Optional[Callable[["SimulatedDesktop"], SimWindow]] = None
    aliases: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# The desktop
# ---------------------------------------------------------------------------
class SimulatedDesktop:
    """Holds every piece of fake state and implements the backend protocols."""

    name = "simulated"

    def __init__(self, width: int = SCREEN_W, height: int = SCREEN_H,
                 seed: int = 7, realtime: bool = False) -> None:
        self.width, self.height = width, height
        self.realtime = realtime
        self._rng = random.Random(seed)
        self._windows: List[SimWindow] = []
        self._active: Optional[SimWindow] = None
        self._handle_seq = 1000
        self._pid_seq = 4000
        self.mouse = Point(width // 2, height // 2)
        self.clipboard = ""
        self.focus: Optional[SimElement] = None
        self.focus_window: Optional[SimWindow] = None
        self.action_log: List[Dict[str, Any]] = []
        self.system_state: Dict[str, Any] = {
            "dark_mode": False, "volume": 42, "muted": False,
            "bluetooth": True, "wifi": True, "battery_percent": 87,
            "plugged_in": True, "night_light": False,
        }
        self.apps: Dict[str, SimAppSpec] = {}
        self._register_builtin_apps()
        self._clock = time.time()

    # ------------------------------------------------------------------
    # time
    # ------------------------------------------------------------------
    def now(self) -> float:
        if self.realtime:
            return time.time()
        return self._clock

    def advance(self, seconds: float) -> None:
        if not self.realtime:
            self._clock += seconds

    def _sleep(self, seconds: float) -> None:
        if self.realtime:
            time.sleep(min(seconds, 0.5))
        else:
            self.advance(seconds)

    # ------------------------------------------------------------------
    # logging helpers
    # ------------------------------------------------------------------
    def log_action(self, kind: str, **kw: Any) -> None:
        entry = {"t": round(self.now(), 3), "kind": kind}
        entry.update(kw)
        self.action_log.append(entry)
        if len(self.action_log) > 4000:
            del self.action_log[:-4000]

    # ------------------------------------------------------------------
    # app registry
    # ------------------------------------------------------------------
    def register_app(self, spec: SimAppSpec) -> None:
        self.apps[spec.name.lower()] = spec
        for a in spec.aliases:
            self.apps.setdefault(a.lower(), spec)

    def _register_builtin_apps(self) -> None:
        self.register_app(SimAppSpec(
            "Spotify", "Spotify.exe", r"C:\Users\user\AppData\Roaming\Spotify\Spotify.exe",
            ready_after=1.4, builder=_build_spotify,
            aliases=("spotify music", "music")))
        self.register_app(SimAppSpec(
            "Chrome", "chrome.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            ready_after=1.1, builder=_build_browser,
            aliases=("google chrome", "chrome browser", "browser")))
        self.register_app(SimAppSpec(
            "Edge", "msedge.exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            ready_after=1.0, builder=_build_browser,
            aliases=("microsoft edge", "edge browser")))
        self.register_app(SimAppSpec(
            "File Explorer", "explorer.exe", r"C:\Windows\explorer.exe",
            ready_after=0.5, builder=_build_explorer,
            aliases=("explorer", "windows explorer", "files", "my computer")))
        self.register_app(SimAppSpec(
            "Notepad", "notepad.exe", r"C:\Windows\System32\notepad.exe",
            ready_after=0.3, builder=_build_notepad, aliases=("text editor",)))
        self.register_app(SimAppSpec(
            "Settings", "SystemSettings.exe", r"C:\Windows\System32\SystemSettings.exe",
            ready_after=0.7, builder=_build_settings,
            aliases=("windows settings", "preferences")))
        self.register_app(SimAppSpec(
            "Discord", "Discord.exe", r"C:\Users\user\AppData\Local\Discord\Discord.exe",
            ready_after=1.6, builder=_build_discord, aliases=("discord chat",)))
        self.register_app(SimAppSpec(
            "After Effects", "AfterFX.exe",
            r"C:\Program Files\Adobe\Adobe After Effects 2024\Support Files\AfterFX.exe",
            ready_after=2.2, builder=None, aliases=("adobe after effects", "ae")))
        self.register_app(SimAppSpec(
            "Blender", "blender.exe", r"C:\Program Files\Blender Foundation\Blender\blender.exe",
            ready_after=1.5, builder=None, aliases=("blender 3d",)))
        self.register_app(SimAppSpec(
            "Minecraft", "Minecraft.Windows.exe",
            r"C:\Program Files\WindowsApps\Minecraft\Minecraft.Windows.exe",
            ready_after=2.0, builder=None, aliases=("minecraft launcher",)))

    # ------------------------------------------------------------------
    # launching
    # ------------------------------------------------------------------
    def find_app(self, query: str) -> Optional[SimAppSpec]:
        if not query:
            return None
        q = query.strip().lower()
        if q in self.apps:
            return self.apps[q]
        best = None
        best_score = 0.0
        for key, spec in self.apps.items():
            if key == q:
                return spec
            score = 0.0
            if q in key or key in q:
                score = 0.85 + 0.1 * (len(key) / max(1, len(q)))
            elif _token_overlap(q, spec.name.lower()):
                score = 0.6
            if score > best_score:
                best, best_score = spec, score
        return best if best_score >= 0.55 else None

    def launch_app(self, query: str) -> Dict[str, Any]:
        spec = self.find_app(query)
        if spec is None:
            # Unknown application -> generic window.  This is the path that
            # proves JARVIS can operate software it was never told about.
            return {"ok": False, "error": f"No application matching '{query}'"}
        existing = self.window_for_app(spec.name)
        if existing is not None:
            existing.minimised = False
            self._active = existing
            self.log_action("launch", app=spec.name, result="already-running")
            return {"ok": True, "app": spec.name, "already_running": True,
                    "pid": existing.pid, "title": existing.title}
        win = SimWindow(
            title=spec.name, app_name=spec.name, process_name=spec.process_name,
            rect=Rect(140, 90, self.width - 280, self.height - 200),
            handle=self._next_handle(), pid=self._next_pid(),
            opened_at=self.now(), ready_after=spec.ready_after,
            state={"app": spec.name},
        )
        if spec.builder is not None:
            built = spec.builder(self)
            win.elements = built.elements
            win.state.update(built.state)
            if built.title:
                win.title = built.title
        else:
            win.elements = _build_generic(spec.name).elements
        self._windows.append(win)
        self._active = win
        self.focus = None
        self.focus_window = win
        self.log_action("launch", app=spec.name, result="started", pid=win.pid)
        return {"ok": True, "app": spec.name, "already_running": False,
                "pid": win.pid, "title": win.title,
                "ready_after_s": spec.ready_after}

    def window_for_app(self, app_name: str) -> Optional[SimWindow]:
        low = app_name.lower()
        for w in self._windows:
            if w.app_name.lower() == low:
                return w
        return None

    def close_app(self, app_name: str) -> Dict[str, Any]:
        win = self.window_for_app(app_name)
        if win is None:
            return {"ok": False, "error": f"{app_name} is not running"}
        self._windows.remove(win)
        if self._active is win:
            self._active = self._windows[-1] if self._windows else None
        self.log_action("close", app=app_name)
        return {"ok": True, "app": app_name}

    # ------------------------------------------------------------------
    # window plumbing
    # ------------------------------------------------------------------
    def _next_handle(self) -> int:
        self._handle_seq += 1
        return self._handle_seq

    def _next_pid(self) -> int:
        self._pid_seq += 7
        return self._pid_seq

    @property
    def active_window(self) -> Optional[SimWindow]:
        return self._active

    def set_active(self, win: Optional[SimWindow]) -> None:
        self._active = win
        self.focus_window = win

    def element_at(self, x: int, y: int) -> Optional[UIElement]:
        """UIABackend protocol: the control under a point (or None)."""
        _win, el = self.element_at_xy(int(x), int(y))
        if el is None:
            return None
        ui = el.to_ui()
        ui.is_focused = (self.focus is el)
        return ui

    def element_at_xy(self, x: int, y: int) -> Tuple[Optional[SimWindow], Optional[SimElement]]:
        """Internal hit-test: the (window, element) under a point."""
        p = Point(int(x), int(y))
        for win in reversed(self._windows):
            if win.minimised or not win.rect.contains(p):
                continue
            for el in reversed(win.visible_elements()):
                if el.rect.contains(p):
                    return win, el
            return win, None
        return None, None

    # ------------------------------------------------------------------
    # InputBackend
    # ------------------------------------------------------------------
    def move(self, x: int, y: int, duration: float = 0.0) -> bool:
        self.mouse = Point(int(x), int(y))
        self._sleep(min(duration, 0.3))
        self.log_action("move", x=int(x), y=int(y))
        return True

    def click(self, x: Optional[int] = None, y: Optional[int] = None,
              button: str = "left", clicks: int = 1) -> bool:
        tx = self.mouse.x if x is None else int(x)
        ty = self.mouse.y if y is None else int(y)
        self.mouse = Point(tx, ty)
        win, el = self.element_at_xy(tx, ty)
        self.log_action("click", x=tx, y=ty, button=button, clicks=clicks,
                        element=el.name if el else None,
                        window=win.title if win else None)
        if win is None:
            return True                      # clicked the desktop
        self.set_active(win)
        if el is None:
            self.focus = None
            return True
        self.focus = el
        self.focus_window = win
        if el.is_password:
            # JARVIS never reads a password field; clicking is still allowed
            # (a human may have asked it to focus the field) but the value is
            # never returned to the model.
            return True
        if button == "left" and el.on_invoke is not None:
            el.on_invoke(win, el)
            return True
        if button == "right":
            win.state.setdefault("context_menu", el.name)
            return True
        return True

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3,
             button: str = "left") -> bool:
        self.mouse = Point(int(x2), int(y2))
        self._sleep(min(duration, 0.3))
        self.log_action("drag", x1=x1, y1=y1, x2=x2, y2=y2, button=button)
        return True

    def scroll(self, dx: int = 0, dy: int = -3,
               x: Optional[int] = None, y: Optional[int] = None) -> bool:
        win, el = self.element_at_xy(self.mouse.x, self.mouse.y)
        self.log_action("scroll", dx=dx, dy=dy,
                        window=win.title if win else None)
        if win is not None:
            win.state["scroll"] = int(win.state.get("scroll", 0)) + int(dy)
            if el is not None and el.control_type == "List":
                el.value = f"scrolled:{win.state['scroll']}"
        return True

    def position(self) -> Point:
        return self.mouse

    def type_text(self, text: str, interval: float = 0.01) -> bool:
        target = self.focus
        win = self.focus_window or self._active
        self.log_action("type", chars=len(text),
                        element=target.name if target else None,
                        window=win.title if win else None,
                        text="[REDACTED]" if (target and target.is_password) else text[:120])
        if target is not None and target.on_set_value is not None and win is not None:
            target.on_set_value(win, target, text)
            target.value = text
            return True
        if target is not None and win is not None:
            target.value = (target.value or "") + text
            return True
        if win is not None:
            doc = win.element("Document") or win.element("Text Area")
            if doc is not None:
                doc.value = (doc.value or "") + text
                return True
        return False

    def press(self, keys: Sequence[str]) -> bool:
        combo = "+".join(str(k).lower() for k in keys)
        win = self.focus_window or self._active
        self.log_action("keys", combo=combo, window=win.title if win else None)
        target = self.focus
        # Enter in a focused search/edit field commits it.
        if combo in ("enter", "return") and target is not None and win is not None:
            if target.on_invoke is not None:
                target.on_invoke(win, target)
            elif "search" in target.name.lower():
                win.state["query"] = target.value
                _refresh_search_results(win, target.value)
            return True
        if combo in ("ctrl+a",) and target is not None:
            target.state_select_all = True  # type: ignore[attr-defined]
            return True
        if combo in ("ctrl+c",) and target is not None:
            self.clipboard = "" if target.is_password else (target.value or "")
            return True
        if combo in ("ctrl+v",) and target is not None and win is not None:
            target.value = (target.value or "") + self.clipboard
            return True
        if combo in ("alt+tab",):
            if self._windows:
                idx = self._windows.index(self._active) if self._active in self._windows else 0
                self._active = self._windows[(idx + 1) % len(self._windows)]
                self.focus_window = self._active
            return True
        if combo in ("escape", "esc"):
            win.state.pop("context_menu", None) if win else None
            return True
        if combo in ("f11",) and win is not None:
            win.state["fullscreen"] = not bool(win.state.get("fullscreen"))
            win.maximised = bool(win.state.get("fullscreen"))
            return True
        if combo in ("space",) and win is not None:
            if "playing" in win.state:
                win.state["playing"] = not win.state["playing"]
            return True
        return True

    def key_down(self, key: str) -> bool:
        self.log_action("key_down", key=key)
        return True

    def key_up(self, key: str) -> bool:
        self.log_action("key_up", key=key)
        return True

    # ------------------------------------------------------------------
    # WindowBackend
    # ------------------------------------------------------------------
    def windows(self) -> List[WindowInfo]:
        out = []
        for w in self._windows:
            out.append(w.info(active=(w is self._active)))
        return out

    def active(self) -> Optional[WindowInfo]:
        return self._active.info(active=True) if self._active else None

    def _resolve(self, win: WindowInfo) -> Optional[SimWindow]:
        for w in self._windows:
            if w.handle == win.handle or (win.title and w.title == win.title):
                return w
        return None

    def focus(self, win: WindowInfo) -> bool:
        w = self._resolve(win)
        if not w:
            return False
        w.minimised = False
        self.set_active(w)
        self.log_action("focus_window", title=w.title)
        return True

    def minimise(self, win: WindowInfo) -> bool:
        w = self._resolve(win)
        if not w:
            return False
        w.minimised = True
        if self._active is w:
            self._active = None
        self.log_action("minimise", title=w.title)
        return True

    def maximise(self, win: WindowInfo) -> bool:
        w = self._resolve(win)
        if not w:
            return False
        w.maximised = True
        w.rect = Rect(0, 0, self.width, self.height)
        self._relayout(w)
        self.log_action("maximise", title=w.title)
        return True

    def restore(self, win: WindowInfo) -> bool:
        w = self._resolve(win)
        if not w:
            return False
        w.maximised = False
        w.minimised = False
        w.rect = Rect(140, 90, self.width - 280, self.height - 200)
        self._relayout(w)
        self.set_active(w)
        self.log_action("restore", title=w.title)
        return True

    def close(self, win: WindowInfo) -> bool:
        w = self._resolve(win)
        if not w:
            return False
        self._windows.remove(w)
        if self._active is w:
            self._active = self._windows[-1] if self._windows else None
        self.log_action("close_window", title=w.title)
        return True

    def move_resize(self, win: WindowInfo, rect: Rect) -> bool:
        w = self._resolve(win)
        if not w:
            return False
        w.rect = rect
        self._relayout(w)
        return True

    def _relayout(self, win: SimWindow) -> None:
        """Scale element rectangles to the new window size (crude but enough
        for the observation pipeline to see a plausible layout)."""
        base = win.state.get("layout_rect") or Rect(140, 90, self.width - 280,
                                                    self.height - 200)
        if base.width <= 0 or base.height <= 0:
            return
        sx = win.rect.width / base.width
        sy = win.rect.height / base.height
        for el in win.elements:
            el.rect = Rect(
                win.rect.x + int((el.rect.x - base.x) * sx),
                win.rect.y + int((el.rect.y - base.y) * sy),
                max(8, int(el.rect.width * sx)),
                max(8, int(el.rect.height * sy)),
            )
        win.state["layout_rect"] = win.rect

    def processes(self) -> List[ProcessInfo]:
        out = [
            ProcessInfo(pid=4, name="System", executable="System", memory_mb=180.0),
            ProcessInfo(pid=892, name="svchost.exe", executable=r"C:\Windows\System32\svchost.exe",
                        memory_mb=420.0),
            ProcessInfo(pid=2140, name="explorer.exe",
                        executable=r"C:\Windows\explorer.exe", memory_mb=310.0),
            ProcessInfo(pid=3388, name="ollama app.exe",
                        executable=r"C:\Program Files\Ollama\ollama app.exe",
                        memory_mb=95.0),
        ]
        for w in self._windows:
            out.append(ProcessInfo(pid=w.pid, name=w.process_name,
                                   executable=_exe_for(w.process_name),
                                   memory_mb=float(self._rng.randint(120, 900)),
                                   windows=[w.title]))
        return out

    # ------------------------------------------------------------------
    # UIABackend
    # ------------------------------------------------------------------
    def elements(self, root: Optional[WindowInfo] = None,
                 max_count: int = 150) -> List[UIElement]:
        target: Optional[SimWindow] = None
        if root is not None:
            target = self._resolve(root)
        else:
            target = self._active
        if target is None:
            return []
        out: List[UIElement] = []
        # window chrome first
        out.append(UIElement(name=target.title, control_type="Window",
                             rect=target.rect, class_name="SimWindow",
                             source="simulated", handle=target.handle,
                             children_count=len(target.elements)))
        for el in target.visible_elements():
            ui = el.to_ui()
            ui.is_focused = (self.focus is el)
            out.append(ui)
            if len(out) >= max_count:
                break
        return out

    def invoke(self, el: UIElement) -> bool:
        win, sim = self._find_by_handle(el.handle)
        if win is None or sim is None:
            return False
        self.set_active(win)
        self.focus = sim
        self.focus_window = win
        if sim.is_password:
            return True
        if sim.on_invoke is not None:
            sim.on_invoke(win, sim)
        self.log_action("invoke", element=sim.name, window=win.title)
        return True

    def set_value(self, el: UIElement, text: str) -> bool:
        win, sim = self._find_by_handle(el.handle)
        if win is None or sim is None:
            return False
        if sim.is_password:
            return False        # refuse, by design
        if sim.on_set_value is not None:
            sim.on_set_value(win, sim, text)
        sim.value = text
        self.focus = sim
        self.focus_window = win
        self.log_action("set_value", element=sim.name, window=win.title, chars=len(text))
        return True

    def focused(self) -> Optional[UIElement]:
        if self.focus is None:
            return None
        ui = self.focus.to_ui()
        ui.is_focused = True
        return ui

    def _find_by_handle(self, handle: Any) -> Tuple[Optional[SimWindow], Optional[SimElement]]:
        if handle is None:
            return None, None
        try:
            hid = int(handle)
        except (TypeError, ValueError):
            return None, None
        for w in self._windows:
            for el in w.elements:
                if id(el) == hid:
                    return w, el
        return None, None

    # ------------------------------------------------------------------
    # ScreenBackend - renders real PNG pixels of the simulated desktop
    # ------------------------------------------------------------------
    def size(self) -> Tuple[int, int]:
        return (self.width, self.height)

    def monitors(self) -> List[Dict[str, Any]]:
        return [{"index": 0, "name": "SIM-DISPLAY-1", "width": self.width,
                 "height": self.height, "primary": True, "x": 0, "y": 0}]

    def capture(self, monitor: int = 0, region: Optional[Rect] = None) -> Optional[Any]:
        return self.render(region)

    def _include_cursor(self) -> bool:
        """Whether the rendered frame should show the pointer.

        The simulated desktop draws its own cursor (a real capture never contains
        one), so it has to respect ``vision.include_cursor`` too - otherwise the
        setting would only affect half the code paths.
        """
        try:
            from ..config import load as load_settings

            return bool(load_settings().vision.include_cursor)
        except Exception:  # noqa: BLE001
            return True

    def render(self, region: Optional[Rect] = None) -> Tuple[int, int, bytearray]:
        w, h = self.width, self.height
        dark = bool(self.system_state.get("dark_mode"))
        buf = bytearray()
        bg = (18, 20, 26) if dark else (226, 230, 238)
        row_bg = bytes(bg) * w
        for _ in range(h):
            buf += row_bg
        for win in self._windows:
            if win.minimised:
                continue
            self._paint_window(buf, w, h, win, dark)
        # taskbar
        self._fill_rect(buf, w, h, Rect(0, h - 48, w, 48), (12, 14, 18))
        # cursor (honours vision.include_cursor, exactly like a real capture)
        if self._include_cursor():
            self._fill_rect(buf, w, h, Rect(self.mouse.x - 3, self.mouse.y - 3, 7, 7),
                            (255, 255, 255))
        if region is not None:
            buf = self._crop(buf, w, h, region)
            w, h = region.width, region.height
        return w, h, buf

    def _paint_window(self, buf: bytearray, W: int, H: int, win: SimWindow,
                      dark: bool) -> None:
        r = win.rect
        body = (32, 35, 42) if dark else (250, 251, 253)
        chrome = (22, 24, 30) if dark else (236, 238, 243)
        self._fill_rect(buf, W, H, r, body)
        self._fill_rect(buf, W, H, Rect(r.x, r.y, r.width, 36), chrome)
        accent = (34, 211, 238) if win is self._active else (90, 96, 110)
        self._fill_rect(buf, W, H, Rect(r.x, r.y, r.width, 3), accent)
        for el in win.visible_elements():
            self._paint_element(buf, W, H, el, win, dark)

    def _paint_element(self, buf: bytearray, W: int, H: int, el: SimElement,
                       win: SimWindow, dark: bool) -> None:
        ct = el.control_type
        if ct == "Button":
            self._fill_rect(buf, W, H, el.rect, (46, 52, 64) if dark else (222, 226, 234))
        elif ct in ("Edit", "Search"):
            colour = (60, 20, 24) if el.is_password else ((20, 22, 28) if dark else (255, 255, 255))
            self._fill_rect(buf, W, H, el.rect, colour)
        elif ct == "List":
            self._fill_rect(buf, W, H, el.rect, (26, 28, 34) if dark else (244, 246, 250))
        elif ct == "Image":
            self._fill_rect(buf, W, H, el.rect, (54, 46, 78))
        if self.focus is el:
            self._outline(buf, W, H, el.rect, (34, 211, 238))

    def _outline(self, buf: bytearray, W: int, H: int, r: Rect, colour) -> None:
        self._fill_rect(buf, W, H, Rect(r.x, r.y, r.width, 2), colour)
        self._fill_rect(buf, W, H, Rect(r.x, r.bottom - 2, r.width, 2), colour)
        self._fill_rect(buf, W, H, Rect(r.x, r.y, 2, r.height), colour)
        self._fill_rect(buf, W, H, Rect(r.right - 2, r.y, 2, r.height), colour)

    @staticmethod
    def _fill_rect(buf: bytearray, W: int, H: int, r: Rect, colour) -> None:
        x0 = max(0, r.x); y0 = max(0, r.y)
        x1 = min(W, r.right); y1 = min(H, r.bottom)
        if x1 <= x0 or y1 <= y0:
            return
        rgb = bytes(colour[:3])
        row = rgb * (x1 - x0)
        for y in range(y0, y1):
            start = (y * W + x0) * 3
            buf[start:start + len(row)] = row

    @staticmethod
    def _crop(buf: bytearray, W: int, H: int, r: Rect) -> bytearray:
        x0 = max(0, r.x); y0 = max(0, r.y)
        x1 = min(W, r.right); y1 = min(H, r.bottom)
        out = bytearray()
        span = (x1 - x0) * 3
        for y in range(y0, y1):
            start = (y * W + x0) * 3
            out += buf[start:start + span]
        return out

    def screenshot(self, out_path: str, monitor: int = 0,
                   region: Optional[Rect] = None) -> Screenshot:
        w, h, buf = self.render(region)
        write_png(out_path, w, h, buf)
        shot = Screenshot(path=out_path, width=w, height=h, ts=self.now(),
                          monitor=monitor,
                          window_label=self._active.title if self._active else "",
                          simulated=True)
        shot.perceptual_hash = _phash(buf, w, h)
        self.log_action("screenshot", path=out_path, w=w, h=h)
        return shot

    # ------------------------------------------------------------------
    # OCRBackend - reads the simulated UI text directly
    # ------------------------------------------------------------------
    def read_ocr(self, image_path: str = "", language: str = "en-AU") -> OcrResult:
        t0 = time.time()
        words: List[OcrWord] = []
        lines: List[str] = []
        for win in self._windows:
            if win.minimised:
                continue
            lines.append(win.title)
            for el in win.visible_elements():
                if el.is_password:
                    continue           # never OCR a password field
                text = el.value or el.name
                if not text:
                    continue
                lines.append(text)
                words.append(OcrWord(text=text[:60], rect=el.rect, confidence=0.97))
        self._sleep(0.05)
        return OcrResult(words=words, lines=lines, text="\n".join(lines),
                         engine="simulated", duration_ms=(time.time() - t0) * 1000,
                         image_size=(self.width, self.height))


# ---------------------------------------------------------------------------
# tiny PNG writer (stdlib only) so the simulator never needs Pillow
# ---------------------------------------------------------------------------
def write_png(path: str, width: int, height: int, rgb: bytearray) -> str:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        fh.write(chunk(b"IEND", b""))
    return path


def _phash(buf: bytearray, w: int, h: int) -> str:
    """Cheap 8x8 average-hash of the framebuffer (change detection)."""
    gw, gh = 8, 8
    cells = [0.0] * (gw * gh)
    counts = [0] * (gw * gh)
    step_x = max(1, w // gw)
    step_y = max(1, h // gh)
    for gy in range(gh):
        for gx in range(gw):
            idx = gy * gw + gx
            total = 0
            n = 0
            for y in range(gy * step_y, min(h, (gy + 1) * step_y), max(1, step_y // 4)):
                base = (y * w + gx * step_x) * 3
                for x in range(0, min(step_x, w - gx * step_x), max(1, step_x // 4)):
                    o = base + x * 3
                    if o + 2 < len(buf):
                        total += buf[o] + buf[o + 1] + buf[o + 2]
                        n += 1
            cells[idx] = (total / (n * 3)) if n else 0.0
            counts[idx] = n
    avg = sum(cells) / max(1, len(cells))
    bits = "".join("1" if c > avg else "0" for c in cells)
    return hex(int(bits, 2))[2:].zfill(16)


def _token_overlap(a: str, b: str) -> bool:
    ta = {t for t in re.split(r"[\s\-_.]+", a.lower()) if t}
    tb = {t for t in re.split(r"[\s\-_.]+", b.lower()) if t}
    return bool(ta & tb)


def _exe_for(process_name: str) -> str:
    return rf"C:\Program Files\{process_name}"


#: A tiny stand-in catalogue.  A real Spotify resolves "Violent Crimes Kanye
#: West" to the track *Violent Crimes* by *Kanye West*; the simulated desktop
#: has to do the same kind of ranking for the flow to be a fair test of JARVIS
#: rather than a test of exact string echoes.
_SIM_CATALOGUE: Tuple[Dict[str, str], ...] = (
    {"title": "Violent Crimes", "artist": "Kanye West", "album": "ye"},
    {"title": "Runaway", "artist": "Kanye West", "album": "My Beautiful Dark Twisted Fantasy"},
    {"title": "Blinding Lights", "artist": "The Weeknd", "album": "After Hours"},
    {"title": "Bohemian Rhapsody", "artist": "Queen", "album": "A Night at the Opera"},
    {"title": "Redbone", "artist": "Childish Gambino", "album": "Awaken, My Love!"},
    {"title": "Do I Wanna Know?", "artist": "Arctic Monkeys", "album": "AM"},
    {"title": "Starboy", "artist": "The Weeknd", "album": "Starboy"},
    {"title": "Sunflower", "artist": "Post Malone", "album": "Spider-Man: Into the Spider-Verse"},
)


def _catalogue_match(query: str) -> Optional[Dict[str, str]]:
    """Best catalogue entry for a free-text music query (title and/or artist)."""
    q = (query or "").strip().lower()
    if not q:
        return None
    q_tokens = {t for t in re.split(r"[^a-z0-9]+", q) if t}
    if not q_tokens:
        return None
    best: Optional[Dict[str, str]] = None
    best_score = 0.0
    for entry in _SIM_CATALOGUE:
        t_tokens = {t for t in re.split(r"[^a-z0-9]+", entry["title"].lower()) if t}
        a_tokens = {t for t in re.split(r"[^a-z0-9]+", entry["artist"].lower()) if t}
        known = t_tokens | a_tokens
        if not (t_tokens & q_tokens):
            continue                      # title must be mentioned to match
        coverage = len(known & q_tokens) / max(1, len(known))
        artist_hit = 0.25 if (a_tokens & q_tokens) else 0.0
        ratio = difflib.SequenceMatcher(None, q, f"{entry['title']} {entry['artist']}").ratio()
        score = coverage * 0.7 + artist_hit + ratio * 0.3
        if score > best_score:
            best, best_score = entry, score
    return best if best_score >= 0.5 else None


def _refresh_search_results(win: SimWindow, query: str) -> None:
    """Populate a results list for browser/spotify style search."""
    q = (query or "").strip()
    if not q:
        return
    kind = win.state.get("app", "").lower()
    results = win.state.setdefault("results", [])
    results.clear()
    if kind == "spotify":
        hit = _catalogue_match(q)
        if hit is not None:
            title, artist, album = hit["title"], hit["artist"], hit["album"]
        else:
            title, artist, album = q, "", ""
            parts = [p for p in q.replace(" by ", "|").split("|") if p.strip()]
            if len(parts) == 2:
                title, artist = parts[0].strip(), parts[1].strip()
        for i in range(4):
            results.append({
                "type": "track" if i < 3 else "artist",
                "title": title if i == 0 else f"{title} (Live)",
                "artist": artist or "Unknown Artist",
                "album": album or "Single",
                "y": 240 + i * 64,
            })
    else:
        for i in range(5):
            results.append({
                "type": "result",
                "title": f"{q} - result {i + 1}" if i else f"{q} (official)",
                "url": f"https://example.test/{i}",
                "y": 220 + i * 78,
            })
    _sync_result_elements(win)


def _sync_result_elements(win: SimWindow) -> None:
    """(Re)create clickable list-item elements matching the current results."""
    win.elements = [e for e in win.elements if e.control_type != "ListItem"]
    r = win.rect
    for item in win.state.get("results", []):
        label = item.get("title", "")
        if item.get("artist"):
            label = f"{label} - {item['artist']}"
        y = r.y + int(item.get("y", 240))
        el = SimElement(
            name=label, control_type="ListItem",
            rect=Rect(r.x + 60, y, r.width - 120, 56),
            automation_id=f"result_{abs(hash(label)) % 10**6}",
            patterns=["invoke", "select"],
            on_invoke=lambda w, e, it=item: _open_result(w, it),
        )
        win.elements.append(el)


def _open_result(win: SimWindow, item: Dict[str, Any]) -> None:
    kind = win.state.get("app", "").lower()
    if kind == "spotify":
        win.state["now_playing"] = {
            "title": item.get("title", ""), "artist": item.get("artist", ""),
            "album": item.get("album") or "Single",
        }
        win.state["playing"] = True
        win.state["position_s"] = 0.0
        title = item.get("title", "")
        artist = item.get("artist", "")
        # Real Spotify puts the track in its window title, which is what the
        # window-title fallback in the Spotify integration parses.
        win.set_title(f"{title} - {artist} - Spotify" if artist else f"{title} - Spotify")
    else:
        win.state["current_url"] = item.get("url", "")
        win.state["page_title"] = item.get("title", "")
        win.set_title(f"{item.get('title', '')} - {win.state.get('browser', 'Browser')}")


# ---------------------------------------------------------------------------
# App builders
# ---------------------------------------------------------------------------
def _toolbar(win_rect: Rect, labels: Sequence[str], y_off: int = 44) -> List[SimElement]:
    out: List[SimElement] = []
    x = win_rect.x + 12
    for label in labels:
        width = 18 + 9 * len(label)
        out.append(SimElement(name=label, control_type="Button",
                              rect=Rect(x, win_rect.y + y_off, width, 28)))
        x += width + 8
    return out


def _build_spotify(desktop: SimulatedDesktop) -> SimWindow:
    r = Rect(140, 90, desktop.width - 280, desktop.height - 200)
    els: List[SimElement] = []
    els += _toolbar(r, ["Home", "Search", "Your Library"])
    search = SimElement(
        name="Search", control_type="Edit",
        rect=Rect(r.x + 60, r.y + 96, 520, 40), automation_id="searchInput",
        patterns=["set_value", "invoke"],
        on_invoke=lambda w, e: _refresh_search_results(w, e.value or ""),
        on_set_value=lambda w, e, t: w.state.__setitem__("pending_query", t),
    )
    els.append(search)
    els.append(SimElement(name="What do you want to listen to?", control_type="Text",
                          rect=Rect(r.x + 60, r.y + 150, 520, 24)))
    els.append(SimElement(name="Results", control_type="List",
                          rect=Rect(r.x + 40, r.y + 200, r.width - 80, r.height - 380),
                          patterns=["scroll", "select"]))

    def play(w: SimWindow, e: SimElement) -> str:
        np = w.state.get("now_playing")
        if not np:
            first = next((i for i in w.state.get("results", [])
                          if i.get("type") == "track"), None)
            if first:
                _open_result(w, first)
        w.state["playing"] = True
        return "playing"

    def pause(w: SimWindow, e: SimElement) -> str:
        w.state["playing"] = False
        return "paused"

    els.append(SimElement(name="Play", control_type="Button",
                          rect=Rect(r.x + r.width // 2 - 120, r.bottom - 120, 64, 48),
                          automation_id="playButton", on_invoke=play))
    els.append(SimElement(name="Pause", control_type="Button",
                          rect=Rect(r.x + r.width // 2 - 32, r.bottom - 120, 64, 48),
                          automation_id="pauseButton", on_invoke=pause))
    els.append(SimElement(name="Next", control_type="Button",
                          rect=Rect(r.x + r.width // 2 + 56, r.bottom - 120, 64, 48),
                          automation_id="nextButton",
                          on_invoke=lambda w, e: w.state.__setitem__("skipped",
                                                                     w.state.get("skipped", 0) + 1)))
    els.append(SimElement(name="Previous", control_type="Button",
                          rect=Rect(r.x + r.width // 2 - 208, r.bottom - 120, 64, 48)))
    els.append(SimElement(name="Volume", control_type="Slider",
                          rect=Rect(r.right - 260, r.bottom - 108, 180, 20),
                          patterns=["set_value"], value="42",
                          on_set_value=lambda w, e, t: w.state.__setitem__("volume", t)))
    els.append(SimElement(name="Now Playing", control_type="Text",
                          rect=Rect(r.x + 40, r.bottom - 60, r.width - 80, 28),
                          value="", automation_id="nowPlaying",
                          on_invoke=None))
    state = {"app": "spotify", "playing": False, "now_playing": None,
             "results": [], "volume": 42, "layout_rect": r}
    win = SimWindow(title="Spotify", app_name="Spotify", process_name="Spotify.exe",
                    rect=r, elements=els, state=state)

    # keep the Now Playing label in sync with state
    np_el = win.element("Now Playing")
    if np_el is not None:
        def _sync(w: SimWindow = win, el: SimElement = np_el) -> bool:
            np = w.state.get("now_playing")
            if np:
                el.value = (f"{np.get('title', '')} - {np.get('artist', '')}"
                            + (" (playing)" if w.state.get("playing") else " (paused)"))
            else:
                el.value = "Nothing playing"
            return True
        np_el.visible_if = _sync
    return win


def _build_browser(desktop: SimulatedDesktop) -> SimWindow:
    name = "Chrome"
    r = Rect(140, 90, desktop.width - 280, desktop.height - 200)
    els: List[SimElement] = []
    els += _toolbar(r, ["Back", "Forward", "Reload", "Bookmarks", "Downloads"])
    addr = SimElement(
        name="Address and search bar", control_type="Edit",
        rect=Rect(r.x + 300, r.y + 44, r.width - 460, 32),
        automation_id="addressBar", patterns=["set_value", "invoke"],
        on_invoke=lambda w, e: _navigate(w, e.value or ""),
        on_set_value=lambda w, e, t: w.state.__setitem__("pending_url", t),
    )
    els.append(addr)
    els.append(SimElement(name="Page", control_type="Document",
                          rect=Rect(r.x, r.y + 96, r.width, r.height - 160),
                          automation_id="pageContent", patterns=["set_value"],
                          value=""))
    els.append(SimElement(name="Full screen", control_type="Button",
                          rect=Rect(r.right - 140, r.y + 44, 120, 30),
                          automation_id="fullscreenBtn",
                          on_invoke=lambda w, e: w.state.__setitem__(
                              "fullscreen", not bool(w.state.get("fullscreen")))))
    state = {"app": "browser", "browser": name, "current_url": "chrome://newtab",
             "page_title": "New Tab", "results": [], "fullscreen": False,
             "layout_rect": r, "history": []}
    return SimWindow(title=f"New Tab - {name}", app_name=name,
                     process_name="chrome.exe" if name == "Chrome" else "msedge.exe",
                     rect=r, elements=els, state=state)


def _navigate(win: SimWindow, target: str) -> None:
    t = (target or "").strip()
    if not t:
        return
    win.state["history"] = list(win.state.get("history", [])) + [t]
    if t.startswith("http://") or t.startswith("https://"):
        win.state["current_url"] = t
        win.state["page_title"] = _domain_of(t)
    else:
        win.state["current_url"] = f"https://www.google.com/search?q={t}"
        win.state["page_title"] = f"{t} - Google Search"
        _refresh_search_results(win, t)
    win.set_title(f"{win.state['page_title']} - {win.state.get('browser', 'Browser')}")
    doc = win.element("Page")
    if doc is not None:
        doc.value = win.state["page_title"]


def _domain_of(url: str) -> str:
    try:
        return url.split("://", 1)[1].split("/", 1)[0]
    except IndexError:
        return url


def _build_explorer(desktop: SimulatedDesktop) -> SimWindow:
    r = Rect(140, 90, desktop.width - 280, desktop.height - 200)
    els: List[SimElement] = []
    els += _toolbar(r, ["New", "Cut", "Copy", "Paste", "Rename", "Delete", "Sort"])
    els.append(SimElement(name="Address bar", control_type="Edit",
                          rect=Rect(r.x + 200, r.y + 84, r.width - 500, 30),
                          automation_id="ExplorerAddressBar",
                          patterns=["set_value", "invoke"],
                          on_invoke=lambda w, e: w.state.__setitem__("path", e.value),
                          value=r"C:\Users\user\Downloads"))
    els.append(SimElement(name="Search box", control_type="Edit",
                          rect=Rect(r.right - 280, r.y + 84, 240, 30),
                          automation_id="ExplorerSearch",
                          patterns=["set_value", "invoke"],
                          on_invoke=lambda w, e: w.state.__setitem__("filter", e.value)))
    els.append(SimElement(name="Items", control_type="List",
                          rect=Rect(r.x + 20, r.y + 130, r.width - 40, r.height - 200),
                          automation_id="ItemsView", patterns=["scroll", "select"]))
    els.append(SimElement(name="Status", control_type="Text",
                          rect=Rect(r.x + 20, r.bottom - 50, r.width - 40, 24),
                          value="12 items"))
    state = {"app": "explorer", "path": r"C:\Users\user\Downloads", "filter": "",
             "layout_rect": r}
    return SimWindow(title="Downloads", app_name="File Explorer",
                     process_name="explorer.exe", rect=r, elements=els, state=state)


def _build_notepad(desktop: SimulatedDesktop) -> SimWindow:
    r = Rect(240, 140, desktop.width - 480, desktop.height - 320)
    els: List[SimElement] = []
    els += _toolbar(r, ["File", "Edit", "Format", "View", "Help"], y_off=36)
    els.append(SimElement(name="Text Area", control_type="Document",
                          rect=Rect(r.x + 8, r.y + 80, r.width - 16, r.height - 96),
                          automation_id="15", patterns=["set_value"], value=""))
    els.append(SimElement(name="Password", control_type="Edit", is_password=True,
                          rect=Rect(r.x + 8, r.bottom - 40, 200, 26),
                          automation_id="pwdDemo", patterns=["set_value"]))
    state = {"app": "notepad", "dirty": False, "layout_rect": r}
    return SimWindow(title="Untitled - Notepad", app_name="Notepad",
                     process_name="notepad.exe", rect=r, elements=els, state=state)


def _build_settings(desktop: SimulatedDesktop) -> SimWindow:
    r = Rect(180, 110, desktop.width - 360, desktop.height - 240)
    els: List[SimElement] = []
    for i, label in enumerate(["System", "Bluetooth & devices", "Network & internet",
                               "Personalisation", "Apps", "Accounts", "Privacy & security",
                               "Windows Update", "Accessibility"]):
        els.append(SimElement(name=label, control_type="ListItem",
                              rect=Rect(r.x + 16, r.y + 90 + i * 46, 280, 40),
                              patterns=["invoke"],
                              on_invoke=lambda w, e, l=label: w.state.__setitem__("page", l)))
    els.append(SimElement(name="Dark mode", control_type="ToggleButton",
                          rect=Rect(r.x + 340, r.y + 120, 220, 40),
                          automation_id="DarkModeToggle", patterns=["toggle", "invoke"],
                          on_invoke=lambda w, e: desktop.system_state.__setitem__(
                              "dark_mode", not desktop.system_state.get("dark_mode"))))
    els.append(SimElement(name="Search settings", control_type="Edit",
                          rect=Rect(r.x + 340, r.y + 44, 400, 34),
                          automation_id="SettingsSearch", patterns=["set_value", "invoke"]))
    state = {"app": "settings", "page": "System", "layout_rect": r}
    return SimWindow(title="Settings", app_name="Settings",
                     process_name="SystemSettings.exe", rect=r, elements=els,
                     state=state)


def _build_discord(desktop: SimulatedDesktop) -> SimWindow:
    r = Rect(140, 90, desktop.width - 280, desktop.height - 200)
    els: List[SimElement] = []
    els.append(SimElement(name="Servers", control_type="List",
                          rect=Rect(r.x + 8, r.y + 48, 72, r.height - 64)))
    els.append(SimElement(name="Channels", control_type="List",
                          rect=Rect(r.x + 88, r.y + 48, 240, r.height - 64)))
    els.append(SimElement(name="Messages", control_type="List",
                          rect=Rect(r.x + 340, r.y + 48, r.width - 700, r.height - 160),
                          automation_id="chatMessages"))
    els.append(SimElement(name="Message Input", control_type="Edit",
                          rect=Rect(r.x + 340, r.bottom - 96, r.width - 700, 48),
                          automation_id="channelTextArea", patterns=["set_value", "invoke"]))
    els.append(SimElement(name="Inbox", control_type="Button",
                          rect=Rect(r.right - 120, r.y + 12, 88, 28),
                          automation_id="inboxBtn",
                          on_invoke=lambda w, e: w.state.__setitem__("inbox_open", True)))
    state = {"app": "discord", "unread": 3, "inbox_open": False, "layout_rect": r,
             "channels": ["general", "music", "dev"],
             "messages": ["alice: hey", "bob: are we on tonight?",
                          "alice: 8pm?"],
             "sent": []}
    return SimWindow(title="Discord", app_name="Discord", process_name="Discord.exe",
                     rect=r, elements=els, state=state)


def _build_generic(app_name: str) -> SimWindow:
    """A window for software JARVIS has never seen before.

    Deliberately minimal: a menu bar, a toolbar, a document area and a status
    bar.  The agent has to work out what to do from observation alone.
    """
    r = Rect(140, 90, SCREEN_W - 280, SCREEN_H - 200)
    els: List[SimElement] = []
    els += _toolbar(r, ["File", "Edit", "View", "Window", "Help"], y_off=36)
    els += _toolbar(r, ["New", "Open", "Save", "Undo", "Redo", "Preferences"], y_off=72)
    els.append(SimElement(name="Workspace", control_type="Pane",
                          rect=Rect(r.x + 12, r.y + 112, r.width - 24, r.height - 176),
                          automation_id="workspace", patterns=["invoke"]))
    els.append(SimElement(name="New Composition", control_type="Button",
                          rect=Rect(r.x + 40, r.y + 140, 200, 40),
                          automation_id="newComp",
                          visible_if=lambda w: bool(w.state.get("menu_open") == "File"
                                                    or True)))
    els.append(SimElement(name="Status Bar", control_type="Text",
                          rect=Rect(r.x + 12, r.bottom - 52, r.width - 24, 26),
                          value="Ready"))
    state = {"app": app_name.lower(), "generic": True, "layout_rect": r,
             "menu_open": "", "compositions": []}
    win = SimWindow(title=f"{app_name}", app_name=app_name,
                    process_name=f"{app_name.replace(' ', '')}.exe",
                    rect=r, elements=els, state=state)

    def new_comp(w: SimWindow, e: SimElement) -> str:
        w.state["compositions"] = list(w.state.get("compositions", [])) + [
            f"Comp {len(w.state.get('compositions', [])) + 1}"]
        w.set_title(f"{', '.join(w.state['compositions'])} - {app_name}")
        sb = w.element("Status Bar")
        if sb:
            sb.value = f"Created {w.state['compositions'][-1]}"
        return "created"

    nc = win.element("New Composition")
    if nc is not None:
        nc.on_invoke = new_comp
    return win
