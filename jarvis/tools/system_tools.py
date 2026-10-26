"""System-level tools: status, Windows settings, audio, media keys, clipboard
and the single, tightly-gated shell escape hatch."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess

from .. import _proc
import time
from typing import Any, Dict, List, Optional, Sequence

from ..security.policy import Risk
from ..security.redaction import contains_secret, redact_text
from .base import Tool, ToolContext, ToolResult, ToolSpec, Verification

log = logging.getLogger(__name__)

SETTINGS_URIS: Dict[str, str] = {
    "home": "ms-settings:",
    "display": "ms-settings:display",
    "brightness": "ms-settings:display",
    "night light": "ms-settings:nightlight",
    "nightlight": "ms-settings:nightlight",
    "sound": "ms-settings:sound",
    "audio": "ms-settings:sound",
    "volume": "ms-settings:sound",
    "bluetooth": "ms-settings:bluetooth",
    "devices": "ms-settings:bluetooth",
    "network": "ms-settings:network",
    "wifi": "ms-settings:network-wifi",
    "personalisation": "ms-settings:personalization",
    "personalization": "ms-settings:personalization",
    "colours": "ms-settings:personalization-colors",
    "colors": "ms-settings:personalization-colors",
    "dark mode": "ms-settings:personalization-colors",
    "theme": "ms-settings:personalization-colors",
    "background": "ms-settings:personalization-background",
    "wallpaper": "ms-settings:personalization-background",
    "lock screen": "ms-settings:lockscreen",
    "taskbar": "ms-settings:taskbar",
    "start menu": "ms-settings:start",
    "notifications": "ms-settings:notifications",
    "focus": "ms-settings:quiethours",
    "do not disturb": "ms-settings:quiethours",
    "power": "ms-settings:powersleep",
    "battery": "ms-settings:batterysaver",
    "storage": "ms-settings:storagesense",
    "multitasking": "ms-settings:multitasking",
    "typing": "ms-settings:typing",
    "privacy": "ms-settings:privacy",
    "microphone": "ms-settings:privacy-microphone",
    "camera": "ms-settings:privacy-webcam",
    "speech": "ms-settings:privacy-speech",
    "accessibility": "ms-settings:easeofaccess",
    "windows update": "ms-settings:windowsupdate",
    "about": "ms-settings:about",
    "system": "ms-settings:about",
    "apps": "ms-settings:appsfeatures",
    "default apps": "ms-settings:defaultapps",
    "date and time": "ms-settings:dateandtime",
    "region": "ms-settings:regionlanguage",
    "language": "ms-settings:regionlanguage",
    "gaming": "ms-settings:gaming-gamemode",
    "game mode": "ms-settings:gaming-gamemode",
    "startup": "ms-settings:startupapps",
    "optional features": "ms-settings:optionalfeatures",
}


class GetSystemStatusTool(Tool):
    spec = ToolSpec(
        name="get_system_status",
        description="Report this computer's current state: CPU, RAM (including "
                    "JARVIS's own usage against its 12 GB ceiling), GPU and VRAM "
                    "(against the 8 GB ceiling), the active Ollama model, whether "
                    "Ollama is reachable, disk space, and which JARVIS subsystems "
                    "(input, capture, OCR, UI Automation, speech, TTS) are "
                    "actually available on this machine.",
        parameters={"type": "object", "properties": {}},
        category="read_only", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, **_: Any) -> ToolResult:
        data: Dict[str, Any] = {}
        guard = self.ctx.guard
        if guard is not None:
            snap = guard.sample()
            data["resources"] = snap.as_dict()
            data["pressure"] = snap.pressure.value
        else:
            data["resources"] = {}
            data["pressure"] = "unknown"
        caps = self.ctx.platform.capabilities()
        data["capabilities"] = caps
        if self.ctx.ollama is not None:
            try:
                data["ollama"] = self.ctx.ollama.status()
            except Exception as exc:  # noqa: BLE001
                data["ollama"] = {"connected": False, "error": str(exc)}
        if self.ctx.router is not None:
            try:
                data["models"] = self.ctx.router.describe()["roles"]
            except Exception:  # noqa: BLE001
                data["models"] = {}
        try:
            import shutil as _sh

            usage = _sh.disk_usage(os.path.expanduser("~"))
            data["disk"] = {"total_gb": round(usage.total / 1024**3, 1),
                            "free_gb": round(usage.free / 1024**3, 1),
                            "percent_used": round(usage.percent, 1)}
        except Exception:  # noqa: BLE001
            data["disk"] = {}
        if self.ctx.speech is not None:
            try:
                data["speech"] = self.ctx.speech.status()
            except Exception:  # noqa: BLE001
                data["speech"] = {}
        missing = [i["tool"] for i in _unavailable(self.ctx)]
        data["unavailable_tools"] = missing[:20]
        summary = (f"RAM {data['resources'].get('process_ram_gb', '?')} GB of "
                   f"{data['resources'].get('ram_budget_gb', '?')} GB budget; "
                   f"pressure {data.get('pressure')}")
        return ToolResult(ok=True, message=summary, data=data,
                          evidence={"pressure": data.get("pressure"),
                                    "ollama_connected":
                                        bool((data.get("ollama") or {}).get("connected"))})


def _unavailable(ctx: ToolContext) -> List[Dict[str, str]]:
    try:
        return ctx.integrations.get("registry").unavailable_report() \
            if isinstance(ctx.integrations.get("registry"), object) else []
    except Exception:  # noqa: BLE001
        return []


class OpenWindowsSettingsTool(Tool):
    spec = ToolSpec(
        name="open_windows_settings",
        description="Open a Windows Settings page directly by name ('display', "
                    "'sound', 'bluetooth', 'personalisation', 'dark mode', "
                    "'windows update', 'microphone', 'power'). Uses the official "
                    "ms-settings: URIs, which is faster and safer than clicking "
                    "through the UI. JARVIS never changes administrator or "
                    "security settings.",
        parameters={
            "type": "object",
            "properties": {"page": {"type": "string",
                                    "description": "Settings page name."}},
        },
        category="system", risk=Risk.ALLOWED, reversible=True,
        requires=("applications",), cost="low")

    def run(self, page: str = "", **_: Any) -> ToolResult:
        key = (page or "").strip().lower()
        uri = SETTINGS_URIS.get(key)
        if uri is None:
            best = [k for k in SETTINGS_URIS if key and (key in k or k in key)]
            if best:
                key = best[0]
                uri = SETTINGS_URIS[key]
        if uri is None:
            return ToolResult(
                ok=False,
                error=f"No known Settings page for '{page}'.",
                data={"known_pages": sorted(SETTINGS_URIS)})
        if os.name != "nt" and not self.ctx.platform.is_simulated:
            return ToolResult(
                ok=False,
                error="ms-settings: URIs only exist on Windows. Tell the user "
                      "which setting to change, or use set_dark_mode / set_volume "
                      "which have non-Windows paths of their own.")
        result = self.ctx.platform.apps.launch(uri) or {}
        time.sleep(1.6)
        win = None
        try:
            for w in self.ctx.platform.windows.windows() or []:
                if "settings" in (w.process_name or "").lower() or \
                        "settings" in (w.title or "").lower():
                    win = w
                    break
        except Exception:  # noqa: BLE001
            pass
        return ToolResult(ok=bool(result.get("ok")),
                          message=f"Opened Settings → {key}",
                          data={"page": key, "uri": uri, "launch": result,
                                "window": win.as_dict() if win else None},
                          evidence={"opened": bool(win), "uri": uri},
                          suggest_observe=True)

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        return Verification(checked=True, succeeded=bool(ev.get("opened")),
                            detail=("Settings window is open" if ev.get("opened")
                                    else "No Settings window was detected"),
                            evidence=ev)


class SetDarkModeTool(Tool):
    spec = ToolSpec(
        name="set_dark_mode",
        description=(
            "Turn Windows dark mode on or off. JARVIS opens the official "
            "Personalisation > Colours page and toggles the 'Choose your mode' / "
            "'Dark mode' control through UI Automation - it never edits the "
            "registry, because registry modification is prohibited. The toggle "
            "state is read back to verify the change."),
        parameters={
            "type": "object",
            "properties": {
                "on": {"type": "boolean", "description": "true = dark, false = light."},
            },
            "required": ["on"],
        },
        category="system", risk=Risk.ALLOWED, reversible=True,
        requires=("applications",), cost="medium")

    def run(self, on: bool = True, **_: Any) -> ToolResult:
        if os.name != "nt" and not self.ctx.platform.is_simulated:
            return self._try_generic(on)
        opened = self.ctx.platform.apps.launch("ms-settings:personalization-colors") \
            if os.name == "nt" else {"ok": True}
        time.sleep(2.0)
        state_before = self._read_mode()
        toggled = False
        routes: List[str] = []
        for phrase in (("choose your mode", "dark mode", "app mode",
                        "windows mode", "mode")):
            if self.ctx.vision is None:
                break
            found = self.ctx.vision.find_element(phrase, fresh=True)
            matches = found.get("matches") or []
            for m in matches[:2]:
                el = m.element
                if el.is_password or el.rect.width <= 0:
                    continue
                want = "dark" if on else "light"
                if want in (el.value or "").lower() or want in (el.name or "").lower():
                    routes.append(f"already-{want}")
                    toggled = True
                    break
                try:
                    self.ctx.platform.uia.invoke(el)
                    toggled = True
                    routes.append(f"uia-invoke:{el.name}")
                    time.sleep(0.5)
                    if want in (self._read_mode() or "").lower():
                        break
                    # ComboBox style: after expanding, pick the item.
                    item = self.ctx.vision.find_element(want, fresh=True)
                    for im in (item.get("matches") or [])[:1]:
                        self.ctx.platform.uia.invoke(im.element)
                        routes.append(f"uia-invoke:{im.element.name}")
                        time.sleep(0.4)
                except Exception as exc:  # noqa: BLE001
                    routes.append(f"failed:{exc}")
            if toggled:
                break
        state_after = self._read_mode()
        verified = bool(state_after) and (("dark" in state_after.lower()) == bool(on))
        return ToolResult(
            ok=bool(toggled or verified),
            message=(f"Dark mode is {'on' if on else 'off'}" if verified else
                     f"Attempted to set dark mode {'on' if on else 'off'}; "
                     f"could not verify (state reads '{state_after}')"),
            data={"requested": bool(on), "before": state_before, "after": state_after,
                  "routes": routes, "opened_settings": bool(opened.get("ok")),
                  "verified": verified},
            evidence={"verified": verified, "state": state_after},
            suggest_observe=not verified)

    def _read_mode(self) -> str:
        """Read the current mode from the visible control, never the registry."""
        if self.ctx.vision is None:
            return ""
        try:
            obs = self.ctx.vision.observe(include_image=False, reason="dark-mode-state")
        except Exception:  # noqa: BLE001
            return ""
        for el in obs.elements:
            name = (el.name or "").lower()
            if "choose your mode" in name or "app mode" in name or name == "mode":
                return (el.value or "").strip()
        text = obs.text().lower()
        if "dark" in text and "light" in text:
            return "dark" if text.find("dark") < text.find("light") else "light"
        return ""

    def _try_generic(self, on: bool) -> ToolResult:
        """Non-Windows hosts: use the desktop's own mechanism when present."""
        attempts = []
        if os.environ.get("DESKTOP_SESSION", "").lower().startswith("kde") or \
                os.path.exists("/usr/bin/kwriteconfig5"):
            attempts.append((["kwriteconfig5", "--file", "kdeglobals", "--group",
                              "General", "--key", "ColorScheme",
                              "BreezeDark" if on else "BreezeLight"], "kde"))
        if os.path.exists("/usr/bin/gsettings"):
            scheme = "prefer-dark" if on else "default"
            attempts.append((["gsettings", "set", "org.gnome.desktop.interface",
                              "color-scheme", scheme], "gnome"))
        for cmd, label in attempts:
            try:
                out = _proc.run(cmd, capture_output=True, text=True, timeout=8)
                if out.returncode == 0:
                    return ToolResult(ok=True, message=f"Dark mode {'on' if on else 'off'} via {label}",
                                      data={"route": label},
                                      evidence={"verified": False, "state": label})
            except Exception as exc:  # noqa: BLE001
                log.debug("%s dark mode attempt failed: %s", label, exc)
        return ToolResult(
            ok=False,
            error="Dark mode could not be changed on this platform. On Windows I "
                  "drive the Personalisation > Colours page; here there is no "
                  "supported mechanism installed.")

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        ok = bool(ev.get("verified"))
        return Verification(checked=True, succeeded=ok,
                            detail=(f"Mode now reads '{ev.get('state')}'" if ok else
                                    "Mode could not be confirmed"),
                            evidence=ev)


