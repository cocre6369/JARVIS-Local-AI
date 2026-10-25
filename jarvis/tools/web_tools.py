"""Web browsing tools.

JARVIS never fetches web content itself - its own network client talks to the
local Ollama server and nothing else (:mod:`jarvis.security.netguard` enforces
that).  Web tasks go through the *user's own browser*, and JARVIS reads the
resulting page with the same vision pipeline it uses for any other application.
That satisfies "may search the internet and inspect publicly accessible
webpages" while keeping every byte of JARVIS's own traffic on localhost.

Anything that would *download* a file, or that points at a banking/financial
site, is classified so the permission layer asks the user first.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List
from urllib.parse import urlparse

from ..security.policy import FINANCIAL_URL_MARKERS, Risk
from .base import Tool, ToolContext, ToolResult, ToolSpec, Verification

log = logging.getLogger(__name__)

DOWNLOAD_EXTENSIONS = (".exe", ".msi", ".zip", ".rar", ".7z", ".iso", ".dmg",
                       ".pkg", ".apk", ".deb", ".rpm", ".appx", ".msix", ".scr",
                       ".bat", ".cmd", ".ps1", ".jar", ".tar", ".gz")


def looks_like_download(url: str) -> bool:
    path = urlparse(url or "").path.lower()
    return path.endswith(DOWNLOAD_EXTENSIONS)


class OpenUrlTool(Tool):
    spec = ToolSpec(
        name="open_url",
        description=(
            "Open a web address in the user's default browser. Banking, "
            "financial and government-account sites require approval before they "
            "are opened, and URLs that point at a downloadable file (installer, "
            "archive, executable) require approval because that is a download. "
            "JARVIS will never log in, submit a form containing credentials, or "
            "complete a purchase on your behalf."),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL, or a domain "
                                                         "like 'youtube.com'."},
                "browser": {"type": "string",
                            "description": "Optional browser name (default: the "
                                           "system default)."},
                "new_tab": {"type": "boolean"},
            },
            "required": ["url"],
        },
        category="navigation", risk=Risk.ALLOWED, reversible=True,
        requires=("applications",), cost="medium")

    def dynamic_risk(self, args: Dict[str, Any]) -> Risk:
        url = str(args.get("url", "")).lower()
        if looks_like_download(url):
            return Risk.APPROVAL
        if any(m in url for m in FINANCIAL_URL_MARKERS):
            return Risk.APPROVAL
        return Risk.ALLOWED

    def run(self, url: str = "", browser: str = "", new_tab: bool = False,
            **_: Any) -> ToolResult:
        browser_integration = self.ctx.integrations.get("browser")
        if browser_integration is None:
            return self._fail("The browser integration is not initialised.")
        from ..integrations.browser import build_url

        full = build_url(url, engine="google")
        if not full:
            return self._fail("No URL given.")
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https", "file"):
            return self._fail(f"Unsupported URL scheme '{parsed.scheme}'.")
        if new_tab and browser_integration.is_running():
            browser_integration.new_tab()
        out = browser_integration.open(full, browser=browser)
        if not out.get("ok"):
            return ToolResult(ok=False, error=out.get("error", "could not open URL"),
                              data=out)
        return ToolResult(ok=True,
                          message=f"Opened {out.get('title') or full}",
                          data=out,
                          evidence={"opened": True, "title": out.get("title", ""),
                                    "url": full, "loaded": bool(out.get("loaded"))},
                          suggest_observe=True)

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        ok = bool(ev.get("opened")) and bool(ev.get("title"))
        return Verification(checked=bool(ev.get("opened")), succeeded=ok,
                            detail=(f"Browser window reads '{ev.get('title')}'"
                                    if ok else "No browser window confirmed the page"),
                            evidence=ev)


class WebSearchTool(Tool):
    spec = ToolSpec(
        name="web_search",
        description=(
            "Search the web in the user's browser and read the results page. "
            "engine: google, duckduckgo, bing, youtube, wikipedia, github, "
            "images, news, maps. After searching, use read_screen_text or "
            "find_element on the results to pick one - do not assume which "
            "result is first. Nothing is downloaded by searching."),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "engine": {"type": "string",
                           "enum": ["google", "duckduckgo", "bing", "youtube",
                                    "wikipedia", "github", "images", "news", "maps"]},
            },
            "required": ["query"],
        },
        category="navigation", risk=Risk.ALLOWED, reversible=True,
        requires=("applications",), cost="medium")

    def run(self, query: str = "", engine: str = "google", **_: Any) -> ToolResult:
        query = (query or "").strip()
        if not query:
            return self._fail("A search query is required.")
        browser_integration = self.ctx.integrations.get("browser")
        if browser_integration is None:
            return self._fail("The browser integration is not initialised.")
        out = browser_integration.search(query, engine=engine or "google")
        if not out.get("ok"):
            return ToolResult(ok=False, error=out.get("error", "search failed"),
                              data=out)
        # Give the page a moment, then read what actually arrived.
        lines: List[str] = []
        if self.ctx.vision is not None:
            try:
                obs = self.ctx.vision.observe(include_image=False, reason="web_search")
                lines = [l for l in obs.ocr.lines if l.strip()][:40]
            except Exception as exc:  # noqa: BLE001
                log.debug("post-search observation failed: %s", exc)
        data = dict(out)
        data["engine"] = engine
        data["query"] = query
        data["results_text"] = lines
        return ToolResult(ok=True, message=f"Searched {engine} for '{query}'",
                          data=data,
                          evidence={"opened": True, "url": out.get("url", ""),
                                    "text_lines": len(lines)},
                          suggest_observe=True)

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        ok = bool(ev.get("opened"))
        return Verification(checked=ok, succeeded=ok,
                            detail=(f"Search page loaded with "
                                    f"{ev.get('text_lines', 0)} readable line(s)"
                                    if ok else "Search page did not load"),
                            evidence=ev)


class BrowserTool(Tool):
    spec = ToolSpec(
        name="browser",
        description=(
            "Control the browser that is already open: current_page (what page "
            "and URL am I on), tabs (only when DevTools mode is enabled in "
            "Automation settings), back, forward, reload, new_tab, close_tab, "
            "fullscreen_on, fullscreen_off, read_page (OCR the visible page). "
            "Use current_page or read_page to verify a navigation worked."),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["current_page", "tabs", "back", "forward",
                                    "reload", "new_tab", "close_tab",
                                    "fullscreen_on", "fullscreen_off", "read_page"]},
                "url": {"type": "string", "description": "For new_tab."},
            },
            "required": ["action"],
        },
        category="navigation", risk=Risk.ALLOWED, reversible=True,
        requires=("window_control",), cost="low")

    def run(self, action: str = "current_page", url: str = "", **_: Any) -> ToolResult:
        b = self.ctx.integrations.get("browser")
        if b is None:
            return self._fail("The browser integration is not initialised.")
        action = (action or "").strip().lower()
        if action == "current_page":
            data = b.current_page()
            return ToolResult(ok=bool(data.get("ok")),
                              message=data.get("page_title") or data.get("error", ""),
                              data=data, evidence={"title": data.get("page_title", "")})
        if action == "tabs":
            tabs = b.tabs()
            if not tabs:
                return ToolResult(
                    ok=False,
                    error="Tab listing needs DevTools mode, which is off by default. "
                          "Enable it in Settings > Automation, or use current_page.",
                    data={"tabs": []})
            return ToolResult(ok=True, message=f"{len(tabs)} open tab(s)",
                              data={"tabs": tabs}, evidence={"count": len(tabs)})
        if action == "back":
            ok = bool(b.go_back())
            return self._after_nav(ok, "Went back", b)
        if action == "forward":
            self.ctx.platform.input.press(["alt", "right"])
            return self._after_nav(True, "Went forward", b)
        if action == "reload":
            ok = bool(b.reload())
            return self._after_nav(ok, "Reloaded the page", b)
        if action == "new_tab":
            out = b.new_tab(url or "")
            return ToolResult(ok=bool(out.get("ok")), message=str(out.get("note") or out.get("url")),
                              data=out, evidence={"opened": bool(out.get("ok"))},
                              suggest_observe=True)
        if action == "close_tab":
            ok = bool(b.close_tab())
            return ToolResult(ok=ok, message="Closed the current tab",
                              data={}, evidence={"requested": ok}, suggest_observe=True)
        if action in ("fullscreen_on", "fullscreen_off"):
            out = b.set_fullscreen(on=(action == "fullscreen_on"))
            return ToolResult(ok=bool(out.get("ok")),
                              message=("Full screen on" if out.get("verified") and
                                       action == "fullscreen_on" else
                                       "Full screen off" if out.get("verified") else
                                       "Full-screen state could not be confirmed"),
                              data=out, evidence={"verified": bool(out.get("verified")),
                                                  "via": out.get("via", "")})
        if action == "read_page":
            win = b.active_window()
            if win is None:
                return self._fail("No browser window is open.")
            self.ctx.platform.windows.focus(win)
            time.sleep(0.3)
            if self.ctx.vision is None:
                return self._fail("Vision is unavailable.")
            obs = self.ctx.vision.observe(window_title=win.title, include_image=False,
                                          reason="browser:read_page")
            lines = [l for l in obs.ocr.lines if l.strip()][:80]
            return ToolResult(ok=bool(lines),
                              message=f"Read {len(lines)} line(s) from the page",
                              data={"lines": lines, "text": "\n".join(lines),
                                    "url": obs.active_window.get("title", "")},
                              evidence={"lines": len(lines)})
        return self._fail(f"Unknown browser action '{action}'.")

    def _after_nav(self, ok: bool, message: str, b: Any) -> ToolResult:
        time.sleep(0.9)
        data = b.current_page()
        return ToolResult(ok=ok, message=f"{message}: {data.get('page_title', '?')}",
                          data=data, evidence={"title": data.get("page_title", "")},
                          suggest_observe=True)

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        if "verified" in ev:
            return Verification(checked=True, succeeded=bool(ev["verified"]),
                                detail=("Window geometry confirms full screen state"
                                        if ev["verified"] else
                                        "Full-screen state is not confirmed"),
                                evidence=ev)
        ok = bool(ev.get("title")) or bool(ev.get("lines")) or bool(ev.get("opened"))
        return Verification(checked=True, succeeded=ok,
                            detail=(f"Page reads '{ev.get('title')}'"
                                    if ev.get("title") else "Page state confirmed"),
                            evidence=ev)


def build(ctx: ToolContext) -> List[Tool]:
    if os.name != "nt" and not ctx.platform.is_simulated:
        log.info("Web tools registered on a non-Windows host; they use the "
                 "generic browser resolver.")
    return [OpenUrlTool(ctx), WebSearchTool(ctx), BrowserTool(ctx)]
