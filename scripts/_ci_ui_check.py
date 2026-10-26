"""CI gate: build the real main window offscreen.

The v1.0.0 release crashed on every launch with
``NameError: name 'QAbstractItemView' is not defined`` - a missing import that
compiles fine and only explodes when Qt actually constructs the page.  This
script does exactly that construction (offscreen, Demo Mode, no speech) so any
page, widget or dialog that cannot be built fails the build instead of the
user's first launch.

Run:  QT_QPA_PLATFORM=offscreen python scripts/_ci_ui_check.py
"""

from __future__ import annotations

import faulthandler
import os
import sys

# Hard ceiling: if anything below blocks (a modal dialog, an event loop), dump
# every traceback and exit non-zero instead of stalling the whole pipeline.
faulthandler.dump_traceback_later(150, exit=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JARVIS_DEMO", "1")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from jarvis.app import build_application

    runtime = build_application(demo=True, with_speech=False, realtime=False)
    # Mark the first run as done: otherwise MainWindow schedules the setup
    # wizard, whose modal exec() would block forever under processEvents().
    runtime.settings_provider().general.first_run_complete = True
    app = QApplication.instance() or QApplication(sys.argv[:1])

    from jarvis.ui.main_window import MainWindow
    from jarvis.ui.pages import PAGE_ORDER
    from jarvis.ui.wizard import SetupWizard  # noqa: F401  (import = compile gate)

    window = MainWindow(runtime, show_wizard=False)
    window.show()
    app.processEvents()

    missing = [pid for pid in PAGE_ORDER if pid not in window.pages]
    if missing:
        print(f"UI CHECK FAILED: pages missing: {missing}")
        return 1
    # Flip through every page so lazy widget construction runs too.
    for pid in PAGE_ORDER:
        window.show_page(pid)
        app.processEvents()

    title = window.windowTitle()
    print(f"UI CHECK OK: {title!r}, pages: {', '.join(PAGE_ORDER)}")
    # Do NOT call window.close(): closeEvent -> quit_application opens a modal
    # quit-confirmation dialog, which blocks forever on a headless runner.
    # Tear the timers and runtime down directly instead.
    window._drain_timer.stop()
    window._metrics_timer.stop()
    window.hide()
    try:
        runtime.shutdown()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
