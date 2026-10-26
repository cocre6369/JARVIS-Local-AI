"""Window, process and application control."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from ..security.policy import Risk
from . import timing
from .base import Tool, ToolContext, ToolResult, ToolSpec, Verification

log = logging.getLogger(__name__)

_WINDOW_PARAMS = {
    "type": "object",
    "properties": {
        "title": {"type": "string",
                  "description": "Window title, application name or a distinctive "
                                 "part of either. Matched fuzzily."},
    },
    "required": ["title"],
}


def _find_window(platform: Any, query: str) -> Optional[Any]:
    from ..vision.service import _pick_window

    try:
        windows = platform.windows.windows() or []
    except Exception as exc:  # noqa: BLE001
        log.debug("window enumeration failed: %s", exc)
        return None
    return _pick_window(query or "", windows)


def _window_dict(w: Any) -> Dict[str, Any]:
    return w.as_dict() if hasattr(w, "as_dict") else dict(w or {})


class LaunchApplicationTool(Tool):
    spec = ToolSpec(
        name="launch_application",
        description=(
            "Start an installed application by name ('Spotify', 'Chrome', "
            "'After Effects', 'Blender'). The name is resolved against the "
            "Start Menu, the registry App Paths, Store apps and PATH, then "
            "verified: this tool waits until the application's window actually "
            "appears (or its process is running) and reports whether it did. If "
            "it is already running, the existing window is focused instead of "
            "starting a second copy. Cannot and will not uninstall anything."),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Application name."},
                "arguments": {"type": "array", "items": {"type": "string"},
                              "description": "Optional command-line arguments."},
                "wait_s": {"type": "number",
                           "description": "Seconds to wait for the window to appear "
                                          "(default 12)."},
            },
            "required": ["name"],
        },
        category="application",
        risk=Risk.ALLOWED,
        reversible=True,
        requires=("applications",),
        cost="medium",
    )

    def run(self, name: str = "", arguments: Optional[List[str]] = None,
            wait_s: float = 0.0, **_: Any) -> ToolResult:
        name = (name or "").strip()
        if not name:
            return self._fail("An application name is required.")
        args = [str(a) for a in (arguments or [])][:8]
        backend = self.ctx.platform.apps

        # Already running?  Never start a second copy: single-instance apps
        # (Spotify, Discord, Steam, ...) react to a duplicate launch by flashing
        # a window and exiting, which the user sees as "opening and closing".
        window, process = self._find_running(name)
        if window is not None:
            self._show(window)
            return ToolResult(
                ok=True,
                message=f"{name} was already running; brought its window to the front.",
                data={"already_running": True, "window": _window_dict(window)},
                evidence={"running": True, "window_title": window.title,
                          "already_running": True})
        if process is not None:
            # Running without a visible window (minimised to the tray, or still
            # starting up).  Give it a few seconds to show one; do NOT relaunch.
            window = self._wait_for_window(name, None, 5.0)
            if window is not None:
                self._show(window)
            return ToolResult(
                ok=True,
                message=(f"{name} is already running (process '{process.name}', "
                         f"PID {process.pid})"
                         + (" and its window is now in front." if window else
                            " but has no visible window - it may sit in the system "
                            "tray. It was NOT started a second time.")),
                data={"already_running": True, "process": process.as_dict(),
                      "window": _window_dict(window) if window else None},
                evidence={"running": True, "already_running": True,
                          "process": process.name,
                          "window_title": window.title if window else ""})

        candidates = []
        try:
            candidates = backend.resolve(name, limit=5) or []
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"Application search failed: {exc}")

        if not candidates:
            installed = []
            try:
                installed = [c.name for c in backend.installed()][:40]
            except Exception:  # noqa: BLE001
                pass
            return ToolResult(
                ok=False,
                error=f"No installed application matches '{name}'.",
                data={"suggestions": installed},
                evidence={"running": False})

        best = candidates[0]
        t0 = time.time()
        result = backend.launch(best.target, args) or {}
        if not result.get("ok"):
            return ToolResult(
                ok=False,
                error=(f"Could not launch {best.name}: {result.get('error', 'unknown error')}"),
                data={"candidate": best.as_dict(),
                      "alternatives": [c.as_dict() for c in candidates[1:4]]},
                evidence={"running": False, "launch_error": result.get("error", "")})

        timeout = float(wait_s) if wait_s else float(self.settings.automation.app_launch_wait_s) + 8.0
        timeout = min(max(timeout, 1.0), float(self.settings.automation.max_wait_s))
        appeared = self._wait_for_window(name, best, timeout)
        elapsed = time.time() - t0
        data = {
            "launched": best.name, "target": best.target, "kind": best.kind,
            "source": best.source, "alternatives": [c.as_dict() for c in candidates[1:4]],
            "window": _window_dict(appeared) if appeared else None,
            "waited_s": round(elapsed, 2),
        }
        if appeared:
            return ToolResult(
                ok=True, message=f"{best.name} is open and its window is ready.",
                data=data,
                evidence={"running": True, "window_title": appeared.title,
                          "waited_s": round(elapsed, 2)})
        _w, proc = self._find_running(name)
        return ToolResult(
            ok=True,
            message=(f"{best.name} was started but no window appeared within "
                     f"{timeout:.0f}s. It may still be loading, or it may run "
                     "without a window."),
            data=data,
            evidence={"running": proc is not None, "process": proc.name if proc else "",
                      "window_title": ""},
            suggest_observe=True)

    # ------------------------------------------------------------------
    def _find_running(self, name: str) -> tuple:
        """Return (window, process) for an already-running app, either may be None.

        The process check matters: an app minimised to the system tray has no
        window, and launching "again" only makes single-instance apps flash.
        """
        low = (name or "").lower().strip()
        if not low:
            return None, None
        window = None
        try:
            windows = self.ctx.platform.windows.windows() or []
        except Exception as exc:  # noqa: BLE001
            log.debug("window enumeration failed: %s", exc)
            windows = []
        for w in windows:
            proc = (w.process_name or "").lower().replace(".exe", "")
            title = (w.title or "").lower()
            if low in proc or low in title:
                window = w
                break
        process = None
        try:
            for p in self.ctx.platform.windows.processes() or []:
                pname = (p.name or "").lower().replace(".exe", "")
                if pname and (low in pname or pname in low):
                    process = p
                    break
        except Exception as exc:  # noqa: BLE001
            log.debug("process enumeration failed: %s", exc)
        return window, process

    def _show(self, win: Any) -> None:
        try:
            if getattr(win, "is_minimised", False):
                self.ctx.platform.windows.restore(win)
            else:
                self.ctx.platform.windows.focus(win)
        except Exception as exc:  # noqa: BLE001
            log.debug("focus/restore failed: %s", exc)

    def _wait_for_window(self, name: str, best: Any, timeout: float) -> Optional[Any]:
        deadline = time.time() + timeout
        stem = ""
        if best is not None:
            stem = os.path.splitext(os.path.basename(str(best.target)))[0].lower()
        keys = sorted({k for k in [
            (name or "").lower(), (getattr(best, "name", "") or "").lower(), stem,
        ] if k and len(k) > 1})
        while time.time() < deadline:
            try:
                windows = self.ctx.platform.windows.windows() or []
            except Exception:  # noqa: BLE001
                windows = []
            for w in windows:
                proc = (w.process_name or "").lower().replace(".exe", "")
                title = (w.title or "").lower()
                if any(k in proc or k in title for k in keys):
                    # A minimised window still counts as "appeared" - the caller
                    # restores it.  Skipping it here made successful launches
                    # look failed and tempted the agent to launch again.
                    return w
            # Poll fast when adaptive waits are on: a window that appears in
            # 300 ms should not cost a fixed quarter-second each time.
            time.sleep(timing.poll_interval(self.ctx)
                       if timing.adaptive(self.ctx) else 0.25)
        return None

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        title = ev.get("window_title") or ""
        proc = ev.get("process") or ""
        ok = bool(ev.get("running")) and bool(title or proc or ev.get("already_running"))
        if ok and title:
            detail = f"Window '{title}' is present"
        elif ok:
            detail = (f"Process '{proc}' is running but shows no window yet "
                      "(background/tray or still starting)")
        else:
            detail = (f"No window and no matching process found "
                      f"(running={ev.get('running')})")
        return Verification(
            checked=True, succeeded=ok, detail=detail, evidence=ev)


class CloseApplicationTool(Tool):
    spec = ToolSpec(
        name="close_application",
        description=(
            "Close an application's window politely (WM_CLOSE, so the app can "
            "offer to save work). Never force-kills a process. Unsaved changes "
            "may prompt the user."),
        parameters=_WINDOW_PARAMS,
        category="application",
        risk=Risk.APPROVAL,
        reversible=False,
        requires=("window_control",),
    )

    def run(self, title: str = "", **_: Any) -> ToolResult:
        win = _find_window(self.ctx.platform, title)
        if win is None:
            return self._fail(f"No open window matches '{title}'.")
        before = win.title
        ok = self.ctx.platform.windows.close(win)
        timing.settle(self.ctx, base_s=0.4, reason="close-window")
        still = _find_window(self.ctx.platform, title)
        data = {"window": before, "closed": bool(ok and still is None),
                "still_open": still is not None}
        return ToolResult(ok=bool(ok), message=f"Close requested for '{before}'",
                          data=data,
                          evidence={"before": before,
                                    "gone": still is None, "requested": bool(ok)})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        gone = bool(ev.get("gone"))
        return Verification(checked=True, succeeded=gone,
                            detail=("Window is gone" if gone else
                                    "Window is still open - it may be showing a "
                                    "'save changes?' dialog"),
                            evidence=ev)


class FocusWindowTool(Tool):
    spec = ToolSpec(
        name="focus_window",
        description=(
            "Bring a window to the foreground and make it the active window "
            "(restores it first if minimised). Verified by re-reading which "
            "window is actually in front."),
        parameters=_WINDOW_PARAMS,
        category="navigation",
        risk=Risk.ALLOWED,
        reversible=True,
        requires=("window_control",),
        cost="low",
    )

    def run(self, title: str = "", **_: Any) -> ToolResult:
        win = _find_window(self.ctx.platform, title)
        if win is None:
            open_windows = []
            try:
                open_windows = [w.title for w in self.ctx.platform.windows.windows()][:15]
            except Exception:  # noqa: BLE001
                pass
            return ToolResult(ok=False,
                              error=f"No open window matches '{title}'.",
                              data={"open_windows": open_windows})
        ok = bool(self.ctx.platform.windows.focus(win))
        time.sleep(0.25)
        active = None
        try:
            active = self.ctx.platform.windows.active()
        except Exception:  # noqa: BLE001
            pass
        is_active = bool(active) and (
            (active.handle == win.handle) or (active.title == win.title))
        return ToolResult(
            ok=is_active or ok,
            message=(f"'{win.title}' is now in front" if is_active else
                     f"Asked Windows to focus '{win.title}'"),
            data={"window": _window_dict(win), "verified_active": is_active},
            evidence={"active": is_active, "title": win.title})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        return Verification(checked=True, succeeded=bool(ev.get("active")),
                            detail=(f"'{ev.get('title')}' confirmed in front"
                                    if ev.get("active") else
                                    "Window did not become foreground"),
                            evidence=ev)


class _WindowStateTool(Tool):
    action = "minimise"

    def run(self, title: str = "", **_: Any) -> ToolResult:
        win = _find_window(self.ctx.platform, title)
        if win is None:
            return self._fail(f"No open window matches '{title}'.")
        fn = getattr(self.ctx.platform.windows, self.method)
        ok = bool(fn(win))
        time.sleep(0.2)
        after = _find_window(self.ctx.platform, title)
        state_ok = False
        if after is not None:
            state_ok = bool(getattr(after, self.flag, False))
        return ToolResult(ok=ok, message=f"{self.action} '{win.title}'",
                          data={"window": _window_dict(win), "state": self.action},
                          evidence={"requested": ok, "state_ok": state_ok,
                                    "title": win.title})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        ok = bool(ev.get("state_ok"))
        return Verification(checked=True, succeeded=ok,
                            detail=(f"Window is {self.action}d" if ok else
                                    f"Window state did not change to {self.action}d"),
                            evidence=ev)


class MinimiseWindowTool(_WindowStateTool):
    action, method, flag = "minimise", "minimise", "is_minimised"
    spec = ToolSpec(name="minimise_window",
                    description="Minimise a window to the taskbar.",
                    parameters=_WINDOW_PARAMS, category="navigation",
                    risk=Risk.ALLOWED, reversible=True, requires=("window_control",),
                    cost="low")


class MaximiseWindowTool(_WindowStateTool):
    action, method, flag = "maximise", "maximise", "is_maximised"
    spec = ToolSpec(name="maximise_window",
                    description="Maximise a window (fill the screen). Use this for "
                                "'put it into full screen' when the application has "
                                "no full-screen control of its own.",
                    parameters=_WINDOW_PARAMS, category="navigation",
                    risk=Risk.ALLOWED, reversible=True, requires=("window_control",),
                    cost="low")


class RestoreWindowTool(_WindowStateTool):
    action, method, flag = "restore", "is_minimised", "is_minimised"
    spec = ToolSpec(name="restore_window",
                    description="Restore a minimised or maximised window to its "
                                "normal size and bring it to the front.",
                    parameters=_WINDOW_PARAMS, category="navigation",
                    risk=Risk.ALLOWED, reversible=True, requires=("window_control",),
                    cost="low")

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        ok = bool(ev.get("requested")) and not ev.get("state_ok")
        return Verification(checked=True, succeeded=ok,
                            detail=("Window restored (no longer minimised)" if ok
                                    else "Window is still minimised"),
                            evidence=ev)


class ListWindowsTool(Tool):
    spec = ToolSpec(
        name="list_windows",
        description="List every open window with its title, owning process, "
                    "position/size and whether it is minimised or in front. Use "
                    "it to check whether an application is already open before "
                    "launching it again.",
        parameters={"type": "object",
                    "properties": {"filter": {"type": "string",
                                              "description": "Optional text the "
                                                             "title or process "
                                                             "must contain."}}},
        category="read_only", risk=Risk.ALLOWED, reversible=True,
        requires=("window_control",), cost="low")

    def run(self, filter: str = "", **_: Any) -> ToolResult:
        try:
            windows = self.ctx.platform.windows.windows() or []
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"Could not enumerate windows: {exc}")
        if filter:
            f = filter.lower()
            windows = [w for w in windows
                       if f in (w.title or "").lower() or f in (w.process_name or "").lower()]
        data = {"count": len(windows),
                "windows": [_window_dict(w) for w in windows[:40]]}
        return ToolResult(ok=True, message=f"{len(windows)} window(s) open", data=data,
                          evidence={"count": len(windows),
                                    "titles": [w.title for w in windows[:10]]})


class GetActiveWindowTool(Tool):
    spec = ToolSpec(
        name="get_active_window",
        description="Report which window is currently in the foreground (title, "
                    "process, position, size).",
        parameters={"type": "object", "properties": {}},
        category="read_only", risk=Risk.ALLOWED, reversible=True,
        requires=("window_control",), cost="low")

    def run(self, **_: Any) -> ToolResult:
        try:
            win = self.ctx.platform.windows.active()
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"Could not read the foreground window: {exc}")
        if win is None:
            return ToolResult(ok=True, message="No window is in the foreground "
                                               "(the desktop is active).",
                              data={"active": None})
        return ToolResult(ok=True, message=f"Foreground: {win.title}",
                          data={"active": _window_dict(win)},
                          evidence={"title": win.title, "process": win.process_name})


class IsApplicationRunningTool(Tool):
    spec = ToolSpec(
        name="is_application_running",
        description="Check whether an application is currently running, either as "
                    "a process or as an open window. Always call this before "
                    "launching something, so you focus an existing window instead "
                    "of starting a second copy.",
        parameters={"type": "object",
                    "properties": {"name": {"type": "string",
                                            "description": "Application or process "
                                                           "name."}},
                    "required": ["name"]},
        category="read_only", risk=Risk.ALLOWED, reversible=True,
        requires=("window_control",), cost="low")

    def run(self, name: str = "", **_: Any) -> ToolResult:
        name = (name or "").strip()
        if not name:
            return self._fail("An application name is required.")
        low = name.lower()
        procs: List[Any] = []
        try:
            procs = self.ctx.platform.windows.process_running(name) or []
        except Exception:  # noqa: BLE001
            try:
                procs = [p for p in (self.ctx.platform.windows.processes() or [])
                         if low in (p.name or "").lower()]
            except Exception:  # noqa: BLE001
                procs = []
        win = _find_window(self.ctx.platform, name)
        data = {"name": name,
                "running": bool(procs or win),
                "processes": [p.as_dict() for p in procs[:6]],
                "window": _window_dict(win) if win else None}
        return ToolResult(ok=True,
                          message=(f"{name} is running" if data["running"]
                                   else f"{name} is not running"),
                          data=data, evidence={"running": data["running"],
                                               "pid": procs[0].pid if procs else 0,
                                               "window": bool(win)})


class ListProcessesTool(Tool):
    spec = ToolSpec(
        name="list_running_processes",
        description="List running processes, biggest memory users first. Optional "
                    "name filter. JARVIS can read this list but will never kill a "
                    "process or uninstall software.",
        parameters={"type": "object",
                    "properties": {"filter": {"type": "string"},
                                   "limit": {"type": "integer"}}},
        category="read_only", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, filter: str = "", limit: int = 25, **_: Any) -> ToolResult:
        try:
            procs = self.ctx.platform.windows.processes() or []
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"Could not list processes: {exc}")
        if filter:
            f = filter.lower()
            procs = [p for p in procs if f in (p.name or "").lower()
                     or f in (p.executable or "").lower()]
        procs = procs[:max(1, int(limit))]
        return ToolResult(ok=True, message=f"{len(procs)} process(es)",
                          data={"processes": [p.as_dict() for p in procs]},
                          evidence={"count": len(procs)})


class ListInstalledApplicationsTool(Tool):
    spec = ToolSpec(
        name="list_installed_applications",
        description="List applications JARVIS can launch on this computer (Start "
                    "Menu entries, Store apps, registry App Paths and PATH). Use "
                    "it when the user names something unfamiliar - do not guess "
                    "an executable path.",
        parameters={"type": "object",
                    "properties": {"query": {"type": "string",
                                             "description": "Optional filter."},
                                   "limit": {"type": "integer"}}},
        category="read_only", risk=Risk.ALLOWED, reversible=True,
        requires=("applications",), cost="low")

    def run(self, query: str = "", limit: int = 30, **_: Any) -> ToolResult:
        try:
            if query:
                apps = self.ctx.platform.apps.resolve(query, limit=max(1, int(limit)))
            else:
                apps = self.ctx.platform.apps.installed()[:max(1, int(limit))]
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"Application search failed: {exc}")
        data = {"count": len(apps),
                "applications": [a.as_dict() for a in apps[:max(1, int(limit))]]}
        return ToolResult(ok=True, message=f"{len(apps)} application(s) found",
                          data=data, evidence={"count": len(apps)})


class OpenPathTool(Tool):
    spec = ToolSpec(
        name="open_path",
        description=(
            "Open a file, a folder or a web address with the application Windows "
            "associates with it. Use this for 'open my Downloads folder', 'open "
            "the report on my desktop' or 'open youtube.com'. Reading files is "
            "always allowed; this only opens them in their normal application."),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Absolute path to a file/folder, or a URL."},
            },
            "required": ["path"],
        },
        category="navigation", risk=Risk.ALLOWED, reversible=True,
        requires=("applications",))

    def run(self, path: str = "", **_: Any) -> ToolResult:
        path = (path or "").strip()
        if not path:
            return self._fail("A path or URL is required.")
        result = self.ctx.platform.apps.launch(path) or {}
        if not result.get("ok"):
            return ToolResult(ok=False, error=result.get("error", "could not open"),
                              data={"path": path})
        return ToolResult(ok=True, message=f"Opened {path}", data=result,
                          evidence={"opened": True, "kind": result.get("kind", "")},
                          suggest_observe=True)

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        return Verification(checked=bool(ev.get("opened")),
                            succeeded=bool(ev.get("opened")),
                            detail=("Opened via the shell" if ev.get("opened")
                                    else "No confirmation available"),
                            evidence=ev)


class WaitForTool(Tool):
    spec = ToolSpec(
        name="wait_for",
        description=(
            "Wait until a condition becomes true, then report. Conditions: a "
            "window with a given name appears/disappears, specific text appears "
            "on screen, or a plain number of seconds elapses. Always prefer this "
            "over guessing how long an application needs to load."),
        parameters={
            "type": "object",
            "properties": {
                "condition": {"type": "string",
                              "enum": ["window_appears", "window_disappears",
                                       "text_on_screen", "seconds"],
                              "description": "What to wait for."},
                "value": {"type": "string",
                          "description": "Window/application name, or the text to "
                                         "look for. Ignored for 'seconds'."},
                "timeout_s": {"type": "number",
                              "description": "Maximum seconds to wait (default 15, "
                                             "hard limit from Automation settings)."},
                "poll_s": {"type": "number", "description": "Poll interval (default 0.5)."},
            },
            "required": ["condition"],
        },
        category="navigation", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, condition: str = "seconds", value: str = "", timeout_s: float = 15.0,
            poll_s: float = 0.5, **_: Any) -> ToolResult:
        max_wait = float(self.settings.automation.max_wait_s)
        timeout = min(max(0.1, float(timeout_s or 15.0)), max_wait)
        poll = min(max(0.1, float(poll_s or 0.5)), 3.0)
        t0 = time.time()
        condition = (condition or "seconds").lower()
        satisfied = False
        detail = ""

        if condition == "seconds":
            time.sleep(timeout)
            satisfied = True
            detail = f"waited {timeout:.1f}s"
        elif condition in ("window_appears", "window_disappears"):
            want = bool(condition == "window_appears")
            while time.time() - t0 < timeout:
                win = _find_window(self.ctx.platform, value)
                if (win is not None) == want:
                    satisfied = True
                    detail = (f"window '{win.title}' {'appeared' if want else 'is present'}"
                              if win else f"window '{value}' disappeared")
                    break
                time.sleep(poll)
            if not satisfied:
                detail = (f"no window matching '{value}' appeared within {timeout:.0f}s"
                          if want else f"window '{value}' was still open after {timeout:.0f}s")
        elif condition == "text_on_screen":
            needle = (value or "").strip().lower()
            while time.time() - t0 < timeout:
                try:
                    obs = self.ctx.vision.observe(include_image=False,
                                                  include_elements=True,
                                                  reason="wait_for:text")
                    hay = (obs.text() or "").lower()
                    if needle and needle in hay:
                        satisfied = True
                        detail = f"found '{value}' on screen"
                        break
                    if obs.signals.get("errors"):
                        detail = "an error indicator appeared: " + ", ".join(
                            obs.signals["errors"][:3])
                        break
                except Exception as exc:  # noqa: BLE001
                    detail = f"observation failed: {exc}"
                    break
                time.sleep(poll)
            if not satisfied and not detail:
                detail = f"'{value}' did not appear within {timeout:.0f}s"
        else:
            return self._fail(f"Unknown condition '{condition}'.")

        elapsed = time.time() - t0
        return ToolResult(ok=satisfied, message=detail,
                          data={"condition": condition, "value": value,
                                "satisfied": satisfied, "waited_s": round(elapsed, 2)},
                          evidence={"satisfied": satisfied, "waited_s": round(elapsed, 2)})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        return Verification(checked=True, succeeded=bool(ev.get("satisfied")),
                            detail=result.message, evidence=ev)


class SwitchApplicationTool(Tool):
    spec = ToolSpec(
        name="switch_application",
        description="Switch to another already-open application, either by name "
                    "or by pressing Alt+Tab a given number of times. Verified by "
                    "checking which window ends up in front.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Application or window name to switch to."},
                "alt_tab_times": {"type": "integer",
                                  "description": "Instead of a name: press Alt+Tab "
                                                 "this many times (1-9)."},
            },
        },
        category="navigation", risk=Risk.ALLOWED, reversible=True,
        requires=("window_control", "input"), cost="low")

    def run(self, name: str = "", alt_tab_times: int = 0, **_: Any) -> ToolResult:
        if name:
            win = _find_window(self.ctx.platform, name)
            if win is None:
                return self._fail(f"'{name}' does not have an open window.")
            self.ctx.platform.windows.focus(win)
            time.sleep(0.3)
            active = self.ctx.platform.windows.active()
            ok = bool(active) and (active.handle == win.handle or active.title == win.title)
            return ToolResult(ok=ok, message=f"Switched to {win.title}",
                              data={"window": _window_dict(win)},
                              evidence={"active": ok, "title": win.title})
        times = max(1, min(int(alt_tab_times or 1), 9))
        for _ in range(times):
            self.ctx.platform.input.press(["alt", "tab"])
            time.sleep(0.18)
        active = self.ctx.platform.windows.active()
        return ToolResult(ok=bool(active),
                          message=f"Alt+Tab x{times} -> {active.title if active else '?'}",
                          data={"window": _window_dict(active) if active else None},
                          evidence={"active": bool(active),
                                    "title": active.title if active else ""})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        return Verification(checked=True, succeeded=bool(ev.get("active")),
                            detail=(f"'{ev.get('title')}' is now in front"
                                    if ev.get("active") else "Foreground did not change"),
                            evidence=ev)


def build(ctx: ToolContext) -> List[Tool]:
    return [
        LaunchApplicationTool(ctx), CloseApplicationTool(ctx), FocusWindowTool(ctx),
        MinimiseWindowTool(ctx), MaximiseWindowTool(ctx), RestoreWindowTool(ctx),
        ListWindowsTool(ctx), GetActiveWindowTool(ctx),
        IsApplicationRunningTool(ctx), ListProcessesTool(ctx),
        ListInstalledApplicationsTool(ctx), OpenPathTool(ctx), WaitForTool(ctx),
        SwitchApplicationTool(ctx),
    ]
