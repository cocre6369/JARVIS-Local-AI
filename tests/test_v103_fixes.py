"""Regression tests for the v1.0.3 fixes.

1. Spotify blindness: UIA chrome-only windows must trigger the OCR fallback,
   and OCR rects must be mapped from the resized/window-cropped capture back
   to SCREEN coordinates (otherwise clicks land in the wrong place).
2. Phantom shortcuts: a task that stumbled must never be remembered as a
   replay shortcut (this is how "keeps opening a txt" persisted).
3. Closing the window must minimise to the tray - locked on, even for
   settings files written by older versions.
"""

from __future__ import annotations


def test_words_to_screen_maps_resized_and_cropped_captures():
    from jarvis.platform.base import OcrWord, Rect
    from jarvis.vision.elements import words_to_screen

    # OCR'd image: 640x360 (resized from a 1280x720 window capture that
    # started at screen (100, 50)).  A word at image (10, 20) 40x8 must land
    # at screen (120, 90) with size 80x16.
    words = [OcrWord(text="Search", rect=Rect(10, 20, 40, 8))]
    out = words_to_screen(words, img_w=640, img_h=360,
                          orig_w=1280, orig_h=720, off_x=100, off_y=50)
    assert len(out) == 1
    r = out[0].rect
    assert (r.x, r.y, r.width, r.height) == (120, 90, 80, 16)
    assert out[0].text == "Search"

    # No mapping needed -> pass-through.
    same = words_to_screen(words, img_w=100, img_h=100, orig_w=0, orig_h=0)
    assert same[0].rect.x == 10


def test_ocr_fallback_triggers_for_chrome_only_uia(runtime):
    """Spotify-like case: UIA exposes only frame/panes -> not 'content'."""
    from jarvis.platform.base import UIElement
    from jarvis.vision.service import VisionService

    chrome = [
        UIElement(name="Spotify", control_type="Window"),
        UIElement(name="", control_type="Pane"),
        UIElement(name="", control_type="Custom"),
    ]
    svc = runtime.vision
    assert isinstance(svc, VisionService)
    assert svc._count_content(chrome) == 1  # only the named window frame counts
    assert svc._count_content(chrome) < 3   # -> the OCR fallback fires

    real = chrome + [UIElement(name="Search", control_type="Edit"),
                     UIElement(name="Home", control_type="Button")]
    assert svc._count_content(real) >= 3    # rich UIA -> no fallback needed


def test_stumbled_task_is_not_remembered_as_shortcut(runtime):
    from jarvis.core.session import Step, TaskState

    agent = runtime.agent
    task = TaskState(task_id="t1", request="play violent crimes by kanye west",
                     session_id="s1")
    task.steps = [Step(index=0, tool="launch_application",
                       args={"name": "Spotify"}, ok=True),
                  Step(index=1, tool="launch_application",
                       args={"name": "Notepad"}, ok=True)]
    task.failures = 2          # it flailed before "completing"
    agent._remember_shortcut(task, "completed")
    assert not runtime.memory.shortcuts(), \
        "a flow with failures must never become a replay shortcut"

    task.repeated_actions = 1
    task.failures = 0
    agent._remember_shortcut(task, "completed")
    assert not runtime.memory.shortcuts(), \
        "a flow that repeated the same action must never become a shortcut"

    task.repeated_actions = 0
    agent._remember_shortcut(task, "completed")
    assert runtime.memory.shortcuts(), "a clean completed flow is worth remembering"


def test_close_to_tray_is_locked_on():
    from jarvis.config import Settings

    s = Settings()
    assert s.ui.close_to_tray is True          # default for fresh installs
    s.ui.close_to_tray = False                 # stale setting from an old version
    s.sanitize()
    assert s.ui.close_to_tray is True          # locked: closing must never quit
    assert s.ui.show_desktop_orb is True
