"""Web browser integration.

JARVIS drives the browser the user already has installed - it never embeds a
browser engine and never uploads anything.  Two modes:

**Shell mode (default).**  Launch the default browser with a URL, then interact
with the visible page through UI Automation and OCR exactly like any other
application.  This works with every browser and needs no special flags.

**DevTools mode (opt-in).**  If the user enables it in Automation settings, the
browser is started with ``--remote-debugging-port`` and JARVIS can read the tab
list over ``http://127.0.0.1`` (localhost only - enforced by
:mod:`jarvis.security.netguard`).  This gives exact tab titles/URLs instead of
guessing from the window title.  It is off by default because it changes how
the browser starts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

SEARCH_ENGINES: Dict[str, str] = {
    "google": "https://www.google.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "ddg": "https://duckduckgo.com/?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "github": "https://github.com/search?q={q}",
    "images": "https://www.google.com/search?tbm=isch&q={q}",
    "news": "https://news.google.com/search?q={q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "stackoverflow": "https://stackoverflow.com/search?q={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
}

BROWSER_CANDIDATES = ("Chrome", "Edge", "Firefox", "Brave", "Opera", "Vivaldi",
                      "Arc", "Safari")


def build_url(query_or_url: str, engine: str = "google") -> str:
    text = (query_or_url or "").strip()
    if not text:
        return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", text):
        return text
    if re.match(r"^[\w\-]+(\.[\w\-]+)+([/?#].*)?$", text) and " " not in text:
        return "https://" + text
    if text.startswith(("about:", "chrome:", "edge:", "file:", "localhost")):
        return text
    template = SEARCH_ENGINES.get((engine or "google").lower(),
                                  SEARCH_ENGINES["google"])
    return template.format(q=urllib.parse.quote_plus(text))


class BrowserIntegration:
    name = "browser"

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._default: Optional[str] = None

    # ------------------------------------------------------------------
    def default_browser(self) -> str:
        if self._default:
            return self._default
        name = ""
        if os.name == "nt":
            name = self._default_from_registry()
        if not name:
            for candidate in BROWSER_CANDIDATES:
                try:
                    if self.ctx.platform.apps.resolve(candidate, limit=1):
                        name = candidate
                        break
                except Exception:  # noqa: BLE001
                    continue
        self._default = name or "browser"
        return self._default

    def _default_from_registry(self) -> str:
        """Read the user's default browser ProgId (read-only registry access)."""
        try:
            from ..platform.app_backends import (_HKEY_CURRENT_USER, _reg_close,
                                                 _reg_get_value, _reg_open)

            handle = _reg_open(
                _HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice")
            if handle is None:
                return ""
            try:
                progid = _reg_get_value(handle, "ProgId")
            finally:
                _reg_close(handle)
            mapping = {"chromehtml": "Chrome", "msedgehtml": "Edge",
                       "firefoxurl": "Firefox", "bravehtml": "Brave",
                       "operahtml": "Opera", "vivaldi": "Vivaldi",
                       "archtml": "Arc", "iesettings": "Edge"}
            for key, value in mapping.items():
                if progid.lower().startswith(key):
                    return value
            return progid.split(".")[0].capitalize() if progid else ""
        except Exception as exc:  # noqa: BLE001
            log.debug("default browser lookup failed: %s", exc)
            return ""

    # ------------------------------------------------------------------
    def windows(self) -> List[Any]:
        out = []
        try:
            for w in self.ctx.platform.windows.windows() or []:
                proc = (w.process_name or "").lower()
                if any(b.lower() in proc for b in
                       ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi",
                        "arc", "safari", "iexplore")):
                    out.append(w)
        except Exception:  # noqa: BLE001
            pass
        return out

    def active_window(self) -> Optional[Any]:
        wins = self.windows()
        if not wins:
            return None
        try:
            active = self.ctx.platform.windows.active()
        except Exception:  # noqa: BLE001
            active = None
        if active is not None:
            for w in wins:
                if w.handle == active.handle:
                    return w
        return wins[0]

    def is_running(self) -> bool:
        return bool(self.windows())

    # ------------------------------------------------------------------
    def open(self, target: str, engine: str = "google", browser: str = "",
             wait_s: float = 6.0) -> Dict[str, Any]:
        url = build_url(target, engine)
        if not url:
            return {"ok": False, "error": "Nothing to open."}
        name = browser or self.default_browser()
        candidates = []
        try:
            candidates = self.ctx.platform.apps.resolve(name, limit=3) or []
        except Exception:  # noqa: BLE001
            candidates = []

        before = {w.title for w in self.windows()}
        launched = None
        if candidates:
            best = candidates[0]
            args = [url]
            if self.cdp_enabled():
                args = [f"--remote-debugging-port={self.cdp_port()}",
                        "--restore-last-session", url]
            launched = self.ctx.platform.apps.launch(best.target, args) or {}
        if not launched or not launched.get("ok"):
            # Fall back to the shell association - always works.
            launched = self.ctx.platform.apps.launch(url) or {}
        if not launched.get("ok"):
            return {"ok": False, "error": launched.get("error", "browser launch failed"),
                    "url": url}

        deadline = time.time() + max(1.0, float(wait_s))
        win = None
        while time.time() < deadline:
            wins = self.windows()
            new = [w for w in wins if w.title not in before and w.title.strip()]
            if new:
                win = new[0]
                break
            if wins:
                win = wins[0]
                if self._looks_loaded(win):
                    break
            time.sleep(0.35)
        if win is None:
            return {"ok": True, "url": url, "browser": name,
                    "note": "The browser was started but no window was detected "
                            "yet; observe_screen will confirm."}
        return {"ok": True, "url": url, "browser": name,
                "window": win.as_dict(), "title": win.title,
                "loaded": self._looks_loaded(win)}

    def _looks_loaded(self, win: Any) -> bool:
        title = (win.title or "").lower()
        if not title:
            return False
        return not any(m in title for m in ("loading", "stop", "waiting", "new tab"))

    # ------------------------------------------------------------------
    def search(self, query: str, engine: str = "google") -> Dict[str, Any]:
        return self.open(query, engine=engine)

    def current_page(self) -> Dict[str, Any]:
        """Best-effort 'what page am I on' from the window title (+CDP if on)."""
        win = self.active_window()
        if win is None:
            return {"ok": False, "error": "No browser window is open."}
        title = win.title or ""
        cleaned = re.sub(r"\s*[-–]\s*(Google Chrome|Microsoft Edge|Mozilla Firefox|"
                         r"Brave|Opera|Safari).*$", "", title, flags=re.I).strip()
        out: Dict[str, Any] = {"ok": True, "window_title": title, "page_title": cleaned,
                               "browser": self.default_browser()}
        tabs = self.tabs()
        if tabs:
            out["tabs"] = tabs
            active_tab = next((t for t in tabs if t.get("active")), None)
            if active_tab:
                out["url"] = active_tab.get("url", "")
                out["page_title"] = active_tab.get("title", cleaned)
        return out

    # ------------------------------------------------------------------
    def cdp_enabled(self) -> bool:
        try:
            return bool(getattr(self.ctx.settings.automation, "browser_devtools", False))
        except Exception:  # noqa: BLE001
            return False

    def cdp_port(self) -> int:
        try:
            return int(getattr(self.ctx.settings.automation, "browser_devtools_port", 9222))
        except Exception:  # noqa: BLE001
            return 9222

    def tabs(self) -> List[Dict[str, Any]]:
        """Read open tabs via the local DevTools endpoint (opt-in)."""
        if not self.cdp_enabled():
            return []
        from ..security.netguard import assert_local_endpoint

        url = f"http://127.0.0.1:{self.cdp_port()}/json/list"
        try:
            assert_local_endpoint(url)
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            log.debug("DevTools tab list unavailable: %s", exc)
            return []
        out = []
        for entry in data if isinstance(data, list) else []:
            if entry.get("type") != "page":
                continue
            out.append({"title": entry.get("title", ""), "url": entry.get("url", ""),
                        "id": entry.get("id", ""), "active": False})
        if out:
            active_win = self.active_window()
            if active_win is not None:
                for t in out:
                    if t["title"] and t["title"] in (active_win.title or ""):
                        t["active"] = True
                        break
        return out

    # ------------------------------------------------------------------
    def set_fullscreen(self, on: bool = True) -> Dict[str, Any]:
        """F11 in the browser, verified by reading the window state back."""
        win = self.active_window()
        if win is None:
            started = self.open("about:blank", wait_s=4.0)
            if not started.get("ok"):
                return started
            win = self.active_window()
        if win is None:
            return {"ok": False, "error": "No browser window to toggle."}
        try:
            self.ctx.platform.windows.focus(win)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
        # Prefer the browser's own control when it exposes one.
        clicked = False
        if self.ctx.vision is not None:
            found = self.ctx.vision.find_element("full screen" if on else "exit full screen",
                                                 fresh=True)
            for m in (found.get("matches") or [])[:1]:
                if m.score >= 0.72:
                    c = m.element.center
                    self.ctx.platform.input.click(c.x, c.y, "left", 1)
                    clicked = True
        if not clicked:
            self.ctx.platform.input.press(["f11"])
        time.sleep(0.9)
        after = self.active_window()
        verified = False
        if after is not None:
            try:
                size = self.ctx.platform.screen.size()
                verified = bool(on) == (after.rect.width >= size[0] - 8
                                        and after.rect.height >= size[1] - 8)
            except Exception:  # noqa: BLE001
                verified = bool(after.is_maximised) == bool(on)
        return {"ok": True, "requested": bool(on), "via": "control" if clicked else "F11",
                "window": after.as_dict() if after else None, "verified": verified}

    def go_back(self) -> bool:
        self.ctx.platform.input.press(["alt", "left"])
        return True

    def reload(self) -> bool:
        self.ctx.platform.input.press(["ctrl", "r"])
        return True

    def new_tab(self, url: str = "") -> Dict[str, Any]:
        self.ctx.platform.input.press(["ctrl", "t"])
        time.sleep(0.5)
        if url:
            return self.open(url, wait_s=4.0)
        return {"ok": True, "note": "Opened a new tab."}

    def close_tab(self) -> bool:
        self.ctx.platform.input.press(["ctrl", "w"])
        return True
