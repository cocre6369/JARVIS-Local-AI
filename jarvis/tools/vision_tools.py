"""Observation tools - how JARVIS looks at the computer."""

from __future__ import annotations

import time

from typing import Any, Dict, List

from ..security.policy import Risk
from .base import Tool, ToolContext, ToolResult, ToolSpec, Verification


class ObserveScreenTool(Tool):
    spec = ToolSpec(
        name="observe_screen",
        description=(
            "Look at the screen right now and report what is there: the active "
            "window, every open window, the interactive controls that UI "
            "Automation can see (buttons, fields, list items with their "
            "coordinates), the text on screen from OCR, and automatic signals "
            "for error dialogs, loading screens and sign-in walls. Call this "
            "before acting on an unfamiliar interface and again after an action "
            "to verify it worked. Password fields are blacked out and their "
            "contents are never reported."),
        parameters={
            "type": "object",
            "properties": {
                "window": {"type": "string",
                           "description": "Optional application or window name to "
                                          "focus the observation on."},
                "include_image": {"type": "boolean",
                                  "description": "Set false to skip the screenshot "
                                                 "and work from the control tree "
                                                 "and OCR only (faster)."},
                "force": {"type": "boolean",
                          "description": "Ignore the 'screen unchanged' cache and "
                                         "re-analyse."},
            },
        },
        category="read_only",
        risk=Risk.ALLOWED,
        reversible=True,
        requires=("screen_capture",),
        cost="medium",
    )

    def run(self, window: str = "", include_image: bool = True,
            force: bool = False, **_: Any) -> ToolResult:
        vision = self.ctx.vision
        if vision is None:
            return self._fail("The vision subsystem is not initialised.")
        obs = vision.observe(window_title=window or "",
                             include_image=bool(include_image),
                             force=bool(force), reason="observe_screen")
        data: Dict[str, Any] = {
            "active_window": obs.active_window,
            "windows": [{"title": w.get("title"), "process": w.get("process_name"),
                         "minimised": w.get("is_minimised")} for w in obs.windows[:14]],
            "elements": obs.element_dicts(limit=34),
            "screen_text": "\n".join(obs.ocr.lines[:60]),
            "signals": obs.signals,
            "changed_since_last_observation": obs.changed,
            "simulated": obs.simulated,
            "notes": obs.notes,
        }
        msg = obs.active_window.get("title") or "the desktop"
        return ToolResult(ok=True, message=f"Observed {msg}", data=data,
                          evidence={"element_count": len(obs.elements),
                                    "ocr_lines": len(obs.ocr.lines),
                                    "signals": obs.signals},
                          duration_ms=obs.duration_ms,
                          screenshot_path=obs.image_path)

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        ok = result.ok and (ev.get("element_count", 0) > 0
                            or ev.get("ocr_lines", 0) > 0
                            or bool(result.data.get("active_window")))
        return Verification(checked=True, succeeded=ok,
                            detail=("Observation produced real data"
                                    if ok else "Observation returned nothing usable"),
                            evidence=ev)


class FindElementTool(Tool):
    spec = ToolSpec(
        name="find_element",
        description=(
            "Locate an interface element from a natural-language description, "
            "e.g. 'the search box', 'Settings button', 'the first search "
            "result', 'Play'. Returns ranked candidates with exact screen "
            "coordinates and a confidence score. Prefer this over guessing "
            "coordinates - the coordinates change with window position, theme "
            "and resolution. Returns found=false when nothing matches, in which "
            "case try different wording or scroll first."),
        parameters={
            "type": "object",
            "properties": {
                "description": {"type": "string",
                                "description": "What to look for, in plain English."},
                "window": {"type": "string",
                           "description": "Optional window/application to search in."},
                "refresh": {"type": "boolean",
                            "description": "Re-capture the screen first instead of "
                                           "using the last observation."},
                "wait_s": {"type": "number",
                           "description": "Seconds to keep re-looking while the UI "
                                          "loads (default from settings; 0 = single "
                                          "look)."},
            },
            "required": ["description"],
        },
        category="read_only",
        risk=Risk.ALLOWED,
        reversible=True,
        requires=("screen_capture",),
        cost="medium",
    )

    def run(self, description: str = "", window: str = "", refresh: bool = False,
            wait_s=None, **_: Any) -> ToolResult:
        if not (description or "").strip():
            return self._fail("A description is required.")
        vision = self.ctx.vision
        if vision is None:
            return self._fail("The vision subsystem is not initialised.")
        if window:
            vision.observe(window_title=window, reason="find_element:window")
        automation = getattr(self.settings, "automation", None)
        default = float(getattr(automation, "element_wait_s", 6.0) or 0.0)
        ceiling = float(getattr(automation, "max_wait_s", 30.0) or 30.0)
        budget = default if wait_s is None else max(0.0, float(wait_s))
        deadline = time.time() + min(budget, max(ceiling, 0.0))
        while True:
            found = vision.find_element(description, fresh=True)
            # A just-opened app is often still blank; a first miss is not an
            # answer - keep re-observing until the budget runs out.
            if found.get("match_dicts") or time.time() >= deadline:
                break
            time.sleep(0.35)
        matches = found.get("match_dicts") or []
        top = matches[0] if matches else None
        data: Dict[str, Any] = {
            "query": description,
            "found": bool(matches),
            "candidates": [{
                "name": m.get("name"), "type": m.get("control_type"),
                "x": m.get("center", {}).get("x"), "y": m.get("center", {}).get("y"),
                "width": m.get("rect", {}).get("width"),
                "height": m.get("rect", {}).get("height"),
                "score": m.get("score"), "reason": m.get("reason"),
                "automation_id": m.get("automation_id"),
                "is_password": m.get("is_password"),
                "value": m.get("value"),
            } for m in matches[:6]],
            "hint": found.get("hint", ""),
        }
        msg = (f"Found '{top.get('name')}' at "
               f"({top['center']['x']},{top['center']['y']}) "
               f"[score {top.get('score', 0):.2f}]"
               if top else f"No element matching '{description}' was found")
        return ToolResult(ok=bool(matches), message=msg, data=data,
                          evidence={"candidate_count": len(matches),
                                    "top_score": (top or {}).get("score", 0.0),
                                    "top_name": (top or {}).get("name", "")})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        ok = bool(ev.get("candidate_count", 0)) and float(ev.get("top_score", 0)) >= 0.5
        return Verification(
            checked=True, succeeded=ok,
            detail=(f"Best candidate '{ev.get('top_name')}' scored "
                    f"{float(ev.get('top_score', 0)):.2f}"
                    if ev.get("candidate_count") else "No candidate was found"),
            evidence=ev)


