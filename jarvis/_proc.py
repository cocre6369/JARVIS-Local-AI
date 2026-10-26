"""Subprocess helpers that never pop a console window.

JARVIS.exe is a *windowed* application: it has no console of its own.  On
Windows, a windowed process that spawns a console program (``nvidia-smi``,
``tasklist``, ``piper``, ``cmd.exe``...) with plain ``subprocess`` makes the OS
allocate a **brand-new visible console window** for the child - the notorious
"cmd prompt keeps flashing" bug.

Every subprocess call in the package must go through :func:`run` / :func:`popen`
below, which add ``CREATE_NO_WINDOW`` and a hidden ``STARTUPINFO`` on Windows
(and are exact pass-throughs everywhere else).  A CI gate
(``scripts/_ci_subprocess_gate.py``) fails the build if a raw
``subprocess.run/Popen`` sneaks back in.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Dict

#: Value of CREATE_NO_WINDOW on Windows; hard-coded so tests on other OSes can
#: still assert the flag would be applied.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_STARTF_USESHOWWINDOW = 0x00000001
_SW_HIDE = 0


def console_free_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the hide-the-console flags into Popen/run keyword arguments.

    Caller-supplied ``creationflags`` (e.g. DETACHED_PROCESS for app launches)
    are OR-ed in, never clobbered; per the Win32 docs CREATE_NO_WINDOW is
    simply ignored when combined with DETACHED_PROCESS/CREATE_NEW_CONSOLE, so
    OR-ing is always safe.
    """
    if not sys.platform.startswith("win"):
        return kwargs
    out = dict(kwargs)
    flags = int(out.get("creationflags") or 0)
    out["creationflags"] = flags | _CREATE_NO_WINDOW
    startupinfo = out.get("startupinfo")
    if startupinfo is None:
        startupinfo = subprocess.STARTUPINFO()
        out["startupinfo"] = startupinfo
    try:
        startupinfo.dwFlags |= _STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = _SW_HIDE
    except Exception:  # noqa: BLE001 - never let cosmetics break a call
        pass
    return out


def run(*args: Any, **kwargs: Any) -> "subprocess.CompletedProcess":
    """``subprocess.run`` with no console window on Windows."""
    return subprocess.run(*args, **console_free_kwargs(kwargs))


def popen(*args: Any, **kwargs: Any) -> "subprocess.Popen":
    """``subprocess.Popen`` with no console window on Windows."""
    return subprocess.Popen(*args, **console_free_kwargs(kwargs))


def call(*args: Any, **kwargs: Any) -> int:
    return subprocess.call(*args, **console_free_kwargs(kwargs))


def check_output(*args: Any, **kwargs: Any) -> Any:
    return subprocess.check_output(*args, **console_free_kwargs(kwargs))


__all__ = ["console_free_kwargs", "run", "popen", "call", "check_output"]
