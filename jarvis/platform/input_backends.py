"""Mouse & keyboard backends.

Primary:   ``pyautogui`` (well tested on Windows, brings its own pause/failsafe)
Fallback:  raw Win32 ``SendInput`` through ``ctypes`` - no third-party package,
           used when pyautogui is missing or raises (it occasionally fails on
           secure desktop transitions and multi-DPI setups).
Linux:     ``xdotool`` / ``ydotool`` when present (X11/Wayland respectively).
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess

from .. import _proc
import time
from typing import Optional, Sequence


class _POINT(ctypes.Structure):
    """Win32 POINT - module scope so every backend shares one definition."""
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

from .base import Point

log = logging.getLogger(__name__)

# Keys that must be translated before they reach the OS layer.
_KEY_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl",
    "alt": "alt", "option": "alt",
    "shift": "shift",
    "win": "winleft", "windows": "winleft", "super": "winleft", "cmd": "winleft",
    "return": "enter", "cr": "enter",
    "esc": "escape",
    "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown", "page down": "pagedown",
    "page up": "pageup",
    "space": "space", "spacebar": "space", " ": "space",
    "backspace": "backspace", "bksp": "backspace",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left",
    "arrowright": "right",
    "printscreen": "printscreen", "prtsc": "printscreen",
    "media_play_pause": "playpause", "playpause": "playpause",
    "volumedown": "volumedown", "volumeup": "volumeup", "volumemute": "volumemute",
    "next": "nexttrack", "prev": "prevtrack", "previous": "prevtrack",
}


def normalise_keys(spec: str | Sequence[str]) -> list:
    """``"ctrl+shift+t"`` or ``["ctrl","shift","t"]`` -> ``["ctrl","shift","t"]``"""
    if isinstance(spec, str):
        parts = [p for p in spec.replace("-", "+").split("+") if p.strip()]
    else:
        parts = [str(p) for p in spec if str(p).strip()]
    out = []
    for p in parts:
        key = p.strip().lower()
        if len(key) > 1:
            key = _KEY_ALIASES.get(key, key)
        out.append(key)
    return out


# ---------------------------------------------------------------------------
class PyAutoGUIInput:
    name = "pyautogui"

    def __init__(self, failsafe: bool = True) -> None:
        import pyautogui  # type: ignore

        pyautogui.FAILSAFE = failsafe
        pyautogui.PAUSE = 0.0
        self._p = pyautogui

    # -- mouse ---------------------------------------------------------
    def move(self, x: int, y: int, duration: float = 0.0) -> bool:
        self._p.moveTo(int(x), int(y), duration=max(0.0, float(duration)))
        return True

    def click(self, x: Optional[int] = None, y: Optional[int] = None,
              button: str = "left", clicks: int = 1) -> bool:
        button = (button or "left").lower()
        if button == "middle":
            button = "middle"
        elif button not in ("left", "right"):
            button = "left"
        clicks = max(1, min(int(clicks), 4))
        if x is None or y is None:
            self._p.click(button=button, clicks=clicks)
        else:
            self._p.click(int(x), int(y), button=button, clicks=clicks)
        return True

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3,
             button: str = "left") -> bool:
        self._p.moveTo(int(x1), int(y1), duration=0.05)
        self._p.dragTo(int(x2), int(y2), duration=max(0.05, float(duration)),
                       button=button or "left")
        return True

    def scroll(self, dx: int = 0, dy: int = -3, x: Optional[int] = None,
               y: Optional[int] = None) -> bool:
        if x is not None and y is not None:
            self._p.moveTo(int(x), int(y), duration=0.0)
        # pyautogui: positive = up
        clicks = -int(dy) * 120
        if clicks:
            self._p.scroll(clicks)
        if dx:
            try:
                self._p.hscroll(-int(dx) * 120)
            except Exception:  # noqa: BLE001 - hscroll is missing on some platforms
                pass
        return True

    def position(self) -> Point:
        p = self._p.position()
        return Point(int(p[0]), int(p[1]))

    # -- keyboard -------------------------------------------------------
    def type_text(self, text: str, interval: float = 0.01) -> bool:
        if not text:
            return True
        try:
            self._p.typewrite(text, interval=max(0.0, float(interval)))
            return True
        except Exception as exc:  # noqa: BLE001 - non-ASCII falls back below
            log.debug("typewrite failed (%s); using write+hotkey fallback", exc)
        for chunk in _split_ascii(text):
            if chunk[0]:
                try:
                    self._p.typewrite(chunk[0], interval=max(0.0, float(interval)))
                except Exception:  # noqa: BLE001
                    pass
            if chunk[1]:
                _clipboard_paste(chunk[1])
        return True

    def press(self, keys: Sequence[str]) -> bool:
        norm = normalise_keys(keys)
        if not norm:
            return False
        if len(norm) == 1:
            self._p.press(norm[0])
        else:
            self._p.hotkey(*norm)
        return True

    def key_down(self, key: str) -> bool:
        self._p.keyDown(normalise_keys(key)[0])
        return True

    def key_up(self, key: str) -> bool:
        self._p.keyUp(normalise_keys(key)[0])
        return True


def _split_ascii(text: str):
    """Yield (ascii_run, unicode_run) pairs."""
    out = []
    cur_a, cur_u = "", ""
    for ch in text:
        if ord(ch) < 128:
            if cur_u:
                out.append(("", cur_u)); cur_u = ""
            cur_a += ch
        else:
            if cur_a:
                out.append((cur_a, "")); cur_a = ""
            cur_u += ch
    if cur_a or cur_u:
        out.append((cur_a, cur_u))
    return out


def _clipboard_paste(text: str) -> bool:
    """Type unicode text via the clipboard (restores the previous contents)."""
    try:
        prev = _clipboard_get()
    except Exception:  # noqa: BLE001
        prev = None
    if not _clipboard_set(text):
        return False
    try:
        import pyautogui  # type: ignore

        pyautogui.hotkey("ctrl", "v")
    except Exception:  # noqa: BLE001
        return False
    time.sleep(0.05)
    if prev is not None and prev != text:
        _clipboard_set(prev)
    return True


def _clipboard_set(text: str) -> bool:
    try:
        import ctypes

        CF_UNICODETEXT = 13
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32  # type: ignore[attr-defined]
        if not user32.OpenClipboard(0):
            return False
        try:
            user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            h = kernel32.GlobalAlloc(0x0042, len(data))
            ptr = kernel32.GlobalLock(h)
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(h)
            user32.SetClipboardData(CF_UNICODETEXT, h)
            return True
        finally:
            user32.CloseClipboard()
    except Exception as exc:  # noqa: BLE001
        log.debug("clipboard set failed: %s", exc)
        return False


def _clipboard_get() -> Optional[str]:
    try:
        import ctypes

        CF_UNICODETEXT = 13
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32  # type: ignore[attr-defined]
        if not user32.OpenClipboard(0):
            return None
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            ptr = kernel32.GlobalLock(h)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
class Win32CtypesInput:
    """Dependency-free Win32 input using ``mouse_event`` / ``keybd_event``."""

    name = "win32-ctypes"

    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_ABSOLUTE = 0x8000
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_EXTENDEDKEY = 0x0001

    def __init__(self) -> None:
        import ctypes

        self._ct = ctypes
        self._u = ctypes.windll.user32  # type: ignore[attr-defined]

    # -- helpers --------------------------------------------------------
    def _abs(self, x: int, y: int) -> tuple:
        w = self._u.GetSystemMetrics(0) or 1920
        h = self._u.GetSystemMetrics(1) or 1080
        return int(x * 65535 / max(1, w - 1)), int(y * 65535 / max(1, h - 1))

    def move(self, x: int, y: int, duration: float = 0.0) -> bool:
        ax, ay = self._abs(int(x), int(y))
        steps = 1 if duration <= 0 else max(1, min(24, int(duration / 0.012)))
        cx, cy = self.position().as_tuple()
        for i in range(1, steps + 1):
            t = i / steps
            ix, iy = self._abs(int(cx + (x - cx) * t), int(cy + (y - cy) * t))
            self._u.mouse_event(self.MOUSEEVENTF_MOVE | self.MOUSEEVENTF_ABSOLUTE,
                              ix, iy, 0, 0)
            if steps > 1:
                time.sleep(max(0.0, duration) / steps)
        return True

    def click(self, x: Optional[int] = None, y: Optional[int] = None,
              button: str = "left", clicks: int = 1) -> bool:
        if x is not None and y is not None:
            self.move(x, y, duration=0.02)
        down, up = {
            "left": (self.MOUSEEVENTF_LEFTDOWN, self.MOUSEEVENTF_LEFTUP),
            "right": (self.MOUSEEVENTF_RIGHTDOWN, self.MOUSEEVENTF_RIGHTUP),
            "middle": (self.MOUSEEVENTF_MIDDLEDOWN, self.MOUSEEVENTF_MIDDLEUP),
        }.get((button or "left").lower(),
              (self.MOUSEEVENTF_LEFTDOWN, self.MOUSEEVENTF_LEFTUP))
        for _ in range(max(1, min(int(clicks), 4))):
            self._u.mouse_event(down, 0, 0, 0, 0)
            self._u.mouse_event(up, 0, 0, 0, 0)
            time.sleep(0.02)
        return True

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3,
             button: str = "left") -> bool:
        self.move(x1, y1, 0.05)
        down, up = (self.MOUSEEVENTF_LEFTDOWN, self.MOUSEEVENTF_LEFTUP)
        if (button or "left") == "right":
            down, up = self.MOUSEEVENTF_RIGHTDOWN, self.MOUSEEVENTF_RIGHTUP
        self._u.mouse_event(down, 0, 0, 0, 0)
        self.move(x2, y2, max(0.05, duration))
        self._u.mouse_event(up, 0, 0, 0, 0)
        return True

    def scroll(self, dx: int = 0, dy: int = -3, x: Optional[int] = None,
               y: Optional[int] = None) -> bool:
        if x is not None and y is not None:
            self.move(x, y, 0.02)
        MOUSEEVENTF_WHEEL = 0x0800
        MOUSEEVENTF_HWHEEL = 0x1000
        delta = -int(dy) * 120
        if delta:
            self._u.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
        if dx:
            self._u.mouse_event(MOUSEEVENTF_HWHEEL, 0, 0, -int(dx) * 120, 0)
        return True

    def position(self) -> Point:
        p = _POINT()
        self._u.GetCursorPos(self._ct.byref(p))
        return Point(int(p.x), int(p.y))

    def type_text(self, text: str, interval: float = 0.01) -> bool:
        for ch in text or "":
            code = ord(ch)
            self._u.keybd_event(0, code, self.KEYEVENTF_UNICODE, 0)
            self._u.keybd_event(0, code, self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP, 0)
            if interval:
                time.sleep(interval)
        return True

    def press(self, keys: Sequence[str]) -> bool:
        codes = [_vk(k) for k in normalise_keys(keys)]
        codes = [c for c in codes if c]
        if not codes:
            return False
        for c in codes:
            self._u.keybd_event(c, 0, _ext_flag(c), 0)
            time.sleep(0.008)
        for c in reversed(codes):
            self._u.keybd_event(c, 0, self.KEYEVENTF_KEYUP | _ext_flag(c), 0)
            time.sleep(0.008)
        return True

    def key_down(self, key: str) -> bool:
        c = _vk(normalise_keys(key)[0])
        if not c:
            return False
        self._u.keybd_event(c, 0, _ext_flag(c), 0)
        return True

    def key_up(self, key: str) -> bool:
        c = _vk(normalise_keys(key)[0])
        if not c:
            return False
        self._u.keybd_event(c, 0, self.KEYEVENTF_KEYUP | _ext_flag(c), 0)
        return True


_VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12, "pause": 0x13,
    "capslock": 0x14, "escape": 0x1B, "esc": 0x1B, "space": 0x20, "pageup": 0x21,
    "pagedown": 0x22, "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "printscreen": 0x2C, "insert": 0x2D,
    "delete": 0x2E, "winleft": 0x5B, "winright": 0x5C, "apps": 0x5D,
    "sleep": 0x5F, "num0": 0x60, "multiply": 0x6A, "add": 0x6B, "separator": 0x6C,
    "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F, "f1": 0x70, "f2": 0x71,
    "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B, "f13": 0x7C, "f14": 0x7D,
    "f15": 0x7E, "f16": 0x7F, "f17": 0x80, "f18": 0x81, "f19": 0x82, "f20": 0x83,
    "f21": 0x84, "f22": 0x85, "f23": 0x86, "f24": 0x87, "numlock": 0x90,
    "scrolllock": 0x91, "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "nexttrack": 0xB0, "prevtrack": 0x91, "playpause": 0xB3, "stop": 0xB2,
}
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2C,
             0x5B, 0x5C, 0x5D, 0x6F, 0x78, 0x79, 0x7A, 0x7B, 0x7C, 0x7D,
             0x7E, 0x7F, 0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87}


def _vk(key: str) -> int:
    key = (key or "").lower()
    if key in _VK:
        return _VK[key]
    if len(key) == 1:
        return ord(key.upper())
    return 0


def _ext_flag(code: int) -> int:
    return 0x0001 if code in _EXTENDED else 0


# ---------------------------------------------------------------------------
class XDoToolInput:
    """Linux/X11 fallback via the ``xdotool`` CLI."""

    name = "xdotool"

    def __init__(self, binary: str = "xdotool") -> None:
        self.bin = binary

    def _run(self, *args: str) -> bool:
        try:
            out = _proc.run([self.bin, *args], capture_output=True, text=True,
                                 timeout=10)
            return out.returncode == 0
        except Exception as exc:  # noqa: BLE001
            log.debug("xdotool %s failed: %s", args, exc)
            return False

    def move(self, x: int, y: int, duration: float = 0.0) -> bool:
        return self._run("mousemove", str(int(x)), str(int(y)))

    def click(self, x=None, y=None, button: str = "left", clicks: int = 1) -> bool:
        b = {"left": "1", "middle": "2", "right": "3"}.get((button or "left").lower(), "1")
        if x is not None and y is not None:
            self.move(x, y)
        return self._run("click", "--repeat", str(max(1, int(clicks))), b)

    def drag(self, x1, y1, x2, y2, duration: float = 0.3, button: str = "left") -> bool:
        self.move(x1, y1)
        self._run("mousedown", "1")
        self.move(x2, y2, duration)
        return self._run("mouseup", "1")

    def scroll(self, dx: int = 0, dy: int = -3, x=None, y=None) -> bool:
        button = "4" if dy < 0 else "5"
        for _ in range(min(abs(int(dy)), 20)):
            self._run("click", button)
        return True

    def position(self) -> Point:
        try:
            out = _proc.run([self.bin, "getmouselocation", "--shell"],
                                 capture_output=True, text=True, timeout=5)
            vals = dict(l.split("=", 1) for l in out.stdout.splitlines() if "=" in l)
            return Point(int(vals.get("X", 0)), int(vals.get("Y", 0)))
        except Exception:  # noqa: BLE001
            return Point(0, 0)

    def type_text(self, text: str, interval: float = 0.01) -> bool:
        return self._run("type", "--delay", str(int(max(1, interval * 1000))),
                         "--", text or "")

    def press(self, keys: Sequence[str]) -> bool:
        combo = "+".join(normalise_keys(keys))
        return self._run("key", "--", combo)

    def key_down(self, key: str) -> bool:
        return self._run("keydown", normalise_keys(key)[0])

    def key_up(self, key: str) -> bool:
        return self._run("keyup", normalise_keys(key)[0])


# ---------------------------------------------------------------------------
class NullInput:
    """Used when no input backend is available - every call reports failure
    honestly instead of pretending to work."""

    name = "none"

    def __init__(self, reason: str = "no input backend available") -> None:
        self.reason = reason

    def _fail(self, *_a, **_k):
        log.warning("Input backend unavailable (%s)", self.reason)
        return False

    move = click = drag = scroll = type_text = press = key_down = key_up = _fail

    def position(self) -> Point:
        return Point(0, 0)


# ---------------------------------------------------------------------------
def create_input_backend(preferred: str = "auto"):
    """Return (backend, capability_detail)."""
    order: list = []
    if os.name == "nt":
        order = ["pyautogui", "win32"] if preferred == "auto" else [preferred]
    elif shutil.which("xdotool"):
        order = ["xdotool"] if preferred == "auto" else [preferred]
    else:
        order = ["pyautogui"] if preferred == "auto" else [preferred]

    errors = []
    for choice in order:
        try:
            if choice == "pyautogui":
                return PyAutoGUIInput(), "pyautogui"
            if choice in ("win32", "ctypes"):
                if os.name != "nt":
                    errors.append("win32 backend needs Windows")
                    continue
                return Win32CtypesInput(), "win32 SendInput/keybd_event (ctypes)"
            if choice == "xdotool":
                if not shutil.which("xdotool"):
                    errors.append("xdotool not installed")
                    continue
                return XDoToolInput(), "xdotool"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{choice}: {exc}")
            log.debug("Input backend %s unavailable: %s", choice, exc)
    return NullInput("; ".join(errors) or "unsupported platform"), \
        ("; ".join(errors) or "unsupported platform")