class SetVolumeTool(Tool):
    spec = ToolSpec(
        name="set_volume",
        description="Set or change the system master volume (0-100) or mute it. "
                    "Uses the Windows Core Audio API when available so the level "
                    "can be read back and verified; otherwise falls back to media "
                    "keys and says so.",
        parameters={
            "type": "object",
            "properties": {
                "percent": {"type": "integer", "description": "Absolute level 0-100."},
                "change": {"type": "integer",
                           "description": "Relative change, e.g. +10 or -20."},
                "mute": {"type": "boolean", "description": "true to mute, false to unmute."},
            },
        },
        category="system", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, percent: Optional[int] = None, change: Optional[int] = None,
            mute: Optional[bool] = None, **_: Any) -> ToolResult:
        audio = self.ctx.integrations.get("audio")
        route = "windows-core-audio" if audio is not None else "media-keys"
        if mute is not None and audio is not None:
            got = audio.set_mute(bool(mute))
            if got is not None:
                return ToolResult(ok=True, message=f"Mute {'on' if got else 'off'}",
                                  data={"muted": got, "route": route},
                                  evidence={"muted": got, "expected": bool(mute)})
        if percent is not None and audio is not None:
            got = audio.set_volume(float(percent))
            if got is not None:
                return ToolResult(ok=True, message=f"Volume set to {got}%",
                                  data={"volume_percent": got, "route": route},
                                  evidence={"volume": got, "expected": float(percent)})
        if change is not None and audio is not None:
            got = audio.step(up=change > 0, steps=abs(int(change)) // 2 or 1)
            if got is not None:
                return ToolResult(ok=True, message=f"Volume now {got}%",
                                  data={"volume_percent": got, "route": route},
                                  evidence={"volume": got})
        # media-key fallback
        if mute is not None:
            self.ctx.platform.input.press(["volumemute"])
            return ToolResult(ok=True, message=f"Mute toggled ({'on' if mute else 'off'} requested)",
                              data={"route": "media-keys"},
                              evidence={"requested": bool(mute)},
                              suggest_observe=True)
        steps = 0
        key = "volumeup"
        if percent is not None:
            current = 50
            steps = min(30, abs(int(percent) - current) // 2 or 1)
            key = "volumeup" if int(percent) >= current else "volumedown"
        elif change is not None:
            steps = min(30, abs(int(change)) // 2 or 1)
            key = "volumeup" if int(change) > 0 else "volumedown"
        for _ in range(max(1, steps)):
            self.ctx.platform.input.press([key])
            time.sleep(0.03)
        return ToolResult(ok=True,
                          message=f"Volume adjusted with media keys ({steps} step(s))",
                          data={"route": "media-keys", "steps": steps, "key": key},
                          evidence={"steps": steps},
                          suggest_observe=True)

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        if "volume" in ev and "expected" in ev:
            ok = abs(float(ev["volume"]) - float(ev["expected"])) <= 2.0
            return Verification(checked=True, succeeded=ok,
                                detail=f"Volume reads {ev['volume']}%", evidence=ev)
        if "muted" in ev and "expected" in ev:
            ok = bool(ev["muted"]) == bool(ev["expected"])
            return Verification(checked=True, succeeded=ok,
                                detail=f"Mute state reads {ev['muted']}", evidence=ev)
        return Verification(checked=False, succeeded=True,
                            detail="Volume keys were sent; the level is not readable "
                                   "through that path.", evidence=ev)


class MediaControlTool(Tool):
    spec = ToolSpec(
        name="media_control",
        description="Control whatever is currently playing - play, pause, "
                    "play/pause toggle, next, previous, stop. Uses the Windows "
                    "media session API first (works even when the player is "
                    "minimised) and falls back to hardware media keys. Reports "
                    "what is playing afterwards so you can verify it worked.",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["play", "pause", "toggle", "next", "previous",
                                    "stop"]},
                "app": {"type": "string",
                        "description": "Optional player name, e.g. 'spotify'."},
            },
            "required": ["action"],
        },
        category="media", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, action: str = "toggle", app: str = "", **_: Any) -> ToolResult:
        action = (action or "toggle").lower()
        smtc = self.ctx.integrations.get("smtc")
        spotify = self.ctx.integrations.get("spotify")
        route = ""
        ok = False
        before = None
        if smtc is not None:
            try:
                before = smtc.now_playing(app)
            except Exception:  # noqa: BLE001
                before = None
        target = spotify if (app and "spotify" in app.lower() and spotify) else None
        if action == "play":
            ok = bool(target.resume()) if target else bool(
                smtc.play(app) if smtc else False)
            route = "integration" if target else "smtc"
        elif action == "pause":
            ok = bool(target.pause()) if target else bool(
                smtc.pause(app) if smtc else False)
            route = "integration" if target else "smtc"
        elif action == "next":
            ok = bool(target.skip("next")) if target else bool(
                smtc.next(app) if smtc else False)
            route = "integration" if target else "smtc"
        elif action == "previous":
            ok = bool(target.skip("previous")) if target else bool(
                smtc.previous(app) if smtc else False)
            route = "integration" if target else "smtc"
        elif action == "stop":
            ok = bool(smtc.stop(app)) if smtc else False
            route = "smtc"
        else:
            ok = bool(target.toggle()) if target else bool(
                smtc.toggle(app) if smtc else False)
            route = "integration" if target else "smtc"
        if not ok:
            key = {"play": "playpause", "pause": "playpause", "toggle": "playpause",
                   "next": "nexttrack", "previous": "prevtrack", "stop": "stop"}.get(
                       action, "playpause")
            self.ctx.platform.input.press([key])
            route = "media-keys"
            ok = True
        time.sleep(0.8)
        after = None
        if smtc is not None:
            try:
                after = smtc.now_playing(app)
            except Exception:  # noqa: BLE001
                after = None
        data = {"action": action, "route": route,
                "before": before.as_dict() if before else None,
                "after": after.as_dict() if after else None}
        label = after.label() if after else (before.label() if before else "")
        return ToolResult(ok=True, message=f"{action.title()} ({route})"
                          + (f" - {label}" if label else ""),
                          data=data,
                          evidence={"route": route,
                                    "status_after": after.status if after else "",
                                    "status_before": before.status if before else "",
                                    "title_after": after.title if after else ""})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        action = (args.get("action") or "").lower()
        after = ev.get("status_after") or ""
        before = ev.get("status_before") or ""
        if not after:
            return Verification(checked=False, succeeded=True,
                                detail=f"{action} sent via {ev.get('route')}; the media "
                                       "session API is not reporting state.",
                                evidence=ev)
        expected = {"play": "playing", "pause": "paused", "stop": "stopped"}.get(action)
        if expected is None:
            ok = after != before or bool(ev.get("title_after"))
            return Verification(checked=True, succeeded=ok,
                                detail=f"Track is now '{ev.get('title_after')}' "
                                       f"(status {after})", evidence=ev)
        ok = after == expected
        return Verification(checked=True, succeeded=ok,
                            detail=f"Playback status is '{after}'"
                                   + ("" if ok else f", expected '{expected}'"),
                            evidence=ev)


