"""Regression tests for the "Spotify keeps opening and closing" bug.

Root causes these lock down:

1. ``launch_application`` only looked for *windows*, so an app minimised to the
   system tray was invisible -> the tool started a second instance -> the
   single-instance app flashed and exited (the "opening and closing").
2. ``_wait_for_window`` skipped minimised windows, so a *successful* launch was
   reported as unverified, tempting the agent loop to launch yet again.
"""

from __future__ import annotations


def _launch(runtime, name: str = "Spotify"):
    return runtime.registry.execute("launch_application", {"name": name})


def _spotify_windows(runtime):
    return [w for w in runtime.platform.windows.windows()
            if "spotify" in (w.title or "").lower()
            or "spotify" in (w.process_name or "").lower()]


def test_second_launch_focuses_instead_of_relaunching(runtime):
    first = _launch(runtime)
    assert first.ok, first.error
    assert len(_spotify_windows(runtime)) == 1

    second = _launch(runtime)
    assert second.ok, second.error
    assert second.data.get("already_running") is True
    # Still exactly one instance - no flash-and-exit duplicate.
    assert len(_spotify_windows(runtime)) == 1

    tool = runtime.registry.get("launch_application")
    assert tool.verify({"name": "Spotify"}, second).succeeded


def test_minimised_window_is_restored_not_relaunched(runtime):
    _launch(runtime)
    win = _spotify_windows(runtime)[0]
    runtime.platform.windows.minimise(win)
    assert _spotify_windows(runtime)[0].is_minimised

    res = _launch(runtime)
    assert res.ok, res.error
    assert res.data.get("already_running") is True
    assert len(_spotify_windows(runtime)) == 1
    assert not _spotify_windows(runtime)[0].is_minimised  # restored, not relaunched

    tool = runtime.registry.get("launch_application")
    assert tool.verify({"name": "Spotify"}, res).succeeded


def test_tray_app_with_no_window_is_not_relaunched(runtime, monkeypatch):
    """Process alive, window hidden (the classic tray case)."""
    _launch(runtime)
    wins_backend = runtime.platform.windows
    monkeypatch.setattr(wins_backend, "windows", lambda: [])

    res = _launch(runtime, name="Spotify")
    assert res.ok, res.error
    assert res.data.get("already_running") is True
    assert res.evidence.get("process")
    assert "NOT started a second time" in res.message
    # Verification must succeed on process evidence alone, so the agent has no
    # reason to retry the launch.
    tool = runtime.registry.get("launch_application")
    assert tool.verify({"name": "Spotify"}, res).succeeded
