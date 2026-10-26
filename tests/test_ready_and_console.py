"""Regression tests for two user-visible bugs.

1. "It opens Spotify and it's blank but keeps going / says it can't find the
   search bar" - JARVIS must WAIT for an app to finish loading (real content,
   not just a window frame) before acting, and element lookups must keep
   re-looking while a UI loads instead of failing on the first miss.
2. "cmd prompt keeps opening and closing" - every console child process must
   be spawned through ``jarvis/_proc.py`` so Windows never allocates a visible
   console window next to the windowed JARVIS.exe.

The simulated desktop models loading honestly: simulated Spotify exposes no
elements for its first 1.4 s, exactly like the real blank shell.
"""

from __future__ import annotations

import sys
import time


def test_launch_waits_until_spotify_has_content(runtime):
    t0 = time.time()
    res = runtime.registry.execute("launch_application", {"name": "Spotify"})
    assert res.ok, res.error
    assert res.data.get("ready") is True, "launch must not report a blank shell as ready"
    # The simulated blank shell lasts 1.4 s - the tool must have waited for it.
    assert time.time() - t0 >= 1.0
    found = runtime.registry.execute("find_element", {"description": "search"})
    assert found.ok, "the loaded UI must expose the Search box immediately"


def test_find_element_waits_while_ui_loads(runtime):
    # Disable the launch-side ready wait so the tool faces a blank shell.
    runtime.settings_provider().automation.app_ready_wait_s = 0.0
    res = runtime.registry.execute("launch_application", {"name": "Spotify"})
    assert res.ok, res.error

    t0 = time.time()
    found = runtime.registry.execute("find_element", {"description": "search"})
    assert found.ok, "find_element must keep looking while the app loads"
    assert time.time() - t0 >= 0.5  # it actually waited instead of failing fast


def test_find_element_wait_zero_fails_fast(runtime):
    settings = runtime.settings_provider()
    settings.automation.app_ready_wait_s = 0.0
    settings.automation.element_wait_s = 0.0
    runtime.registry.execute("launch_application", {"name": "Spotify"})

    t0 = time.time()
    found = runtime.registry.execute("find_element",
                                     {"description": "search", "wait_s": 0})
    assert time.time() - t0 < 1.0
    assert not found.ok  # an honest, immediate "not found" when asked for


def test_console_free_kwargs_hide_the_window():
    from jarvis import _proc

    kwargs = _proc.console_free_kwargs({"creationflags": 0x8})
    if sys.platform.startswith("win"):
        assert kwargs["creationflags"] & 0x08000000  # CREATE_NO_WINDOW OR-ed in
        assert kwargs["startupinfo"] is not None
    else:
        # Pass-through everywhere else: no behaviour change on Linux/macOS.
        assert kwargs == {"creationflags": 0x8}
