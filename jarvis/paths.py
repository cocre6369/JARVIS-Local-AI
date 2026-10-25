"""Filesystem locations used by JARVIS.

All persistent state (settings, memory database, logs, audit log, downloaded
voices and speech models) lives under a single per-user data directory so the
application can be installed into ``Program Files`` without needing write
access to its own folder.

Windows:  ``%APPDATA%\\JARVIS``
macOS:    ``~/Library/Application Support/JARVIS``
Linux:    ``~/.local/share/JARVIS``   (honours ``$XDG_DATA_HOME``)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

APP_DIR_NAME = "JARVIS"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle (i.e. the shipped .exe)."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Directory that holds bundled read-only assets.

    When frozen this is ``sys._MEIPASS`` (onefile) or the exe folder (onedir).
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Root of all writable per-user state."""
    override = os.environ.get("JARVIS_DATA_DIR")
    if override:
        root = Path(override).expanduser()
    elif is_windows():
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        root = Path(base) / APP_DIR_NAME
    elif is_macos():
        root = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        root = Path(base) / APP_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sub(name: str) -> Path:
    p = data_dir() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_file() -> Path:
    return data_dir() / "settings.json"


def memory_db() -> Path:
    return _sub("memory") / "jarvis.db"


def log_dir() -> Path:
    return _sub("logs")


def cache_dir() -> Path:
    return _sub("cache")


def models_dir() -> Path:
    """Downloaded speech assets (whisper checkpoints, piper voices)."""
    return _sub("models")


def stt_model_dir() -> Path:
    return models_dir() / "stt"


def tts_voice_dir() -> Path:
    return models_dir() / "tts"


def wakeword_dir() -> Path:
    return models_dir() / "wakeword"


def screenshots_dir() -> Path:
    """Short-lived working directory for observation screenshots.

    Files here are pruned aggressively (see :func:`prune_scratch`) because
    screenshots are a memory/disk hazard and they may contain private data.
    """
    return _sub("scratch") / "screenshots"


def scratch_dir() -> Path:
    return _sub("scratch")


def temp_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "jarvis-runtime"
    d.mkdir(parents=True, exist_ok=True)
    return d


def icon_path() -> Path:
    for candidate in (
        bundle_dir() / "packaging" / "assets" / "jarvis.ico",
        bundle_dir() / "packaging" / "assets" / "jarvis.png",
        Path(__file__).resolve().parent / "ui" / "assets" / "jarvis.png",
    ):
        if candidate.exists():
            return candidate
    return bundle_dir() / "packaging" / "assets" / "jarvis.ico"


def prune_scratch(max_files: int = 12, max_age_seconds: float = 900.0) -> int:
    """Delete old screenshots / scratch files. Returns number removed.

    JARVIS never keeps an unbounded pile of screenshots on disk: they are
    private (they show the user's desktop) and they eat memory when decoded.
    """
    import time

    root = scratch_dir()
    removed = 0
    now = time.time()
    files: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file():
            files.append(p)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[max_files:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    for p in files[:max_files]:
        try:
            if now - p.stat().st_mtime > max_age_seconds:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed
