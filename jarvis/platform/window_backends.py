"""Window and process enumeration / manipulation.

Windows: pure ``ctypes`` against ``user32`` (EnumWindows, SetForegroundWindow,
ShowWindow, MoveWindow, PostMessage WM_CLOSE).  No pywin32 required, which
keeps the frozen executable smaller and removes a frequent packaging problem.
Process metadata comes from ``psutil``.

Linux/X11: ``wmctrl`` + ``xdotool`` when installed.

Everything degrades to :class:`NullWindows` with an honest reason string rather
than raising, so the UI can tell the user exactly what is missing.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess

from .. import _proc
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import ProcessInfo, Rect, WindowInfo

log = logging.getLogger(__name__)

WM_CLOSE = 0x0010
SW_HIDE, SW_SHOWNORMAL, SW_SHOWMINIMIZED, SW_SHOWMAXIMIZED = 0, 1, 2, 3
SW_RESTORE, SW_SHOW = 9, 5


class Win32Windows:
    name = "win32"

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Win32 window backend requires Windows")
        self._u = ctypes.windll.user32      # type: ignore[attr-defined]
        self._psutil = None
        try:
            import psutil  # type: ignore

            self._psutil = psutil
        except Exception as exc:  # noqa: BLE001
            log.debug("psutil unavailable (%s); process metadata will be limited", exc)

    # ------------------------------------------------------------------
    def _title(self, hwnd: int) -> str:
        length = self._u.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        self._u.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value or ""

    def _class(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        self._u.GetClassNameW(hwnd, buf, 256)
        return buf.value or ""

    def _rect(self, hwnd: int) -> Rect:
        class R(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        r = R()
        self._u.GetWindowRect(hwnd, ctypes.byref(r))
        return Rect(r.left, r.top, max(0, r.right - r.left), max(0, r.bottom - r.top))

    def _pid(self, hwnd: int) -> int:
        pid = ctypes.c_ulong(0)
        self._u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def windows(self, include_untitled: bool = False) -> List[WindowInfo]:
        out: List[WindowInfo] = []
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        fg = self._u.GetForegroundWindow()
        names = self._process_names()

        def cb(hwnd, _lparam):
            try:
                hwnd = int(hwnd or 0)
                if not hwnd or not self._u.IsWindowVisible(hwnd):
                    return True
                title = self._title(hwnd)
                if not title and not include_untitled:
                    return True
                cls = self._class(hwnd)
                # Skip tool windows / cloaked UWP shells that have no real UI.
                if cls in ("Progman", "WorkerW", "Shell_TrayWnd",
                           "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow"):
                    if not title:
                        return True
                pid = self._pid(hwnd)
                out.append(WindowInfo(
                    title=title, handle=hwnd, pid=pid,
                    process_name=names.get(pid, ""),
                    rect=self._rect(hwnd),
                    is_active=(hwnd == fg),
                    is_minimised=bool(self._u.IsIconic(hwnd)),
                    is_maximised=bool(self._u.IsZoomed(hwnd)),
                    class_name=cls,
                ))
            except Exception:  # noqa: BLE001 - one bad window must not kill the scan
                log.debug("window enumeration error", exc_info=True)
            return True

        self._u.EnumWindows(EnumProc(cb), 0)
        return out

    def _process_names(self) -> Dict[int, str]:
        if self._psutil is None:
            return {}
        out: Dict[int, str] = {}
        try:
            for p in self._psutil.process_iter(["pid", "name"]):
                try:
                    out[int(p.info["pid"])] = str(p.info.get("name") or "")
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        return out

    def active(self) -> Optional[WindowInfo]:
        hwnd = self._u.GetForegroundWindow()
        if not hwnd:
            return None
        names = self._process_names()
        pid = self._pid(hwnd)
        return WindowInfo(
            title=self._title(hwnd), handle=int(hwnd), pid=pid,
            process_name=names.get(pid, ""), rect=self._rect(hwnd), is_active=True,
            is_minimised=bool(self._u.IsIconic(hwnd)),
            is_maximised=bool(self._u.IsZoomed(hwnd)),
            class_name=self._class(hwnd),
        )

    # ------------------------------------------------------------------
    def _bring_to_front(self, hwnd: int) -> bool:
        """ForegroundWindow without stealing focus from a fullscreen app when
        the target is already foreground."""
        try:
            if self._u.GetForegroundWindow() == hwnd:
                return True
            if self._u.IsIconic(hwnd):
                self._u.ShowWindow(hwnd, SW_RESTORE)
            # AttachThreadInput trick: Windows refuses SetForegroundWindow unless
            # the calling thread owns the foreground.  Simulating an ALT key
            # releases that restriction and is the standard workaround.
            self._u.keybd_event(0x12, 0, 0, 0)     # ALT down
            self._u.keybd_event(0x12, 0, 2, 0)     # ALT up
            ok = bool(self._u.SetForegroundWindow(hwnd))
            self._u.BringWindowToTop(hwnd)
            self._u.ShowWindow(hwnd, SW_SHOW)
            deadline = time.time() + 1.5
            while time.time() < deadline:
                if self._u.GetForegroundWindow() == hwnd:
                    return True
                time.sleep(0.03)
            return ok
        except Exception as exc:  # noqa: BLE001
            log.debug("focus failed for %s: %s", hwnd, exc)
            return False

    def focus(self, win: WindowInfo) -> bool:
        return self._bring_to_front(int(win.handle or 0))

    def minimise(self, win: WindowInfo) -> bool:
        return bool(self._u.ShowWindow(int(win.handle or 0), SW_SHOWMINIMIZED))

    def maximise(self, win: WindowInfo) -> bool:
        return bool(self._u.ShowWindow(int(win.handle or 0), SW_SHOWMAXIMIZED))

    def restore(self, win: WindowInfo) -> bool:
        self._u.ShowWindow(int(win.handle or 0), SW_RESTORE)
        return self._bring_to_front(int(win.handle or 0))

    def close(self, win: WindowInfo) -> bool:
        hwnd = int(win.handle or 0)
        if not hwnd:
            return False
        return bool(self._u.PostMessageW(hwnd, WM_CLOSE, 0, 0))

    def move_resize(self, win: WindowInfo, rect: Rect) -> bool:
        return bool(self._u.MoveWindow(int(win.handle or 0), rect.x, rect.y,
                                       rect.width, rect.height, True))

    # ------------------------------------------------------------------
    def processes(self) -> List[ProcessInfo]:
        if self._psutil is None:
            return []
        wins = self.windows()
        by_pid: Dict[int, List[str]] = {}
        for w in wins:
            by_pid.setdefault(w.pid, []).append(w.title)
        out: List[ProcessInfo] = []
        try:
            for p in self._psutil.process_iter(
                    ["pid", "name", "exe", "memory_info", "create_time"]):
                try:
                    info = p.info
                    mem = info.get("memory_info")
                    out.append(ProcessInfo(
                        pid=int(info["pid"]),
                        name=str(info.get("name") or ""),
                        executable=str(info.get("exe") or ""),
                        memory_mb=(mem.rss / 1024 / 1024) if mem else 0.0,
                        create_time=float(info.get("create_time") or 0.0),
                        windows=by_pid.get(int(info["pid"]), []),
                    ))
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            log.debug("process iteration failed", exc_info=True)
        out.sort(key=lambda p: p.memory_mb, reverse=True)
        return out

    def process_running(self, name: str) -> List[ProcessInfo]:
        low = (name or "").lower()
        if not low:
            return []
        hits = []
        for p in self.processes():
            if low in p.name.lower() or low in os.path.basename(p.executable).lower():
                hits.append(p)
        return hits


# ---------------------------------------------------------------------------
class X11Windows:
    """wmctrl/xdotool based backend for Linux desktops (development hosts)."""

    name = "x11"

    def __init__(self) -> None:
        self.wmctrl = shutil.which("wmctrl")
        self.xdotool = shutil.which("xdotool")
        if not self.wmctrl and not self.xdotool:
            raise OSError("neither wmctrl nor xdotool is installed")
        self._psutil = None
        try:
            import psutil  # type: ignore

            self._psutil = psutil
        except Exception:  # noqa: BLE001
            pass

    def _run(self, cmd: List[str], timeout: float = 6.0) -> str:
        try:
            out = _proc.run(cmd, capture_output=True, text=True, timeout=timeout)
            return out.stdout or ""
        except Exception as exc:  # noqa: BLE001
            log.debug("%s failed: %s", cmd, exc)
            return ""

    def windows(self) -> List[WindowInfo]:
        out: List[WindowInfo] = []
        if self.wmctrl:
            text = self._run([self.wmctrl, "-l", "-G", "-p"])
            for line in text.splitlines():
                parts = line.split(None, 8)
                if len(parts) < 8:
                    continue
                try:
                    hid = int(parts[0], 16)
                    pid = int(parts[2])
                    x, y, w, h = (int(v) for v in parts[3:7])
                except ValueError:
                    continue
                title = parts[7] if len(parts) > 7 else ""
                out.append(WindowInfo(title=title, handle=hid, pid=pid,
                                      process_name=self._pid_name(pid),
                                      rect=Rect(x, y, w, h), class_name=""))
        elif self.xdotool:
            ids = self._run([self.xdotool, "search", "--onlyvisible", "--name", ""]).split()
            active = self._run([self.xdotool, "getactivewindow"]).strip()
            for sid in ids:
                name = self._run([self.xdotool, "getwindowname", sid]).strip()
                geo = self._run([self.xdotool, "getwindowgeometry", "--shell", sid])
                vals = dict(l.split("=", 1) for l in geo.splitlines() if "=" in l)
                pid = int(self._run([self.xdotool, "getwindowpid", sid]).strip() or 0)
                out.append(WindowInfo(
                    title=name, handle=int(sid), pid=pid,
                    process_name=self._pid_name(pid),
                    rect=Rect(int(vals.get("X", 0)), int(vals.get("Y", 0)),
                              int(vals.get("WIDTH", 0)), int(vals.get("HEIGHT", 0))),
                    is_active=(sid == active)))
        return out

    def _pid_name(self, pid: int) -> str:
        if not pid or self._psutil is None:
            return ""
        try:
            return self._psutil.Process(pid).name()
        except Exception:  # noqa: BLE001
            return ""

    def active(self) -> Optional[WindowInfo]:
        for w in self.windows():
            if w.is_active:
                return w
        ws = self.windows()
        return ws[0] if ws else None

    def focus(self, win: WindowInfo) -> bool:
        if self.wmctrl:
            return self._run([self.wmctrl, "-i", "-a", hex(int(win.handle))]) is not None
        return bool(self._run([self.xdotool, "windowactivate", str(win.handle)]))

    def minimise(self, win: WindowInfo) -> bool:
        if self.wmctrl:
            self._run([self.wmctrl, "-i", "-r", hex(int(win.handle)),
                       "-b", "add,hidden"])
            return True
        return bool(self._run([self.xdotool, "windowminimize", str(win.handle)]))

    def maximise(self, win: WindowInfo) -> bool:
        if self.wmctrl:
            self._run([self.wmctrl, "-i", "-r", hex(int(win.handle)),
                       "-b", "add,maximized_vert,maximized_horz"])
            return True
        return True

    def restore(self, win: WindowInfo) -> bool:
        if self.wmctrl:
            self._run([self.wmctrl, "-i", "-r", hex(int(win.handle)),
                       "-b", "remove,hidden,maximized_vert,maximized_horz"])
        return self.focus(win)

    def close(self, win: WindowInfo) -> bool:
        if self.wmctrl:
            self._run([self.wmctrl, "-i", "-c", hex(int(win.handle))])
            return True
        return bool(self._run([self.xdotool, "windowclose", str(win.handle)]))

    def move_resize(self, win: WindowInfo, rect: Rect) -> bool:
        if self.wmctrl:
            self._run([self.wmctrl, "-i", "-r", hex(int(win.handle)), "-e",
                       f"0,{rect.x},{rect.y},{rect.width},{rect.height}"])
            return True
        return bool(self._run([self.xdotool, "windowmove", str(win.handle),
                               str(rect.x), str(rect.y)]))

    def processes(self) -> List[ProcessInfo]:
        if self._psutil is None:
            return []
        out = []
        for p in self._psutil.process_iter(["pid", "name", "exe", "memory_info"]):
            try:
                mem = p.info.get("memory_info")
                out.append(ProcessInfo(pid=int(p.info["pid"]),
                                       name=str(p.info.get("name") or ""),
                                       executable=str(p.info.get("exe") or ""),
                                       memory_mb=(mem.rss / 1024 / 1024) if mem else 0.0))
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda p: p.memory_mb, reverse=True)
        return out

    def process_running(self, name: str) -> List[ProcessInfo]:
        low = (name or "").lower()
        return [p for p in self.processes() if low in p.name.lower()]


# ---------------------------------------------------------------------------
class NullWindows:
    name = "none"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def windows(self) -> List[WindowInfo]:
        return []

    def active(self) -> Optional[WindowInfo]:
        return None

    def focus(self, win) -> bool:
        return False

    minimise = maximise = restore = close = focus

    def move_resize(self, win, rect) -> bool:
        return False

    def processes(self) -> List[ProcessInfo]:
        return []

    def process_running(self, name: str) -> List[ProcessInfo]:
        return []


def create_window_backend(preferred: str = "auto"):
    errors: List[str] = []
    order = (["win32", "x11"] if os.name == "nt" or preferred == "auto"
             else [preferred])
    if os.name != "nt":
        order = ["x11", "win32"] if preferred == "auto" else [preferred]
    for choice in order:
        try:
            if choice == "win32":
                return Win32Windows(), "Win32 user32 (ctypes)"
            if choice == "x11":
                return X11Windows(), "wmctrl/xdotool"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{choice}: {exc}")
            log.debug("window backend %s unavailable: %s", choice, exc)
    return NullWindows("; ".join(errors)), ("; ".join(errors) or "unsupported platform")
