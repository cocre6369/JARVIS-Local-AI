"""Spotify integration.

Strategy, best first:

1. **SMTC** for playback state and transport controls - works whether Spotify is
   in the foreground or minimised, and needs no clicks at all.
2. **UI Automation** on the Spotify window for searching and selecting a track.
3. **Media keys** as the last resort.

``now_playing`` additionally falls back to parsing Spotify's own window title
(``"Violent Crimes - Kanye West - Spotify"``), which works even when the media
API is unavailable.

Nothing here bypasses the permission layer: launching Spotify is an
``application`` action, and typing into its search box is an ``input`` action -
both are classified and audited exactly like any other tool call.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

SPOTIFY_PROCESS_NAMES = ("spotify.exe", "spotify")
TITLE_SUFFIX = " - spotify"


@dataclass
class TrackResult:
    title: str
    artist: str = ""
    kind: str = "track"
    y: int = 0
    x: int = 0
    source: str = "uia"
    raw: str = ""

    def label(self) -> str:
        return f"{self.title} - {self.artist}".strip(" -")

    def as_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "artist": self.artist, "kind": self.kind,
                "x": self.x, "y": self.y, "source": self.source}


@dataclass
class SpotifyStatus:
    running: bool = False
    window_title: str = ""
    focused: bool = False
    now_playing: str = ""
    title: str = ""
    artist: str = ""
    playback: str = "unknown"
    source: str = ""
    tracks: List[TrackResult] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"running": self.running, "window_title": self.window_title,
                "focused": self.focused, "now_playing": self.now_playing,
                "title": self.title, "artist": self.artist,
                "playback": self.playback, "source": self.source,
                "tracks": [t.as_dict() for t in self.tracks[:20]]}


class SpotifyIntegration:
    name = "spotify"

    def __init__(self, ctx: Any, smtc: Any = None) -> None:
        self.ctx = ctx
        self.smtc = smtc

    # ------------------------------------------------------------------
    def _windows(self) -> List[Any]:
        try:
            return self.ctx.platform.windows.windows() or []
        except Exception:  # noqa: BLE001
            return []

    def spotify_window(self) -> Optional[Any]:
        for w in self._windows():
            proc = (w.process_name or "").lower()
            title = (w.title or "").lower()
            if any(n in proc for n in SPOTIFY_PROCESS_NAMES) or title.endswith(TITLE_SUFFIX):
                return w
        return None

    def is_running(self) -> bool:
        if self.spotify_window() is not None:
            return True
        try:
            return bool(self.ctx.platform.windows.process_running("spotify"))
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    def ensure_running(self, timeout_s: float = 25.0) -> Dict[str, Any]:
        """Launch or focus Spotify and wait until its window exists."""
        win = self.spotify_window()
        if win is None:
            try:
                procs = self.ctx.platform.windows.process_running("spotify") or []
            except Exception:  # noqa: BLE001
                procs = []
            if procs:
                # Running with no window (minimised to tray) - ask it to open.
                self.ctx.platform.apps.launch("spotify")
        if win is None:
            result = self.ctx.platform.apps.launch("Spotify") or {}
            if not result.get("ok"):
                return {"ok": False,
                        "error": result.get("error", "Spotify could not be launched"),
                        "hint": "Is Spotify installed? JARVIS looked in the Start "
                                "Menu, the registry and PATH."}
        deadline = time.time() + max(3.0, float(timeout_s))
        while time.time() < deadline:
            win = self.spotify_window()
            if win is not None and win.title.strip():
                break
            time.sleep(0.4)
        if win is None:
            return {"ok": False,
                    "error": "Spotify was started but no window appeared. It may "
                             "still be loading or showing a login screen."}
        try:
            self.ctx.platform.windows.focus(win)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.6)
        return {"ok": True, "window": win.as_dict(), "title": win.title,
                "waited_s": round(timeout_s - (deadline - time.time()), 2)}

    # ------------------------------------------------------------------
    def now_playing(self) -> SpotifyStatus:
        status = SpotifyStatus(running=self.is_running())
        win = self.spotify_window()
        if win is not None:
            status.window_title = win.title
            try:
                active = self.ctx.platform.windows.active()
                status.focused = bool(active and active.handle == win.handle)
            except Exception:  # noqa: BLE001
                status.focused = False

        # 1. SMTC
        if self.smtc is not None:
            try:
                session = self.smtc.now_playing("spotify")
            except Exception as exc:  # noqa: BLE001
                log.debug("SMTC now_playing failed: %s", exc)
                session = None
            if session is not None and session.title:
                status.title = session.title
                status.artist = session.artist
                status.playback = session.status
                status.now_playing = session.label()
                status.source = "smtc"
                return status

        # 2. window title
        if win is not None and win.title:
            parsed = _parse_spotify_title(win.title)
            if parsed:
                status.title, status.artist = parsed
                status.now_playing = f"{parsed[0]} - {parsed[1]}".strip(" -")
                status.playback = "unknown"
                status.source = "window-title"
                return status

        # 3. UI Automation "Now Playing" text
        if win is not None:
            try:
                elements = self.ctx.platform.uia.elements(root=win, max_count=220) or []
            except Exception:  # noqa: BLE001
                elements = []
            for el in elements:
                name = (el.name or "").lower()
                if "now playing" in name or (el.automation_id or "").lower() == "nowplaying":
                    text = (el.value or el.name or "").strip()
                    if text:
                        status.now_playing = text
                        status.source = "ui-automation"
                        break
        if not status.now_playing:
            status.now_playing = "Nothing is playing" if status.running else \
                "Spotify is not running"
        return status

    # ------------------------------------------------------------------
    def search(self, query: str, wait_s: float = 3.0) -> Dict[str, Any]:
        """Type a query into Spotify's search box and return the results."""
        win = self.spotify_window()
        if win is None:
            started = self.ensure_running()
            if not started.get("ok"):
                return started
            win = self.spotify_window()
        if win is None:
            return {"ok": False, "error": "Spotify has no window to search in."}
        self.ctx.platform.windows.focus(win)
        time.sleep(0.35)

        typed = False
        route = ""
        # A. UI Automation
        try:
            elements = self.ctx.platform.uia.elements(root=win, max_count=220) or []
        except Exception:  # noqa: BLE001
            elements = []
        box = _pick(elements, ("search",), prefer_types=("Edit", "Search", "ComboBox"))
        if box is not None:
            try:
                self.ctx.platform.uia.invoke(box)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)
            if self.ctx.platform.uia.set_value(box, query):
                typed, route = True, "ui-automation"
        # B. click the search box by description, then keyboard
        if not typed and self.ctx.vision is not None:
            found = self.ctx.vision.find_element("search", fresh=True)
            matches = found.get("matches") or []
            if matches:
                el = matches[0].element
                self.ctx.platform.input.click(el.center.x, el.center.y, "left", 1)
                time.sleep(0.25)
                self.ctx.platform.input.press(["ctrl", "a"])
                self.ctx.platform.input.type_text(
                    query, interval=float(self.ctx.settings.automation.typing_interval_s))
                typed, route = True, "vision+keyboard"
        # C. Spotify's global shortcut: Ctrl+K opens search in the desktop app
        if not typed:
            self.ctx.platform.input.press(["ctrl", "k"])
            time.sleep(0.4)
            self.ctx.platform.input.type_text(
                query, interval=float(self.ctx.settings.automation.typing_interval_s))
            route = "ctrl+k shortcut"
            typed = True
        if not typed:
            return {"ok": False, "error": "Could not reach Spotify's search field."}

        self.ctx.platform.input.press(["enter"])
        time.sleep(max(0.8, float(wait_s)))

        tracks = self._collect_results(win, query)
        return {"ok": True, "query": query, "route": route,
                "results": [t.as_dict() for t in tracks],
                "count": len(tracks)}

    def _collect_results(self, win: Any, query: str) -> List[TrackResult]:
        out: List[TrackResult] = []
        try:
            elements = self.ctx.platform.uia.elements(root=win, max_count=300) or []
        except Exception:  # noqa: BLE001
            elements = []
        for el in elements:
            if el.control_type in ("ListItem", "List", "DataItem", "Hyperlink",
                                   "Text", "Button"):
                name = (el.name or "").strip()
                if not name or len(name) < 2:
                    continue
                if el.rect.width <= 0:
                    continue
                title, artist = _split_title_artist(name)
                if not title:
                    continue
                out.append(TrackResult(title=title, artist=artist,
                                       kind=_guess_kind(el, name),
                                       x=el.center.x, y=el.center.y,
                                       source="uia", raw=name))
        if not out and self.ctx.vision is not None:
            obs = self.ctx.vision.observe(window_title=win.title, include_image=False,
                                          reason="spotify-results")
            for line in obs.ocr.lines:
                line = line.strip()
                if not line or len(line) < 3:
                    continue
                title, artist = _split_title_artist(line)
                out.append(TrackResult(title=title, artist=artist, kind="ocr-line",
                                       source="ocr", raw=line))
        return _rank_results(out, query)

    # ------------------------------------------------------------------
    def play(self, query: str = "") -> Dict[str, Any]:
        """Search (optionally) and start playback.  Verifies via SMTC/title."""
        if query:
            found = self.search(query)
            if not found.get("ok"):
                return found
            results = found.get("results") or []
            if not results:
                return {"ok": False, "error": f"No Spotify results for '{query}'.",
                        "searched": True}
            best = results[0]
            clicked = False
            if best.get("x") and best.get("y"):
                self.ctx.platform.input.click(int(best["x"]), int(best["y"]), "left", 2)
                clicked = True
            if not clicked and self.ctx.vision is not None:
                match = self.ctx.vision.find_element(best.get("title", query))
                matches = match.get("matches") or []
                if matches:
                    c = matches[0].element.center
                    self.ctx.platform.input.click(c.x, c.y, "left", 2)
                    clicked = True
            if not clicked:
                return {"ok": False,
                        "error": "Found results but could not click one.",
                        "results": results[:5]}
            time.sleep(1.2)
            # A double-click on a search result queues it; make sure it plays.
            self.resume()
            time.sleep(1.0)
            status = self.now_playing()
            return {"ok": status.playback == "playing" or bool(status.title),
                    "action": "play", "query": query, "selected": best,
                    "now_playing": status.now_playing,
                    "playback": status.playback, "source": status.source,
                    "verified": status.playback == "playing"}
        ok = self.resume()
        status = self.now_playing()
        return {"ok": bool(ok), "action": "resume", "now_playing": status.now_playing,
                "playback": status.playback, "source": status.source,
                "verified": status.playback == "playing"}

    def resume(self) -> bool:
        if self.smtc is not None and self.smtc.play("spotify"):
            return True
        win = self.spotify_window()
        if win is not None:
            try:
                elements = self.ctx.platform.uia.elements(root=win, max_count=200) or []
                btn = _pick(elements, ("play",), prefer_types=("Button",))
                if btn is not None and self.ctx.platform.uia.invoke(btn):
                    return True
            except Exception as exc:  # noqa: BLE001
                log.debug("Spotify play button failed: %s", exc)
        self.ctx.platform.input.press(["playpause"])
        return True

    def pause(self) -> bool:
        if self.smtc is not None and self.smtc.pause("spotify"):
            return True
        self.ctx.platform.input.press(["playpause"])
        return True

    def toggle(self) -> bool:
        if self.smtc is not None and self.smtc.toggle("spotify"):
            return True
        self.ctx.platform.input.press(["playpause"])
        return True

    def skip(self, direction: str = "next") -> bool:
        if self.smtc is not None:
            ok = self.smtc.next("spotify") if direction == "next" else \
                self.smtc.previous("spotify")
            if ok:
                return True
        self.ctx.platform.input.press(["nexttrack"] if direction == "next"
                                      else ["prevtrack"])
        return True

    def set_volume(self, percent: Optional[int] = None,
                   relative: int = 0) -> Dict[str, Any]:
        """Set or nudge the volume.

        Spotify's own slider is not reliably scriptable (it is a custom-drawn
        control in the desktop client), so JARVIS uses the Windows master volume
        through Core Audio and says so, falling back to media keys when that API
        is unavailable.  The route used is always reported - never guessed.
        """
        audio = None
        try:
            audio = self.ctx.integrations.get("audio")
        except Exception:  # noqa: BLE001
            audio = None

        if audio is not None:
            if percent is not None:
                got = audio.set_volume(float(percent))
                if got is not None:
                    return {"ok": True, "route": "windows-core-audio",
                            "volume_percent": got}
            elif relative:
                got = audio.step(up=relative > 0, steps=abs(relative))
                if got is not None:
                    return {"ok": True, "route": "windows-core-audio",
                            "volume_percent": got}

        steps = abs(int(relative or 0)) or (5 if percent is not None else 0)
        key = "volumeup" if (relative or 0) > 0 or (percent or 0) > 0 else "volumedown"
        for _ in range(min(steps, 25)):
            self.ctx.platform.input.press([key])
            time.sleep(0.03)
        if percent == 0:
            self.ctx.platform.input.press(["volumemute"])
        return {"ok": True, "route": "media-keys",
                "note": "Adjusted with media keys; the exact level is not "
                        "readable through that path."}