class GetClipboardTool(Tool):
    spec = ToolSpec(
        name="get_clipboard",
        description="Read the current clipboard text. If the clipboard looks like "
                    "it holds a password, token or card number, JARVIS refuses to "
                    "read it and says why - clipboard contents are never stored "
                    "in memory or logged.",
        parameters={"type": "object", "properties": {}},
        category="read_only", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, **_: Any) -> ToolResult:
        text = _read_clipboard(self.ctx)
        if text is None:
            return self._fail("The clipboard could not be read on this platform.")
        if contains_secret(text):
            return ToolResult(
                ok=False,
                error="The clipboard appears to contain a password, token or card "
                      "number. JARVIS does not read or store credentials.",
                data={"chars": len(text)})
        return ToolResult(ok=True, message=f"Clipboard holds {len(text)} character(s)",
                          data={"text": redact_text(text)[:4000], "chars": len(text)},
                          evidence={"chars": len(text)})


class SetClipboardTool(Tool):
    spec = ToolSpec(
        name="set_clipboard",
        description="Put text on the clipboard. Useful for pasting into "
                    "applications that do not accept synthetic keystrokes. Never "
                    "use it to stage a password.",
        parameters={"type": "object",
                    "properties": {"text": {"type": "string"}}, "required": ["text"]},
        category="input", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, text: str = "", **_: Any) -> ToolResult:
        if contains_secret(str(text)):
            return ToolResult(ok=False,
                              error="Refusing to place what looks like a credential "
                                    "on the clipboard.")
        ok = _write_clipboard(self.ctx, str(text))
        if not ok:
            return self._fail("Could not write to the clipboard on this platform.")
        readback = _read_clipboard(self.ctx)
        return ToolResult(ok=True, message=f"Copied {len(str(text))} characters",
                          data={"chars": len(str(text))},
                          evidence={"written": ok,
                                    "matches": (readback or "") == str(text)})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        ok = bool(ev.get("matches"))
        return Verification(checked=True, succeeded=ok,
                            detail=("Clipboard read-back matches" if ok
                                    else "Clipboard read-back did not match"),
                            evidence=ev)


