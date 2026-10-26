"""Developer smoke test: drive the whole tool stack against the simulated
desktop.  Run with:  python scripts/_smoke_spotify.py

This is the manual equivalent of tests/test_agent_end_to_end.py and exists so a
contributor can watch the observation / action / verification loop work step by
step without needing a Windows machine or an Ollama server.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jarvis.config as config                      # noqa: E402
from jarvis.events import EventBus                  # noqa: E402
from jarvis.integrations.spotify import SpotifyIntegration   # noqa: E402
from jarvis.memory import MemoryStore               # noqa: E402
from jarvis.platform import create_simulated_platform        # noqa: E402
from jarvis.security.audit import AuditLog          # noqa: E402
from jarvis.security.guard import ResourceGuard     # noqa: E402
from jarvis.security.policy import PolicyEngine, SessionGrants  # noqa: E402
from jarvis.tools import ToolContext, build_default_registry    # noqa: E402
from jarvis.vision import VisionService             # noqa: E402


def build_context(realtime: bool = False):
    s = config.load()
    bus = EventBus()
    plat = create_simulated_platform(lambda: s, realtime=realtime)
    guard = ResourceGuard(lambda: s)
    vision = VisionService(lambda: s, plat, guard, bus)
    ctx = ToolContext(
        settings_provider=lambda: s, platform=plat, bus=bus,
        policy=PolicyEngine(lambda: s, SessionGrants()),
        audit=AuditLog(), guard=guard,
        memory=MemoryStore(settings_provider=lambda: s), vision=vision,
        integrations={})
    ctx.integrations["spotify"] = SpotifyIntegration(ctx)
    reg = build_default_registry(ctx)
    ctx.integrations["registry"] = reg
    return s, bus, plat, ctx, reg


def show(result, label: str) -> None:
    print(f"--- {label}: ok={result.ok} | {result.message[:100]}")
    if result.error:
        print(f"    error: {result.error[:220]}")
    v = result.verification
    if v is not None:
        print(f"    verify: checked={v.checked} succeeded={v.succeeded} "
              f"| {v.detail[:150]}")


def main() -> int:
    s, bus, plat, ctx, reg = build_context()
    desk = plat.simulated
    print(f"tools={len(reg)} simulated={plat.is_simulated}\n")

    show(reg.execute("list_windows"), "list_windows")
    show(reg.execute("is_application_running", {"name": "spotify"}), "spotify running?")
    show(reg.execute("launch_application", {"name": "Spotify"}), "launch_application")
    desk.advance(3)
    show(reg.execute("observe_screen", {}), "observe_screen")
    show(reg.execute("find_element", {"description": "search box"}), "find search box")
    show(reg.execute("set_element_value",
                     {"description": "search", "text": "Violent Crimes Kanye West"}),
         "set_element_value")
    show(reg.execute("press_keys", {"keys": "enter"}), "press enter")
    desk.advance(1)
    show(reg.execute("observe_screen", {"force": True}), "observe results")
    show(reg.execute("click_element",
                     {"description": "Violent Crimes - Kanye West"}), "click result")
    show(reg.execute("click_element", {"description": "Play"}), "click play")
    show(reg.execute("read_screen_text", {}), "read_screen_text")

    win = desk.window_for_app("Spotify")
    print("\nSpotify state:", json.dumps(
        {"now_playing": win.state.get("now_playing"), "playing": win.state.get("playing"),
         "title": win.title}, indent=2))
    print("screenshots written:", len(os.listdir(
        __import__("jarvis.paths", fromlist=["x"]).screenshots_dir())))
    print("action log:", len(desk.action_log), "entries")
    ok = bool(win.state.get("playing")) and win.state.get("now_playing", {}).get(
        "title") == "Violent Crimes"
    print("\nRESULT:", "PASS - song is playing" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