class ReadScreenTextTool(Tool):
    spec = ToolSpec(
        name="read_screen_text",
        description=(
            "Read the text currently visible on the screen (OCR) and return it "
            "as plain lines. Use it for 'what am I looking at?', 'read what's "
            "on the screen', 'what error am I getting?' and for confirming that "
            "a page finished loading. Any text inside a password field is "
            "excluded."),
        parameters={
            "type": "object",
            "properties": {
                "window": {"type": "string",
                           "description": "Optional window to read instead of the "
                                          "whole screen."},
                "max_lines": {"type": "integer",
                              "description": "Maximum number of lines to return "
                                             "(default 80)."},
            },
        },
        category="read_only",
        risk=Risk.ALLOWED,
        reversible=True,
        requires=("screen_capture",),
        cost="medium",
    )

    def run(self, window: str = "", max_lines: int = 80, **_: Any) -> ToolResult:
        vision = self.ctx.vision
        if vision is None:
            return self._fail("The vision subsystem is not initialised.")
        obs = vision.observe(window_title=window or "", include_image=True,
                             include_elements=False, reason="read_screen_text")
        lines: List[str] = [l for l in obs.ocr.lines if l.strip()][:max(1, int(max_lines))]
        data = {
            "lines": lines,
            "text": "\n".join(lines),
            "engine": obs.ocr.engine,
            "active_window": obs.active_window.get("title", ""),
            "signals": obs.signals,
            "notes": obs.notes,
        }
        if not lines:
            return ToolResult(
                ok=False, data=data,
                error="OCR returned no text. Either the screen is empty, or no "
                      "OCR engine is installed (see Settings > Automation for "
                      "what is available).")
        return ToolResult(ok=True, message=f"Read {len(lines)} lines of screen text",
                          data=data, evidence={"line_count": len(lines),
                                               "engine": obs.ocr.engine})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        n = (result.evidence or {}).get("line_count", 0)
        return Verification(checked=True, succeeded=bool(n),
                            detail=f"{n} line(s) of text recognised",
                            evidence=result.evidence or {})


class WhatIsOnScreenTool(Tool):
    """Convenience wrapper the model can call for 'what am I looking at?'."""

    spec = ToolSpec(
        name="describe_screen",
        description=(
            "Produce a short natural-language description of what is currently "
            "on the screen: which application is in front, what it is showing, "
            "whether an error or a loading indicator is visible. Cheaper than "
            "observe_screen - use it for questions like 'what am I looking at?' "
            "or 'why isn't this application working?'."),
        parameters={
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "Optional window name."},
            },
        },
        category="read_only",
        risk=Risk.ALLOWED,
        reversible=True,
        requires=("screen_capture",),
        cost="low",
    )

    def run(self, window: str = "", **_: Any) -> ToolResult:
        vision = self.ctx.vision
        if vision is None:
            return self._fail("The vision subsystem is not initialised.")
        obs = vision.observe(window_title=window or "", include_image=False,
                             include_elements=True, reason="describe_screen")
        summary = obs.prompt_text(max_chars=2200)
        data = {"summary": summary, "signals": obs.signals,
                "active_window": obs.active_window.get("title", ""),
                "windows": [w.get("title") for w in obs.windows[:10]],
                "simulated": obs.simulated}
        return ToolResult(ok=True, message="Screen described", data=data,
                          evidence={"has_text": bool(summary.strip()),
                                    "element_count": len(obs.elements)})


def build(ctx: ToolContext) -> List[Tool]:
    return [ObserveScreenTool(ctx), FindElementTool(ctx), ReadScreenTextTool(ctx),
            WhatIsOnScreenTool(ctx)]