def _read_clipboard(ctx: ToolContext) -> Optional[str]:
    if ctx.platform.is_simulated:
        return ctx.platform.simulated.clipboard
    try:
        if os.name == "nt":
            from ..platform.input_backends import _clipboard_get

            return _clipboard_get()
    except Exception as exc:  # noqa: BLE001
        log.debug("win32 clipboard read failed: %s", exc)
    for cmd in (("wl-paste", "--no-newline"), ("xclip", "-selection", "clipboard", "-o"),
                ("pbpaste",)):
        try:
            if not os.path.exists(os.path.dirname(cmd[0]) or "") and \
                    not _which(cmd[0]):
                continue
            out = _proc.run(list(cmd), capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                return out.stdout
        except Exception:  # noqa: BLE001
            continue
    return None


def _write_clipboard(ctx: ToolContext, text: str) -> bool:
    if ctx.platform.is_simulated:
        ctx.platform.simulated.clipboard = text
        return True
    try:
        if os.name == "nt":
            from ..platform.input_backends import _clipboard_set

            return bool(_clipboard_set(text))
    except Exception as exc:  # noqa: BLE001
        log.debug("win32 clipboard write failed: %s", exc)
    for cmd in (("wl-copy",), ("xclip", "-selection", "clipboard"), ("pbcopy",)):
        try:
            if not _which(cmd[0]):
                continue
            proc = _proc.run(list(cmd), input=text, text=True, timeout=5)
            if proc.returncode == 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _which(name: str) -> Optional[str]:
    import shutil

    return shutil.which(name)


class RunCommandTool(Tool):
    """The only free-form execution path - and it is never silent."""

    spec = ToolSpec(
        name="run_command",
        description=(
            "Run a single shell command as the logged-in user. THIS ALWAYS "
            "REQUIRES EXPLICIT USER APPROVAL and is refused outright if it "
            "matches a prohibited pattern (elevating privileges, uninstalling "
            "software, editing the registry, disabling security features, "
            "changing accounts, touching system files, downloading and piping to "
            "a shell, reading credentials). Prefer a specific tool: "
            "launch_application, open_path, search_files, media_control, "
            "spotify. Output is captured, truncated and redacted."),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string",
                            "description": "The command line to run."},
                "cwd": {"type": "string",
                        "description": "Working directory (must be inside your "
                                       "personal folders)."},
                "timeout_s": {"type": "number", "description": "Default 30, max 300."},
                "reason": {"type": "string",
                           "description": "One sentence explaining to the user why "
                                          "this command is needed."},
            },
            "required": ["command", "reason"],
        },
        category="shell", risk=Risk.APPROVAL, reversible=False)

    def run(self, command: str = "", cwd: str = "", timeout_s: float = 30.0,
            reason: str = "", **_: Any) -> ToolResult:
        command = (command or "").strip()
        if not command:
            return self._fail("No command given.")
        if not (reason or "").strip():
            return self._fail("A reason is required so the user can decide.")
        timeout = min(max(1.0, float(timeout_s or 30.0)), 300.0)

        workdir: Optional[str] = None
        if cwd:
            from .file_tools import inside_writable, is_protected, resolve_path

            p, err = resolve_path(cwd)
            if err:
                return self._fail(err)
            if is_protected(p):
                return ToolResult(ok=False, error=str(is_protected(p)))
            if not inside_writable(p, self.settings):
                return ToolResult(ok=False,
                                  error=f"{p} is outside the folders JARVIS may "
                                        "run commands in.")
            workdir = str(p)

        argv, shell = _build_argv(command)
        try:
            proc = _proc.run(
                argv, shell=shell, cwd=workdir, capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace",
                env=_clean_env())
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False,
                              error=f"Command timed out after {timeout:.0f}s and was killed.",
                              data={"command": command})
        except FileNotFoundError as exc:
            return ToolResult(ok=False, error=f"Command not found: {exc}",
                              data={"command": command})
        except OSError as exc:
            return ToolResult(ok=False, error=f"Command failed to start: {exc}",
                              data={"command": command})

        stdout = redact_text((proc.stdout or "")[:6000])
        stderr = redact_text((proc.stderr or "")[:3000])
        return ToolResult(
            ok=proc.returncode == 0,
            message=(f"Exit code {proc.returncode}"
                     + (f" - {stderr.splitlines()[0][:120]}"
                        if proc.returncode != 0 and stderr.strip() else "")),
            data={"command": command, "returncode": proc.returncode,
                  "stdout": stdout, "stderr": stderr, "cwd": workdir or "",
                  "reason": reason},
            evidence={"returncode": proc.returncode,
                      "stdout_chars": len(proc.stdout or ""),
                      "stderr_chars": len(proc.stderr or "")})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        rc = ev.get("returncode")
        ok = rc == 0
        return Verification(checked=rc is not None, succeeded=bool(ok),
                            detail=(f"Exit code {rc}" if rc is not None
                                    else "No exit code was captured"),
                            evidence=ev)


