"""Application bootstrap.

This module is the seam between "the engine" and "a running program".  It loads
settings, configures logging with redaction, builds the runtime (models, tools,
vision, memory, security, speech), installs a crash hook that writes to the log
instead of vanishing, and either opens the window or hands over to the CLI.

Nothing here imports PySide6 at module level: ``jarvis doctor`` must work on a
machine with no interface components at all.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

from . import __version__, paths
from .config import ABSOLUTE_RAM_LIMIT_GB, ABSOLUTE_VRAM_LIMIT_GB, load
from .logging_setup import dump_diagnostics, setup as configure_logging

log = logging.getLogger(__name__)

BANNER = r"""
     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
     ██║███████║██████╔╝██║   ██║██║███████╗
██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝
"""


def prepare_logging(settings: Any) -> None:
    """Configure the redacting logger from settings."""
    configure_logging(level=str(getattr(settings.general, "log_level", "INFO")),
                      to_file=bool(getattr(settings.general, "log_to_file", True)),
                      files_kept=int(getattr(settings.general, "log_files_kept", 5)))


def install_crash_hook(window_getter: Optional[Any] = None) -> None:
    """Log every uncaught exception, and show it if a window exists.

    A silent crash is the worst outcome for a non-technical user, so the hook
    writes the traceback to the log file and, when the interface is up, says so
    in the conversation rather than just disappearing.
    """
    def _hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("Uncaught exception:\n%s", text)
        try:
            window = window_getter() if callable(window_getter) else None
            if window is not None:
                conversation = getattr(window, "conversation", None)
                if conversation is not None:
                    conversation.append_notice(
                        "Something went wrong",
                        f"{exc_type.__name__}: {exc}\n"
                        "The details are in the log. JARVIS is still running.",
                        "danger")
        except Exception:  # noqa: BLE001
            pass
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _hook


def build_application(demo: Optional[bool] = None,
                      force_simulated: Optional[bool] = None,
                      with_speech: bool = True,
                      realtime: bool = True,
                      data_dir: Optional[str] = None) -> Any:
    """Create (but do not start) the JARVIS runtime.

    ``demo`` forces Demo Mode; ``force_simulated=False`` refuses to fall back to
    simulation and is used by tests that need real backends.
    """
    if data_dir:
        os.environ["JARVIS_DATA_DIR"] = str(data_dir)
    settings = load()
    prepare_logging(settings)
    if demo or os.environ.get("JARVIS_DEMO") == "1":
        os.environ["JARVIS_DEMO"] = "1"
        force_simulated = True
    from .core.runtime import build_runtime

    log.info("JARVIS %s starting (data dir: %s)", __version__, paths.data_dir())
    runtime = build_runtime(settings=settings, force_simulated=force_simulated,
                            realtime=realtime, with_speech=with_speech)
    return runtime


SINGLE_INSTANCE_MUTEX = "jarvis-single-instance"


def acquire_single_instance() -> Optional[Any]:
    """Hold a named mutex so only one JARVIS runs at a time.

    Two instances would fight over the memory database, the microphone and the
    push-to-talk hotkey.  The handle must stay alive for the life of the
    process; the installer checks the same name before overwriting files.
    Returns the handle, or None when another JARVIS already holds it (and
    therefore this process should exit) - on non-Windows platforms a lock file
    does the same job.
    """
    if os.name == "nt":
        try:
            import ctypes

            kernel = ctypes.windll.kernel32
            handle = kernel.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
            last_error = ctypes.get_last_error() if ctypes.get_last_error \
                else kernel.GetLastError()
            if not handle:
                return object()          # cannot tell; assume we are alone
            if last_error == 183:        # ERROR_ALREADY_EXISTS
                kernel.CloseHandle(handle)
                return None
            return handle
        except Exception:  # noqa: BLE001
            log.debug("mutex unavailable", exc_info=True)
            return object()
    lock_path = paths.data_dir() / ".single-instance.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.exists():
            age = time.time() - lock_path.stat().st_mtime
            try:
                owner = int(lock_path.read_text().strip() or 0)
            except (ValueError, OSError):
                owner = 0
            # A stale lock (crashed process, powered-off machine) must not
            # lock the user out for ever.
            if age < 86400 and owner == os.getpid():
                return None
            if age < 300 and owner and _pid_alive(owner):
                return None
        lock_path.write_text(str(os.getpid()))
        return lock_path
    except OSError:
        return object()


def _pid_alive(pid: int) -> bool:
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:  # noqa: BLE001
        return False


def run_gui(argv: Optional[List[str]] = None, demo: bool = False,
            show_wizard: bool = False, data_dir: Optional[str] = None) -> int:
    """Start the graphical interface.  Returns a process exit code."""
    argv = list(argv or [])
    instance = acquire_single_instance()
    if instance is None:
        message = ("JARVIS is already running.\n\nUse the window or the tray "
                   "icon - a second copy would fight the first for the "
                   "microphone and the memory database.")
        print(message, file=sys.stderr)
        if os.name == "nt":
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    0, message, "JARVIS", 0x40)     # MB_ICONINFORMATION
            except Exception:  # noqa: BLE001
                pass
        return 0
    from .ui import qt_available

    probe = qt_available()
    if not probe.get("available"):
        print("The JARVIS interface cannot start on this machine.\n"
              f"Reason: {probe.get('reason')}\n"
              f"What to do: {probe.get('hint')}\n\n"
              "The engine still works headless - try 'jarvis doctor' or "
              "'jarvis run \"what can you do?\"'.", file=sys.stderr)
        return 3

    runtime = build_application(demo=demo or None, data_dir=data_dir)
    window_box: Dict[str, Any] = {"window": None}
    install_crash_hook(lambda: window_box.get("window"))

    from PySide6.QtWidgets import QApplication

    from .ui.controller import UiController
    from .ui.main_window import MainWindow
    from .ui.theme import Theme, stylesheet

    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("JARVIS")
    app.setOrganizationName("JARVIS Local")
    app.setApplicationVersion(__version__)
    theme = Theme.from_settings(runtime.settings_provider())
    app.setStyleSheet(stylesheet(theme))

    runtime.start(background=True)

    controller = UiController(runtime)
    window = MainWindow(runtime, controller=controller,
                        show_wizard=show_wizard or not
                        bool(runtime.settings.general.first_run_complete))
    window_box["window"] = window
    if runtime.settings.ui.start_minimised:
        window.showMinimized()
    else:
        window.show()

    code = app.exec()
    try:
        runtime.shutdown()
    except Exception:  # noqa: BLE001
        log.exception("shutdown failed")
    return int(code)


def print_report(runtime: Any) -> str:
    """One honest block of text describing what this install can do."""
    status = runtime.status().as_dict()
    settings = runtime.settings_provider()
    ollama = status.get("ollama") or {}
    tools = status.get("tools") or {}
    speech = status.get("speech") or {}
    resources = status.get("resources") or {}
    install = runtime.ollama_install_info or {}
    lines = [
        f"JARVIS {__version__} - data dir: {paths.data_dir()}",
        "",
        "Ollama",
        f"  installed: {'yes' if install.get('installed') else 'NO'}"
        + (f" ({install.get('version')})" if install.get("version") else ""),
        f"  reachable: {'yes' if ollama.get('reachable') else 'NO'}"
        f" at {settings.ollama.host}:{settings.ollama.port}",
        f"  models installed: {len(ollama.get('models') or [])}",
        f"  main model: {ollama.get('main_model') or '(none selected)'}",
        f"  vision model: {ollama.get('vision_model') or '(none selected)'}",
        f"  fast model: {ollama.get('fast_model') or '(none selected)'}",
        "",
        "Tools",
        f"  registered: {tools.get('total', 0)}    available here: "
        f"{tools.get('available', 0)}",
        "",
        "Screen and input",
    ]
    lines.append(runtime.platform_summary().strip() or "  (no capability report)")
    lines += [
        "",
        "Voice",
        f"  recognition: {'on' if settings.speech.stt_enabled else 'off'}"
        f"   synthesis: {'on' if settings.speech.tts_enabled else 'off'}"
        f"   activation: {settings.speech.activation_mode}",
        f"  recognition: {str((speech.get('stt') or {}).get('engine', 'none'))} "
        f"({'available' if (speech.get('stt') or {}).get('available') else 'not available'})",
        f"  synthesis:   {str((speech.get('tts') or {}).get('engine', 'none'))} "
        f"({'available' if (speech.get('tts') or {}).get('available') else 'not available'})",
        "",
        "Safety",
        f"  mode: {settings.security.mode}   automation master switch: "
        f"{'on' if settings.security.automation_master_switch else 'OFF'}",
        f"  RAM ceiling: {ABSOLUTE_RAM_LIMIT_GB:.0f} GB   VRAM ceiling: "
        f"{ABSOLUTE_VRAM_LIMIT_GB:.0f} GB",
        f"  current RAM: {float(resources.get('ram_gb', 0.0)):.2f} GB   pressure: "
        f"{resources.get('pressure', 'unknown')}",
        "",
        "Simulation",
        f"  demo/simulated mode: {'YES - actions are not real' if runtime.platform.is_simulated else 'no'}",
    ]
    unavailable = runtime.registry.unavailable_report()
    if unavailable:
        lines += ["", "Tools that cannot run on this machine:"]
        for entry in unavailable[:40]:
            lines.append(f"  • {entry.get('tool')}: {entry.get('reason')}")
    return "\n".join(lines)


__all__ = ["ABSOLUTE_RAM_LIMIT_GB", "ABSOLUTE_VRAM_LIMIT_GB", "BANNER",
           "build_application", "dump_diagnostics", "install_crash_hook",
           "print_report", "prepare_logging", "run_gui"]