# ---------------------------------------------------------------------------
def _pick(elements: List[Any], keywords, prefer_types=()) -> Optional[Any]:
    best, best_score = None, 0.0
    for el in elements:
        hay = f"{el.name} {el.automation_id} {el.class_name}".lower()
        score = 0.0
        for kw in keywords:
            if kw in hay:
                score += 1.0
        if not score:
            continue
        if el.control_type in prefer_types:
            score += 0.5
        if el.is_enabled and el.is_visible and el.rect.width > 0:
            score += 0.2
        if el.is_password:
            score -= 5.0
        if score > best_score:
            best, best_score = el, score
    return best


def _split_title_artist(text: str) -> tuple:
    text = (text or "").strip()
    text = re.sub(r"\s+-\s+spotify$", "", text, flags=re.I).strip()
    if not text:
        return ("", "")
    if " - " in text:
        left, right = text.split(" - ", 1)
        return (left.strip(), right.strip())
    if " by " in text.lower():
        parts = re.split(r"(?i)\s+by\s+", text, maxsplit=1)
        return (parts[0].strip(), parts[1].strip() if len(parts) > 1 else "")
    return (text, "")


def _parse_spotify_title(title: str) -> Optional[tuple]:
    t = (title or "").strip()
    if not t or not t.lower().endswith(TITLE_SUFFIX):
        return None
    body = t[: -len(TITLE_SUFFIX)].strip()
    if not body:
        return None
    title_part, artist = _split_title_artist(body)
    return (title_part, artist) if title_part else None


