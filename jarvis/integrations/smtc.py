"""Windows System Media Transport Controls (SMTC).

This is the *native* way to ask "what's playing?" and to control playback for
every media application on Windows 10/11 - Spotify, a YouTube tab, Groove,
VLC.  It is dramatically more reliable than clicking buttons, so JARVIS tries it
first and only falls back to UI automation when it is unavailable.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..platform.winrt_util import await_op, import_winrt

log = logging.getLogger(__name__)

PLAYBACK_STATUS = {
    0: "closed", 1: "changing", 2: "stopped", 3: "playing", 4: "paused",
}


@dataclass
class MediaSession:
    app_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    status: str = "unknown"
    position_ms: int = 0
    duration_ms: int = 0
    controls: Dict[str, bool] = field(default_factory=dict)

    @property
    def is_playing(self) -> bool:
        return self.status == "playing"

    def as_dict(self) -> Dict[str, Any]:
        return {"app_id": self.app_id, "title": self.title, "artist": self.artist,
                "album": self.album, "status": self.status,
                "position_s": round(self.position_ms / 1000.0, 1),
                "duration_s": round(self.duration_ms / 1000.0, 1),
                "controls": dict(self.controls)}

    def label(self) -> str:
        if not self.title:
            return "Nothing is playing"
        who = f" by {self.artist}" if self.artist else ""
        return f"{self.title}{who}"


class SmtcMedia:
    name = "smtc"

    def __init__(self) -> None:
        self._mod = import_winrt([
            "winrt.windows.media.control",
            "windows.media.control",
        ])
        if self._mod is None:
            raise ImportError(
                "Windows.Media.Control projection is not installed "
                "(pip install winrt-Windows.Media.Control)")
        self._manager: Any = None
        self._manager_ts: float = 0.0

    # ------------------------------------------------------------------
    def _get_manager(self) -> Any:
        if self._manager is not None and time.time() - self._manager_ts < 5.0:
            return self._manager
        mgr_cls = getattr(self._mod, "GlobalSystemMediaTransportControlsSessionManager")
        self._manager = await_op(mgr_cls.request_async(), timeout=8.0)
        self._manager_ts = time.time()
        return self._manager

    def sessions(self) -> List[Any]:
        try:
            mgr = self._get_manager()
            return list(mgr.get_sessions() or [])
        except Exception as exc:  # noqa: BLE001
            log.debug("SMTC get_sessions failed: %s", exc)
            return []

    def current(self) -> Optional[Any]:
        try:
            return self._get_manager().get_current_session()
        except Exception as exc:  # noqa: BLE001
            log.debug("SMTC current session failed: %s", exc)
            return None

    def session_for(self, app_hint: str) -> Optional[Any]:
        hint = (app_hint or "").lower()
        if not hint:
            return self.current()
        for s in self.sessions():
            try:
                if hint in str(getattr(s, "source_app_user_model_id", "")).lower():
                    return s
            except Exception:  # noqa: BLE001
                continue
        return self.current()

    # ------------------------------------------------------------------
    def now_playing(self, app_hint: str = "") -> Optional[MediaSession]:
        session = self.session_for(app_hint)
        if session is None:
            return None
        out = MediaSession(app_id=str(getattr(session, "source_app_user_model_id", "")))
        try:
            props = session.try_get_media_properties()
            if props is not None:
                out.title = str(getattr(props, "title", "") or "")
                out.artist = str(getattr(props, "artist", "") or "")
                out.album = str(getattr(props, "album_title", "") or "")
        except Exception as exc:  # noqa: BLE001
            log.debug("SMTC properties failed: %s", exc)
        try:
            info = session.get_playback_info()
            out.status = PLAYBACK_STATUS.get(int(info.playback_status), "unknown")
            for name in ("controls",):
                controls = getattr(info, name, None)
                if controls is not None:
                    out.controls = {
                        "play": bool(getattr(controls, "is_play_enabled", False)),
                        "pause": bool(getattr(controls, "is_pause_enabled", False)),
                        "next": bool(getattr(controls, "is_next_enabled", False)),
                        "previous": bool(getattr(controls, "is_previous_enabled", False)),
                        "shuffle": bool(getattr(controls, "is_shuffle_enabled", False)),
                        "repeat": bool(getattr(controls, "is_repeat_enabled", False)),
                    }
        except Exception as exc:  # noqa: BLE001
            log.debug("SMTC playback info failed: %s", exc)
        try:
            timeline = session.get_timeline_properties()
            out.position_ms = int(getattr(timeline, "position", 0) // 10000)
            out.duration_ms = int(getattr(timeline, "end_time", 0) // 10000)
        except Exception:  # noqa: BLE001
            pass
        return out

    # ------------------------------------------------------------------
    def _invoke(self, method: str, app_hint: str = "") -> bool:
        session = self.session_for(app_hint)
        if session is None:
            return False
        fn = getattr(session, method, None)
        if fn is None:
            return False
        try:
            result = await_op(fn(), timeout=6.0)
            return bool(result) if result is not None else True
        except Exception as exc:  # noqa: BLE001
            log.debug("SMTC %s failed: %s", method, exc)
            return False

    def play(self, app_hint: str = "") -> bool:
        return self._invoke("try_play_async", app_hint)

    def pause(self, app_hint: str = "") -> bool:
        return self._invoke("try_pause_async", app_hint)

    def toggle(self, app_hint: str = "") -> bool:
        return self._invoke("try_toggle_play_pause_async", app_hint)

    def next(self, app_hint: str = "") -> bool:
        return self._invoke("try_skip_next_async", app_hint)

    def previous(self, app_hint: str = "") -> bool:
        return self._invoke("try_previous_async", app_hint)

    def stop(self, app_hint: str = "") -> bool:
        return self._invoke("try_stop_async", app_hint)

    def seek(self, position_s: float, app_hint: str = "") -> bool:
        session = self.session_for(app_hint)
        if session is None:
            return False
        try:
            ticks = int(float(position_s) * 10_000_000)
            return bool(await_op(
                session.try_change_playback_position_async(ticks), timeout=6.0))
        except Exception as exc:  # noqa: BLE001
            log.debug("SMTC seek failed: %s", exc)
            return False


class NullSmtc:
    name = "none"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def now_playing(self, app_hint: str = "") -> Optional[MediaSession]:
        return None

    def sessions(self) -> List[Any]:
        return []

    def play(self, app_hint: str = "") -> bool:
        return False

    pause = toggle = next = previous = stop = play

    def seek(self, position_s: float, app_hint: str = "") -> bool:
        return False


def create_smtc() -> Any:
    try:
        return SmtcMedia()
    except Exception as exc:  # noqa: BLE001
        log.info("SMTC unavailable: %s", exc)
        return NullSmtc(str(exc))