def _build_argv(command: str):
    """Never run through a shell unless the command genuinely needs one."""
    stripped = command.strip()
    lowered = stripped.lower()
    needs_shell = any(lowered.startswith(p) for p in ("cmd ", "cmd.exe", "cmd/",
                                                      "powershell", "pwsh"))
    if needs_shell:
        if os.name == "nt":
            if lowered.startswith(("powershell", "pwsh")):
                return stripped, True
            return stripped, True
        return ["/bin/sh", "-c", stripped], False
    try:
        argv = shlex.split(stripped, posix=(os.name != "nt"))
    except ValueError as exc:
        return stripped, True
    if not argv:
        return stripped, True
    if os.name == "nt":
        # On Windows, .bat/.cmd and internal commands need cmd.exe.
        exe = argv[0].lower()
        internals = {"dir", "copy", "move", "del", "type", "echo", "cd", "start",
                     "assoc", "ver", "where", "ren", "mklink", "cls", "set"}
        if exe in internals or exe.endswith((".bat", ".cmd")):
            return stripped, True
    return argv, False


def _clean_env() -> Dict[str, str]:
    env = dict(os.environ)
    # Remove anything that looks like a credential so a child process cannot
    # inherit and echo it.
    for key in list(env):
        if any(m in key.lower() for m in ("password", "secret", "token", "api_key",
                                          "apikey", "private_key", "credentials")):
            env.pop(key, None)
    env["JARVIS_CHILD"] = "1"
    env.pop("JARVIS_OLLAMA_HOST", None)
    return env


