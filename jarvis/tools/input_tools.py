"""Mouse and keyboard control.

Every tool here resolves its target *dynamically* - by description through UI
Automation / OCR - rather than trusting coordinates the model remembered from a
previous screenshot.  Coordinate-based tools exist as an escape hatch (drag and
drop, canvas work, games) and say so in their description.

Verification is the point of this module: after a click or a keystroke JARVIS
looks at the screen again and reports whether anything actually changed, so the
model can retry with a different approach instead of marching on blind.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence

from ..security.policy import Risk
from ..vision.elements import EDITABLE_TYPES
from . import timing
from .base import Tool, ToolContext, ToolResult, ToolSpec, Verification

log = logging.getLogger(__name__)

BUTTONS = ("left", "right", "middle")


#: Controls that can actually receive typed text.  Passing this to the element
#: matcher is how JARVIS tells Spotify's "Search" *button* apart from its
#: "Search" *field* - intent, not just a name match.
EDITABLE = tuple(EDITABLE_TYPES)


def _element_wait_budget(ctx: ToolContext, wait_s: Optional[float]) -> float:
    """How long to keep re-looking for an element while a UI loads."""
    automation = getattr(ctx.settings, "automation", None)
    default = float(getattr(automation, "element_wait_s", 6.0) or 0.0)
    ceiling = float(getattr(automation, "max_wait_s", 30.0) or 30.0)
    budget = default if wait_s is None else max(0.0, float(wait_s))
    return min(budget, max(ceiling, 0.0))


def _resolve(ctx: ToolContext, description: str, window: str = "",
             prefer_types: Sequence[str] = (),
             require_pattern: Optional[str] = None,
             wait_s: Optional[float] = None) -> Dict[str, Any]:
    """Ask the vision service for the best element matching a description.

    A just-opened app is often still blank (Spotify loads for a second or two),
    so a first miss is NOT an answer: keep re-observing until the element shows
    up or the wait budget runs out.  Only then report "not found".
    """
    if ctx.vision is None:
        return {}
    if window:
        ctx.vision.observe(window_title=window, reason="resolve:window")
    deadline = time.time() + _element_wait_budget(ctx, wait_s)
    found: Dict[str, Any] = {}
    while True:
        found = ctx.vision.find_element(description, fresh=True,
                                        prefer_types=prefer_types,
                                        require_pattern=require_pattern)
        if found.get("matches") or time.time() >= deadline:
            break
        time.sleep(0.35)
    if not found.get("matches") and (prefer_types or require_pattern):
        # Nothing of the wanted kind exists.  Retry without the intent filter
        # so the caller can report the nearest thing instead of nothing at all.
        fallback = ctx.vision.find_element(description, fresh=True)
        fallback["intent_filtered_out"] = True
        return fallback
    return found


def _click_result_from(found: Dict[str, Any], ctx: ToolContext,
                       button: str, clicks: int,
                       use_uia: bool = True) -> ToolResult:
    matches = found.get("matches") or []
    if not matches:
        return ToolResult(
            ok=False,
            error=(f"Could not find anything matching '{found.get('query')}'. "
                   "Nothing was clicked."),
            data={"query": found.get("query"), "hint": found.get("hint", "")},
            evidence={"clicked": False, "reason": "element-not-found"})
    top = matches[0]
    el = top.element
    cx, cy = el.center.x, el.center.y
    if el.is_password:
        return ToolResult(
            ok=False,
            error=("That is a password field. JARVIS will not interact with "
                   "credential inputs - please type your password yourself."),
            data={"query": found.get("query")},
            evidence={"clicked": False, "reason": "password-field"})

    clicked_via = "coordinates"
    ok = False
    if use_uia and el.source == "uia" and el.handle is not None and button == "left" \
            and clicks == 1:
        try:
            ok = bool(ctx.platform.uia.invoke(el))
            clicked_via = "ui-automation-invoke"
        except Exception as exc:  # noqa: BLE001
            log.debug("UIA invoke failed (%s); falling back to a real click", exc)
            ok = False
    if not ok:
        try:
            ctx.platform.input.move(cx, cy,
                                    duration=float(ctx.settings.automation.mouse_move_duration_s)
                                    if ctx.settings.automation.move_mouse_smoothly else 0.0)
            ok = bool(ctx.platform.input.click(cx, cy, button, clicks))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, error=f"Click failed: {exc}",
                              evidence={"clicked": False, "reason": str(exc)})
    timing.click_settle(ctx)
    return ToolResult(
        ok=bool(ok),
        message=(f"Clicked '{el.name or el.value or el.control_type}' at ({cx},{cy}) "
                 f"via {clicked_via}"),
        data={"element": el.as_dict(), "x": cx, "y": cy, "button": button,
              "clicks": clicks, "clicked_via": clicked_via,
              "score": top.score, "reason": top.reason},
        evidence={"clicked": bool(ok), "x": cx, "y": cy,
                  "element": (el.name or el.value or el.control_type)[:80],
                  "type": el.control_type, "source": el.source,
                  "via": clicked_via},
        suggest_observe=True)


class ClickElementTool(Tool):
    spec = ToolSpec(
        name="click_element",
        description=(
            "Click an interface element found by description ('Search', 'Play', "
            "'the first result', 'New Composition', 'Dark mode'). This is the "
            "preferred way to click: it uses UI Automation when the control "
            "supports it and falls back to real mouse coordinates from the "
            "current screen, so it keeps working when windows move or the theme "
            "changes. Refuses to touch password fields."),
        parameters={
            "type": "object",
            "properties": {
                "description": {"type": "string",
                                "description": "What to click, in plain English."},
                "window": {"type": "string",
                           "description": "Optional window/application to search in."},
                "button": {"type": "string", "enum": list(BUTTONS),
                           "description": "Mouse button (default left)."},
                "clicks": {"type": "integer",
                           "description": "1 = click, 2 = double-click (default 1)."},
                "candidate": {"type": "integer",
                              "description": "Use the Nth ranked candidate (0 = best, "
                                             "default 0) when the best match is wrong."},
                "wait_s": {"type": "number",
                           "description": "Seconds to keep looking while the UI loads "
                                          "(default from settings; 0 = fail fast)."},
            },
            "required": ["description"],
        },
        category="input", risk=Risk.ALLOWED, reversible=True,
        requires=("input", "screen_capture"), cost="medium")

    def run(self, description: str = "", window: str = "", button: str = "left",
            clicks: int = 1, candidate: int = 0, wait_s: Optional[float] = None,
            **_: Any) -> ToolResult:
        if not (description or "").strip():
            return self._fail("A description of what to click is required.")
        button = (button or "left").lower()
        if button not in BUTTONS:
            button = "left"
        clicks = max(1, min(int(clicks or 1), 3))
        found = _resolve(self.ctx, description, window, wait_s=wait_s)
        matches = found.get("matches") or []
        idx = max(0, min(int(candidate or 0), max(0, len(matches) - 1)))
        if matches and idx:
            found = dict(found)
            found["matches"] = [matches[idx]] + [m for m in matches[:idx]]
        return _click_result_from(found, self.ctx, button, clicks)

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        return _verify_screen_changed(self.ctx, result,
                                      f"clicked '{args.get('description')}'")


class ClickAtTool(Tool):
    spec = ToolSpec(
        name="click_at",
        description=(
            "Click absolute screen coordinates. Only use this when no named "
            "element exists (a canvas, a game, an image region) - prefer "
            "click_element, because coordinates break as soon as the window "
            "moves."),
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "Horizontal screen position."},
                "y": {"type": "integer", "description": "Vertical screen position."},
                "button": {"type": "string", "enum": list(BUTTONS)},
                "clicks": {"type": "integer", "description": "1 or 2."},
            },
            "required": ["x", "y"],
        },
        category="input", risk=Risk.ALLOWED, reversible=True,
        requires=("input",), cost="low")

    def run(self, x: int = 0, y: int = 0, button: str = "left", clicks: int = 1,
            **_: Any) -> ToolResult:
        width, height = self._screen_size()
        x, y = int(x), int(y)
        if width and height and not (0 <= x < width and 0 <= y < height):
            return self._fail(f"({x},{y}) is outside the screen ({width}x{height}).")
        element = None
        try:
            element = self.ctx.platform.uia.element_at(x, y)
        except Exception:  # noqa: BLE001
            element = None
        if element is not None and element.is_password:
            return ToolResult(
                ok=False,
                error="That position contains a password field. JARVIS does not "
                      "interact with credential inputs.")
        button = (button or "left").lower() if (button or "left").lower() in BUTTONS else "left"
        clicks = max(1, min(int(clicks or 1), 3))
        ok = bool(self.ctx.platform.input.click(x, y, button, clicks))
        timing.click_settle(self.ctx)
        return ToolResult(ok=ok, message=f"Clicked ({x},{y})",
                          data={"x": x, "y": y, "button": button, "clicks": clicks,
                                "element_at_point": element.as_dict() if element else None},
                          evidence={"clicked": ok, "x": x, "y": y,
                                    "element": (element.name if element else "")},
                          suggest_observe=True)

    def _screen_size(self):
        try:
            return self.ctx.platform.screen.size()
        except Exception:  # noqa: BLE001
            return (0, 0)

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        return _verify_screen_changed(self.ctx, result, f"clicked ({args.get('x')},{args.get('y')})")


class MoveMouseTool(Tool):
    spec = ToolSpec(
        name="move_mouse",
        description="Move the mouse cursor to coordinates, to the centre of a "
                    "named element, or read the current cursor position (pass no "
                    "arguments). Hovering is how you reveal tooltips and "
                    "dropdown menus.",
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer"}, "y": {"type": "integer"},
                "description": {"type": "string",
                                "description": "Optional element description to hover."},
                "report_position": {"type": "boolean"},
            },
        },
        category="input", risk=Risk.ALLOWED, reversible=True,
        requires=("input",), cost="low")

    def run(self, x: Optional[int] = None, y: Optional[int] = None,
            description: str = "", report_position: bool = False, **_: Any) -> ToolResult:
        if report_position or (x is None and y is None and not description):
            pos = self.ctx.platform.input.position()
            return ToolResult(ok=True, message=f"Cursor at ({pos.x},{pos.y})",
                              data={"x": pos.x, "y": pos.y},
                              evidence={"x": pos.x, "y": pos.y})
        if description:
            found = _resolve(self.ctx, description)
            matches = found.get("matches") or []
            if not matches:
                return ToolResult(ok=False,
                                  error=f"No element matching '{description}'.")
            c = matches[0].element.center
            x, y = c.x, c.y
        duration = float(self.settings.automation.mouse_move_duration_s) \
            if self.settings.automation.move_mouse_smoothly else 0.0
        ok = bool(self.ctx.platform.input.move(int(x), int(y), duration=duration))
        pos = self.ctx.platform.input.position()
        return ToolResult(ok=ok, message=f"Cursor moved to ({pos.x},{pos.y})",
                          data={"requested": [int(x), int(y)], "actual": [pos.x, pos.y]},
                          evidence={"moved": ok, "x": pos.x, "y": pos.y})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        req = (result.data or {}).get("requested")
        ok = bool(ev.get("moved"))
        if req:
            ok = ok and abs(ev.get("x", -9999) - req[0]) <= 3 and \
                abs(ev.get("y", -9999) - req[1]) <= 3
        return Verification(checked=True, succeeded=ok,
                            detail=(f"Cursor confirmed at ({ev.get('x')},{ev.get('y')})"
                                    if ok else "Cursor did not reach the target"),
                            evidence=ev)


class ScrollTool(Tool):
    spec = ToolSpec(
        name="scroll",
        description="Scroll the window or list under the cursor. direction: up, "
                    "down, left, right, page_up, page_down, home, end. Use it to "
                    "reveal content that is not visible yet before searching for "
                    "an element.",
        parameters={
            "type": "object",
            "properties": {
                "direction": {"type": "string",
                              "enum": ["up", "down", "left", "right", "page_up",
                                       "page_down", "home", "end"]},
                "amount": {"type": "integer",
                           "description": "Number of wheel notches (default 3)."},
                "x": {"type": "integer", "description": "Optional position to scroll at."},
                "y": {"type": "integer"},
                "description": {"type": "string",
                                "description": "Optional element to scroll over."},
            },
            "required": ["direction"],
        },
        category="input", risk=Risk.ALLOWED, reversible=True,
        requires=("input",), cost="low")

    def run(self, direction: str = "down", amount: int = 3, x: Optional[int] = None,
            y: Optional[int] = None, description: str = "", **_: Any) -> ToolResult:
        direction = (direction or "down").lower()
        amount = max(1, min(int(amount or 3), 30))
        if description:
            found = _resolve(self.ctx, description)
            matches = found.get("matches") or []
            if matches:
                c = matches[0].element.center
                x, y = c.x, c.y
        if direction == "home":
            self.ctx.platform.input.press(["ctrl", "home"])
            return self._ok("Scrolled to the top with Ctrl+Home", direction=direction)
        if direction == "end":
            self.ctx.platform.input.press(["ctrl", "end"])
            return self._ok("Scrolled to the bottom with Ctrl+End", direction=direction)
        if direction == "page_up":
            for _ in range(amount):
                self.ctx.platform.input.press(["pageup"])
                time.sleep(0.06)
            return self._ok(f"Paged up {amount} time(s)", direction=direction)
        if direction == "page_down":
            for _ in range(amount):
                self.ctx.platform.input.press(["pagedown"])
                time.sleep(0.06)
            return self._ok(f"Paged down {amount} time(s)", direction=direction)
        dx = dy = 0
        if direction == "down":
            dy = amount
        elif direction == "up":
            dy = -amount
        elif direction == "right":
            dx = amount
        elif direction == "left":
            dx = -amount
        else:
            return self._fail(f"Unknown scroll direction '{direction}'.")
        ok = bool(self.ctx.platform.input.scroll(dx, dy, x, y))
        time.sleep(0.15)
        return ToolResult(ok=ok, message=f"Scrolled {direction} by {amount}",
                          data={"direction": direction, "amount": amount,
                                "at": [x, y] if x is not None else None},
                          evidence={"scrolled": ok, "direction": direction},
                          suggest_observe=True)

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        return _verify_screen_changed(self.ctx, result, "scrolled", tolerate_same=True)


class DragTool(Tool):
    spec = ToolSpec(
        name="drag",
        description="Drag from one point or element to another (drag and drop, "
                    "slider adjustment, selecting a region). Provide either "
                    "descriptions or explicit coordinates for both ends.",
        parameters={
            "type": "object",
            "properties": {
                "from_description": {"type": "string"},
                "to_description": {"type": "string"},
                "from_x": {"type": "integer"}, "from_y": {"type": "integer"},
                "to_x": {"type": "integer"}, "to_y": {"type": "integer"},
                "duration_s": {"type": "number"},
                "button": {"type": "string", "enum": list(BUTTONS)},
            },
        },
        category="input", risk=Risk.APPROVAL, reversible=True,
        requires=("input",), cost="medium")

    def run(self, from_description: str = "", to_description: str = "",
            from_x: Optional[int] = None, from_y: Optional[int] = None,
            to_x: Optional[int] = None, to_y: Optional[int] = None,
            duration_s: float = 0.0, button: str = "left", **_: Any) -> ToolResult:
        def point(desc: str, x: Optional[int], y: Optional[int]):
            if desc:
                matches = (_resolve(self.ctx, desc).get("matches") or [])
                if not matches:
                    return None, f"no element matching '{desc}'"
                c = matches[0].element.center
                return (c.x, c.y), ""
            if x is not None and y is not None:
                return (int(x), int(y)), ""
            return None, "missing coordinates"

        start, err1 = point(from_description, from_x, from_y)
        end, err2 = point(to_description, to_x, to_y)
        if start is None or end is None:
            return self._fail(f"Cannot drag: {err1 or err2}")
        dur = float(duration_s) or float(self.settings.automation.mouse_move_duration_s) + 0.2
        ok = bool(self.ctx.platform.input.drag(start[0], start[1], end[0], end[1],
                                               duration=dur, button=button or "left"))
        time.sleep(0.25)
        return ToolResult(ok=ok, message=f"Dragged {start} -> {end}",
                          data={"from": list(start), "to": list(end), "button": button},
                          evidence={"dragged": ok, "from": list(start), "to": list(end)},
                          suggest_observe=True)

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        return _verify_screen_changed(self.ctx, result, "dragged")


class TypeTextTool(Tool):
    spec = ToolSpec(
        name="type_text",
        description=(
            "Type text. If 'element' is given, that element is located and "
            "focused first (and optionally cleared) so the text lands in the "
            "right place. Then, when the control supports it, the value is "
            "written through UI Automation and read back to confirm; otherwise "
            "real keystrokes are sent and the screen is re-read. JARVIS refuses "
            "to type into password fields - the user must enter credentials."),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type."},
                "element": {"type": "string",
                            "description": "Optional element description to type into "
                                           "('Search box', 'Address bar')."},
                "window": {"type": "string"},
                "clear_first": {"type": "boolean",
                                "description": "Select-all and delete before typing "
                                               "(default true when an element is given)."},
                "press_enter": {"type": "boolean",
                                "description": "Press Enter afterwards (default false)."},
                "slow": {"type": "boolean",
                         "description": "Type slowly for applications that drop "
                                        "fast keystrokes."},
            },
            "required": ["text"],
        },
        category="input", risk=Risk.ALLOWED, reversible=True,
        requires=("input",), cost="medium")

    def run(self, text: str = "", element: str = "", window: str = "",
            clear_first: Optional[bool] = None, press_enter: bool = False,
            slow: bool = False, **_: Any) -> ToolResult:
        if text is None:
            return self._fail("Nothing to type.")
        text = str(text)
        if len(text) > 8000:
            text = text[:8000]
            log.warning("Truncated type_text payload to 8000 characters")
        interval = float(self.settings.automation.typing_interval_s)
        if slow:
            interval = max(interval, 0.06)
        target = None
        if element:
            found = _resolve(self.ctx, element, window,
                             prefer_types=EDITABLE, require_pattern="set_value")
            matches = found.get("matches") or []
            if not matches:
                return ToolResult(
                    ok=False,
                    error=(f"Could not find an input matching '{element}'. "
                           "Nothing was typed."),
                    data={"hint": found.get("hint", "")},
                    evidence={"typed": False, "reason": "element-not-found"})
            target = matches[0].element
            if target.is_password:
                return ToolResult(
                    ok=False,
                    error=("Refusing to type into a password field. Please enter "
                           "credentials yourself - JARVIS never handles passwords."),
                    evidence={"typed": False, "reason": "password-field"})
            cx, cy = target.center.x, target.center.y
            self.ctx.platform.input.move(cx, cy, 0.0)
            self.ctx.platform.input.click(cx, cy, "left", 1)
            time.sleep(0.12)
        if clear_first if clear_first is not None else bool(element):
            self.ctx.platform.input.press(["ctrl", "a"])
            time.sleep(0.05)
            self.ctx.platform.input.press(["delete"])
            time.sleep(0.08)

        wrote_via = "keystrokes"
        ok = False
        if target is not None and target.source == "uia" and target.handle is not None:
            try:
                if self.ctx.platform.uia.set_value(target, text):
                    ok, wrote_via = True, "ui-automation"
            except Exception as exc:  # noqa: BLE001
                log.debug("UIA set_value failed: %s", exc)
        if not ok:
            ok = bool(self.ctx.platform.input.type_text(text, interval=interval))
        if press_enter and ok:
            time.sleep(0.1)
            self.ctx.platform.input.press(["enter"])
        time.sleep(0.2)

        # read back
        readback = ""
        if target is not None:
            readback = self._read_back(target)
        return ToolResult(
            ok=bool(ok),
            message=(f"Typed {len(text)} characters"
                     + (f" into '{element}'" if element else "")
                     + (" and pressed Enter" if press_enter else "")),
            data={"chars": len(text), "element": element, "via": wrote_via,
                  "readback": readback[:200], "pressed_enter": bool(press_enter)},
            evidence={"typed": bool(ok), "chars": len(text), "via": wrote_via,
                      "readback": readback[:200], "expected_prefix": text[:60]},
            suggest_observe=bool(press_enter))

    def _read_back(self, target: Any) -> str:
        try:
            focused = self.ctx.platform.uia.focused()
            if focused is not None and focused.value and not focused.is_password:
                return focused.value
        except Exception:  # noqa: BLE001
            pass
        try:
            el = self.ctx.platform.uia.element_at(target.center.x, target.center.y)
            if el is not None and not el.is_password:
                return el.value or ""
        except Exception:  # noqa: BLE001
            pass
        return ""

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        if not ev.get("typed"):
            return Verification(checked=True, succeeded=False,
                                detail="Text was not accepted by the system",
                                evidence=ev)
        expected = (args.get("text") or "")[:60].strip().lower()
        readback = (ev.get("readback") or "").strip().lower()
        if expected and readback:
            ok = expected in readback or readback in expected
            return Verification(checked=True, succeeded=ok,
                                detail=(f"Field now contains '{readback[:60]}'"
                                        if ok else
                                        f"Field contains '{readback[:60]}' but "
                                        f"'{expected[:40]}' was expected"),
                                evidence=ev)
        return Verification(checked=False, succeeded=True,
                            detail="Keystrokes were sent; no read-back was "
                                   "possible, so observe_screen is recommended.",
                            evidence=ev)


class SetElementValueTool(Tool):
    spec = ToolSpec(
        name="set_element_value",
        description="Set the value of an input control directly through UI "
                    "Automation (no keystrokes, so nothing can steal focus "
                    "mid-way) and read it back to confirm. Preferred over "
                    "type_text for search boxes, address bars and form fields.",
        parameters={
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Which control."},
                "text": {"type": "string", "description": "New value."},
                "window": {"type": "string"},
            },
            "required": ["description", "text"],
        },
        category="input", risk=Risk.ALLOWED, reversible=True,
        requires=("ui_elements",), cost="low")

    def run(self, description: str = "", text: str = "", window: str = "",
            **_: Any) -> ToolResult:
        found = _resolve(self.ctx, description, window,
                         prefer_types=EDITABLE, require_pattern="set_value")
        matches = found.get("matches") or []
        if not matches:
            return ToolResult(ok=False,
                              error=f"No control matching '{description}'.",
                              evidence={"set": False})
        el = matches[0].element
        if "set_value" not in (el.patterns or ()) and el.control_type not in EDITABLE:
            return ToolResult(
                ok=False,
                error=(f"'{el.name or el.control_type}' is a {el.control_type}, not "
                       "a text field, so it cannot hold that value. Describe the "
                       "field itself (for example 'search box' rather than 'search')."),
                data={"element": el.as_dict()},
                evidence={"set": False, "reason": "not-an-input"})
        if el.is_password:
            return ToolResult(
                ok=False,
                error="Refusing to write to a password field.",
                evidence={"set": False, "reason": "password-field"})
        ok = False
        try:
            ok = bool(self.ctx.platform.uia.set_value(el, str(text)))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, error=f"set_value failed: {exc}",
                              evidence={"set": False})
        if not ok:
            # UIA could not write it - fall back to focus + keystrokes.
            try:
                self.ctx.platform.input.click(el.center.x, el.center.y, "left", 1)
                time.sleep(0.1)
                self.ctx.platform.input.press(["ctrl", "a"])
                self.ctx.platform.input.press(["delete"])
                ok = bool(self.ctx.platform.input.type_text(
                    str(text), interval=float(self.settings.automation.typing_interval_s)))
            except Exception as exc:  # noqa: BLE001
                return ToolResult(ok=False, error=f"fallback typing failed: {exc}",
                                  evidence={"set": False})
        time.sleep(0.15)
        readback = ""
        try:
            again = self.ctx.platform.uia.element_at(el.center.x, el.center.y)
            if again is not None and not again.is_password:
                readback = again.value or ""
        except Exception:  # noqa: BLE001
            pass
        return ToolResult(ok=bool(ok), message=f"Set '{el.name}' to '{str(text)[:60]}'",
                          data={"element": el.as_dict(), "text": str(text)[:400],
                                "readback": readback[:200]},
                          evidence={"set": bool(ok), "readback": readback[:200],
                                    "expected": str(text)[:60]})

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        ev = result.evidence or {}
        expected = (ev.get("expected") or "").strip().lower()
        got = (ev.get("readback") or "").strip().lower()
        if not ev.get("set"):
            return Verification(checked=True, succeeded=False,
                                detail="Value was not written", evidence=ev)
        if got:
            ok = expected in got or got in expected
            return Verification(checked=True, succeeded=ok,
                                detail=(f"Control now reads '{got[:60]}'" if ok else
                                        f"Control reads '{got[:60]}', expected "
                                        f"'{expected[:40]}'"), evidence=ev)
        return Verification(checked=False, succeeded=True,
                            detail="Value written; read-back unavailable", evidence=ev)


class PressKeysTool(Tool):
    spec = ToolSpec(
        name="press_keys",
        description=(
            "Press a key or a key combination, e.g. 'enter', 'escape', 'f11', "
            "'ctrl+s', 'alt+tab', 'ctrl+shift+t', 'alt+f4'. Use it for keyboard "
            "shortcuts, menu navigation and confirming dialogs."),
        parameters={
            "type": "object",
            "properties": {
                "keys": {"type": "string",
                         "description": "Key name or '+'-separated combination."},
                "repeat": {"type": "integer", "description": "How many times (default 1)."},
            },
            "required": ["keys"],
        },
        category="input", risk=Risk.ALLOWED, reversible=True,
        requires=("input",), cost="low")

    def run(self, keys: Any = "", repeat: int = 1, **_: Any) -> ToolResult:
        from ..platform.input_backends import normalise_keys

        if isinstance(keys, (list, tuple)):
            combo = list(keys)
        else:
            combo = normalise_keys(str(keys))
        if not combo:
            return self._fail("No keys given.")
        repeat = max(1, min(int(repeat or 1), 20))
        ok = True
        for _ in range(repeat):
            ok = ok and bool(self.ctx.platform.input.press(combo))
            time.sleep(0.06)
        time.sleep(0.15)
        return ToolResult(ok=ok, message=f"Pressed {'+'.join(combo)}"
                          + (f" x{repeat}" if repeat > 1 else ""),
                          data={"keys": combo, "repeat": repeat},
                          evidence={"pressed": ok, "combo": "+".join(combo)},
                          suggest_observe=True)

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        return _verify_screen_changed(self.ctx, result,
                                      f"pressed {(result.evidence or {}).get('combo')}",
                                      tolerate_same=True)


def _verify_screen_changed(ctx: ToolContext, result: ToolResult, what: str,
                           tolerate_same: bool = False) -> Verification:
    """Independent verification: look at the screen again and see if it moved.

    ``tolerate_same`` is used for actions (hover/scroll/keys) where "nothing
    changed" is a legitimate outcome - the verification then reports that the
    action was delivered but produced no observable difference.
    """
    ev = dict(result.evidence or {})
    delivered = any(ev.get(k) for k in ("clicked", "scrolled", "pressed", "dragged"))
    if not delivered:
        return Verification(checked=True, succeeded=False,
                            detail=f"{what} was not delivered to the system",
                            evidence=ev)
    if ctx.vision is None:
        return Verification(checked=False, succeeded=True,
                            detail=f"{what} was delivered; no observation available",
                            evidence=ev)
    try:
        before = ctx.vision.last
        obs = ctx.vision.observe(force=True, reason=f"verify:{what}")
    except Exception as exc:  # noqa: BLE001
        return Verification(checked=False, succeeded=True,
                            detail=f"verification observation failed: {exc}",
                            evidence=ev)
    changed = True
    if before is not None and before.perceptual_hash and obs.perceptual_hash:
        changed = before.perceptual_hash != obs.perceptual_hash
    focus_changed = ""
    try:
        focused = ctx.platform.uia.focused()
        if focused is not None:
            focus_changed = focused.name or focused.control_type
    except Exception:  # noqa: BLE001
        pass
    ev.update({"screen_changed": bool(changed), "focused_now": focus_changed,
               "signals": obs.signals})
    if changed:
        return Verification(checked=True, succeeded=True,
                            detail=(f"{what} changed the screen"
                                    + (f"; focus is now '{focus_changed}'"
                                       if focus_changed else "")),
                            evidence=ev)
    if obs.signals.get("errors"):
        return Verification(checked=True, succeeded=False,
                            detail=(f"{what} produced no visible change and an "
                                    "error indicator is on screen: "
                                    + ", ".join(obs.signals["errors"][:3])),
                            evidence=ev)
    return Verification(
        checked=True, succeeded=bool(tolerate_same),
        detail=(f"{what} was delivered but the screen did not visibly change"
                + (" (this can be normal for this action)" if tolerate_same else
                   " - the click may have missed, or the control is unresponsive")),
        evidence=ev)


def build(ctx: ToolContext) -> List[Tool]:
    return [ClickElementTool(ctx), ClickAtTool(ctx), MoveMouseTool(ctx),
            ScrollTool(ctx), DragTool(ctx), TypeTextTool(ctx),
            SetElementValueTool(ctx), PressKeysTool(ctx)]
