"""The JARVIS interface.

Importing :mod:`jarvis.ui` does **not** import PySide6 - the engine, tools and
security layers must stay usable headless (CLI, tests, CI on Linux).  Use
:func:`launch` from :mod:`jarvis.app` to start the window.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .controller import ConversationLine, UiController, UiUpdate

__all__ = ["ConversationLine", "UiController", "UiUpdate", "qt_available",
           "launch_window"]


def qt_available() -> Dict[str, Any]:
    """Report whether the interface can start on this machine, and why not."""
    try:
        import PySide6  # noqa: F401
        from PySide6.QtWidgets import QApplication  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return {"available": False,
                "reason": f"PySide6 could not be imported: {exc}",
                "hint": "Reinstall the interface components: "
                        "pip install --force-reinstall PySide6"}
    return {"available": True, "reason": "", "hint": ""}


def launch_window(runtime: Any, show_wizard: bool = False) -> int:
    """Build the main window and run the Qt event loop.  Returns the exit code."""
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow
    from .theme import Theme, stylesheet

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("JARVIS")
    app.setOrganizationName("JARVIS Local")
    theme = Theme.from_settings(runtime.settings_provider())
    app.setStyleSheet(stylesheet(theme))
    controller = UiController(runtime)
    window = MainWindow(runtime, controller=controller, show_wizard=show_wizard)
    if runtime.settings.ui.start_minimised:
        window.showMinimized()
    else:
        window.show()
    return app.exec()


__all__.append("qt_available")