class GetCapabilitiesTool(Tool):
    spec = ToolSpec(
        name="get_capabilities",
        description="Report which of JARVIS's subsystems work on this computer "
                    "right now (input, screen capture, window control, UI "
                    "Automation, OCR, applications, speech-to-text, text-to-speech, "
                    "Ollama) and why anything is missing. Call this before "
                    "promising the user something, and never claim a capability "
                    "that is listed as unavailable.",
        parameters={"type": "object", "properties": {}},
        category="read_only", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, **_: Any) -> ToolResult:
        caps = self.ctx.platform.capabilities()
        data: Dict[str, Any] = {"platform": caps["items"],
                                "simulated": caps["simulated"]}
        if self.ctx.speech is not None:
            try:
                data["speech"] = self.ctx.speech.status()
            except Exception:  # noqa: BLE001
                data["speech"] = {}
        if self.ctx.ollama is not None:
            try:
                data["ollama"] = self.ctx.ollama.status()
            except Exception as exc:  # noqa: BLE001
                data["ollama"] = {"connected": False, "error": str(exc)}
        if self.ctx.memory is not None:
            try:
                data["memory"] = self.ctx.memory.stats()
            except Exception:  # noqa: BLE001
                data["memory"] = {}
        missing = [c["name"] for c in caps["items"] if not c["available"]
                   and c["name"] != "simulation"]
        return ToolResult(ok=True,
                          message=("All subsystems available" if not missing else
                                   f"Unavailable: {', '.join(missing)}"),
                          data=data, evidence={"missing": missing,
                                               "simulated": caps["simulated"]})


def build(ctx: ToolContext) -> List[Tool]:
    return [GetSystemStatusTool(ctx), GetCapabilitiesTool(ctx),
            OpenWindowsSettingsTool(ctx), SetDarkModeTool(ctx), SetVolumeTool(ctx),
            MediaControlTool(ctx), GetClipboardTool(ctx), SetClipboardTool(ctx),
            RunCommandTool(ctx)]