def _guess_kind(el: Any, name: str) -> str:
    low = name.lower()
    if "playlist" in low:
        return "playlist"
    if "album" in low:
        return "album"
    if el.control_type == "Hyperlink":
        return "artist"
    return "track"


def _rank_results(results: List[TrackResult], query: str) -> List[TrackResult]:
    """Put the best match for the query first."""
    if not query:
        return results
    q = re.sub(r"[^a-z0-9 ]+", "", query.lower()).strip()
    q_tokens = [t for t in q.replace(" by ", " ").split() if t and len(t) > 1]
    stop = {"play", "the", "song", "track", "by", "on", "spotify", "music",
            "please", "some", "listen", "to"}
    q_tokens = [t for t in q_tokens if t not in stop]

    def score(r: TrackResult) -> float:
        hay = re.sub(r"[^a-z0-9 ]+", "", f"{r.title} {r.artist}".lower())
        s = 0.0
        if r.kind == "track":
            s += 0.6
        elif r.kind in ("playlist", "album", "artist"):
            s += 0.1
        for t in q_tokens:
            if t in hay.split():
                s += 1.0
            elif t in hay:
                s += 0.6
        if r.source == "uia":
            s += 0.2
        if r.title and r.artist:
            s += 0.15
        return s

    return sorted(results, key=lambda r: (-score(r), r.y))
