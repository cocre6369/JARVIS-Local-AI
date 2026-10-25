"""Music and media tools (Spotify first, generic media sessions second)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from ..security.policy import Risk
from .base import Tool, ToolContext, ToolResult, ToolSpec, Verification

log = logging.getLogger(__name__)


class SpotifyTool(Tool):
    spec = ToolSpec(
        name="spotify",
        description=(
            "Control Spotify natively: launch, search, play a track/artist/"
            "playlist, pause, resume, skip, and read what is playing. Playback "
            "state and transport come from the Windows media session API (which "
            "works even when Spotify is minimised); searching uses Spotify's own "
            "search field through UI Automation. Every action is verified by "
            "reading the 'now playing' state back. Use this instead of clicking "
            "around the Spotify window."),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["launch", "search", "play", "pause", "resume",
                                    "toggle", "next", "previous", "now_playing",
                                    "volume"]},
                "query": {"type": "string",
                          "description": "For play/search: what to play, e.g. "
                                         "'Violent Crimes Kanye West'."},
                "volume_percent": {"type": "integer",
                                   "description": "For action=volume: 0-100."},
            },
            "required": ["action"],
        },
        category="media", risk=Risk.ALLOWED, reversible=True,
        requires=("applications",), cost="medium")

    def run(self, action: str = "now_playing", query: str = "",
            volume_percent: Optional[int] = None, **_: Any) -> ToolResult:
        spotify = self.ctx.integrations.get("spotify")
        if spotify is None:
            return self._fail("The Spotify integration is not initialised.")
        action = (action or "").strip().lower()

        if action == "launch":
            started = spotify.ensure_running()
            return ToolResult(ok=bool(started.get("ok")),
                              message=("Spotify is open" if started.get("ok")
                                       else started.get("error", "Spotify did not open")),
                              data=started,
                              evidence={"running": spotify.is_running(),
                                        "window": bool(started.get("window"))})

        if action == "now_playing":
            status = spotify.now_playing()
            return ToolResult(ok=True, message=status.now_playing,
                              data=status.as_dict(),
                              evidence={"running": status.running,
                                        "title": status.title,
                                        "playback": status.playback,
                                        "source": status.source})

        if action == "volume":
            out = spotify.set_volume(percent=volume_percent)
            return ToolResult(ok=bool(out.get("ok")), message=str(out.get("route")),
                              data=out, evidence={"route": out.get("route"),
                                                  "volume": out.get("volume_percent")})

        if action == "search":
            if not query:
                return self._fail("A search query is required.")
            started = spotify.ensure_running()
            if not started.get("ok"):
                return ToolResult(ok=False, error=started.get("error", ""),
                                  data=started)
            found = spotify.search(query)
            results = found.get("results") or []
            return ToolResult(ok=bool(found.get("ok")) and bool(results),
                              message=(f"{len(results)} result(s) for '{query}'"
                                       if results else
                                       f"No Spotify results for '{query}'"),
                              data=found, evidence={"count": len(results),
                                                    "top": results[0]["title"]
                                                    if results else ""})

        if action == "play":
            started = spotify.ensure_running()
            if not started.get("ok"):
                return ToolResult(ok=False, error=started.get("error", ""),
                                  data=started)
            out = spotify.play(query)
            ok = bool(out.get("ok"))
            msg = (f"Playing {out.get('now_playing')}" if ok and out.get("now_playing")
                   else out.get("error") or "Playback could not be confirmed")
            return ToolResult(ok=ok, message=msg, data=out,
                              evidence={"playing": bool(out.get("verified")),
                                        "now_playing": out.get("now_playing", ""),
                                        "playback": out.get("playback", ""),
                                        "selected": (out.get("selected") or {}).get("title", "")},
                              suggest_observe=not bool(out.get("verified")))

        if action in ("pause", "resume", "toggle", "next", "previous"):
            fn = {"pause": spotify.pause, "resume": spotify.resume,
                  "toggle": spotify.toggle,
                  "next": lambda: spotify.skip("next"),
                  "previous": lambda: spotify.skip("previous")}[action]
            ok = bool(fn())
            time.sleep(0.7)
            status = spotify.now_playing()
            return ToolResult(ok=ok, message=f"{action.title()} -> {status.now_playing}",
                              data=status.as_dict(),
                              evidence={"requested": action,
                                        "playback": status.playback,
                                        "title": status.title})

        return self._fail(f"Unknown Spotify action '{action}'.")

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        action = str(args.get("action", "")).lower()
        if action == "play":
            ok = bool(ev.get("playing"))
            return Verification(
                checked=True, succeeded=ok,
                detail=(f"Spotify confirms it is playing '{ev.get('now_playing')}'"
                        if ok else
                        f"Spotify does not confirm playback (status "
                        f"'{ev.get('playback', 'unknown')}'). The track may still be "
                        "buffering, or the wrong result was selected."),
                evidence=ev)
        if action == "launch":
            ok = bool(ev.get("running"))
            return Verification(checked=True, succeeded=ok,
                                detail=("Spotify process/window is present" if ok
                                        else "Spotify is not running"), evidence=ev)
        if action == "pause":
            ok = ev.get("playback") == "paused"
            return Verification(checked=bool(ev.get("playback")), succeeded=ok,
                                detail=f"Playback status: {ev.get('playback')}",
                                evidence=ev)
        if action == "resume":
            ok = ev.get("playback") == "playing"
            return Verification(checked=bool(ev.get("playback")), succeeded=ok,
                                detail=f"Playback status: {ev.get('playback')}",
                                evidence=ev)
        if action in ("next", "previous"):
            return Verification(checked=True,
                                succeeded=bool(ev.get("title")),
                                detail=f"Now playing: {ev.get('title') or 'unknown'}",
                                evidence=ev)
        return Verification(checked=False, succeeded=result.ok,
                            detail=result.message, evidence=ev)


class GetCurrentTrackTool(Tool):
    spec = ToolSpec(
        name="get_current_track",
        description="Report what is playing right now in any media application "
                    "(title, artist, album, position, play/pause state) using the "
                    "Windows media session API. This is the honest way to answer "
                    "'what song is this?' - do not guess from the window title "
                    "when this tool is available.",
        parameters={"type": "object",
                    "properties": {"app": {"type": "string",
                                           "description": "Optional player filter."}}},
        category="read_only", risk=Risk.ALLOWED, reversible=True, cost="low")

    def run(self, app: str = "", **_: Any) -> ToolResult:
        smtc = self.ctx.integrations.get("smtc")
        if smtc is None:
            spotify = self.ctx.integrations.get("spotify")
            if spotify is not None:
                status = spotify.now_playing()
                return ToolResult(ok=status.running, message=status.now_playing,
                                  data=status.as_dict(),
                                  evidence={"title": status.title,
                                            "playback": status.playback,
                                            "source": status.source})
            return self._fail("No media session API is available on this machine.")
        session = None
        try:
            session = smtc.now_playing(app)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"Media session query failed: {exc}")
        if session is None:
            return ToolResult(ok=False,
                              error="No media application is reporting a session. "
                                    "Nothing appears to be playing.",
                              data={})
        data = session.as_dict()
        return ToolResult(ok=True, message=session.label(), data=data,
                          evidence={"title": session.title, "artist": session.artist,
                                    "status": session.status, "app": session.app_id})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        ok = bool(ev.get("title"))
        return Verification(checked=True, succeeded=ok,
                            detail=(f"{ev.get('title')} - {ev.get('artist', '')} "
                                    f"[{ev.get('status')}]" if ok
                                    else "No track information was reported"),
                            evidence=ev)


def build(ctx: ToolContext) -> List[Tool]:
    return [SpotifyTool(ctx), GetCurrentTrackTool(ctx)]
