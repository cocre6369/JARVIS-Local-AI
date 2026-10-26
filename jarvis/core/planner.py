"""Planning.

JARVIS does not produce a rigid up-front plan and then execute it blindly -
that breaks the moment an application shows a dialog nobody predicted.  Instead
it keeps a *living* plan: a short goal statement, the steps it believes are
left, and the steps it has completed.  The plan is re-derived after every
verified action from what the screen actually shows.

Two things make this fast in practice:

* :func:`plan_from_shortcuts` replays a sequence the user already taught JARVIS
  ("do that again"), which skips model round-trips entirely for the steps that
  are known to work.
* :func:`suggest_first_action` gives the model a concrete starting point for the
  common cases, so the first tool call is usually right first time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .intents import (
    INTENT_APP_LAUNCH,
    INTENT_COMPLEX,
    INTENT_FILE_TASK,
    INTENT_MEDIA,
    INTENT_MEMORY,
    INTENT_REPEAT,
    INTENT_SCREEN_QUESTION,
    INTENT_SYSTEM_TASK,
    INTENT_WEB_TASK,
    Intent,
)


@dataclass
class PlanStep:
    """One intended action, before it has been attempted."""

    description: str
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    done: bool = False
    ok: Optional[bool] = None
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"description": self.description, "tool": self.tool,
                "args": self.args, "done": self.done, "ok": self.ok, "note": self.note}


@dataclass
class Plan:
    """The living plan for one task."""

    goal: str
    steps: List[PlanStep] = field(default_factory=list)
    source: str = "model"            # model | shortcut | heuristic
    notes: List[str] = field(default_factory=list)
    revisions: int = 0

    # ------------------------------------------------------------------
    def remaining(self) -> List[PlanStep]:
        return [s for s in self.steps if not s.done]

    def complete(self, index: int, ok: bool, note: str = "") -> None:
        if 0 <= index < len(self.steps):
            self.steps[index].done = True
            self.steps[index].ok = bool(ok)
            if note:
                self.steps[index].note = note

    def revise(self, steps: Sequence[str], reason: str = "") -> None:
        """Replace the not-yet-done part of the plan after new evidence."""
        self.steps = [s for s in self.steps if s.done]
        self.steps += [PlanStep(description=d) for d in steps if str(d).strip()]
        self.revisions += 1
        if reason:
            self.notes.append(reason)

    def add_note(self, note: str) -> None:
        if note and note not in self.notes:
            self.notes.append(note)

    def progress_text(self) -> str:
        if not self.steps:
            return self.goal
        done = sum(1 for s in self.steps if s.done)
        return f"{self.goal} ({done}/{len(self.steps)} steps done)"

    def as_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "source": self.source,
                "steps": [s.as_dict() for s in self.steps],
                "notes": list(self.notes), "revisions": self.revisions}


# ---------------------------------------------------------------------------
def plan_from_shortcuts(memory: Any, text: str) -> Optional[Plan]:
    """Replay a user-taught shortcut or a near-identical past task."""
    if memory is None:
        return None
    try:
        shortcut = memory.match_shortcut(text)
    except Exception:  # noqa: BLE001
        shortcut = None
    if shortcut:
        action = shortcut.get("action")
        steps = _steps_from_saved_action(action)
        if steps:
            plan = Plan(goal=text.strip(), steps=steps, source="shortcut")
            plan.add_note(f"Replaying the shortcut '{shortcut.get('phrase', text)}' "
                          "that you taught me.")
            return plan

    try:
        similar = memory.similar_tasks(text, limit=1)
    except Exception:  # noqa: BLE001
        similar = []
    for task in similar or []:
        steps = _steps_from_saved_action(getattr(task, "steps", None))
        if steps and str(getattr(task, "status", "")) == "completed":
            plan = Plan(goal=text.strip(), steps=steps, source="shortcut")
            plan.add_note("This is very close to a task that worked before, so I "
                          "am starting from the same steps and will re-check each one.")
            return plan
    return None


def _steps_from_saved_action(action: Any) -> List[PlanStep]:
    out: List[PlanStep] = []
    if isinstance(action, dict):
        action = [action]
    if not isinstance(action, (list, tuple)):
        return out
    for item in action:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("tool") or "").strip()
        args = item.get("arguments") or item.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        if not name:
            continue
        out.append(PlanStep(description=_describe(name, args), tool=name, args=dict(args)))
    return out[:12]


def _describe(tool: str, args: Dict[str, Any]) -> str:
    key = ""
    for candidate in ("description", "element", "name", "app", "path", "query",
                      "text", "url", "keys"):
        if args.get(candidate):
            key = str(args[candidate])[:48]
            break
    return f"{tool.replace('_', ' ')}" + (f": {key}" if key else "")


# ---------------------------------------------------------------------------
def heuristic_plan(intent: Intent) -> Plan:
    """A starting plan derived from the local intent classification.

    It is deliberately coarse: the model refines or replaces it after the first
    observation.  Its job is to stop the loop from spending a turn working out
    what kind of task this is.
    """
    text = intent.text.strip()
    kind = intent.kind
    steps: List[str] = []

    if kind == INTENT_SCREEN_QUESTION:
        steps = ["Observe the screen", "Answer from what is actually visible"]
    elif kind == INTENT_MEDIA:
        steps = ["Use the native media integration", "Verify playback state changed"]
    elif kind == INTENT_APP_LAUNCH:
        steps = ["Resolve and launch the application", "Verify its window is in front"]
    elif kind == INTENT_WEB_TASK:
        steps = ["Open the user's own browser at the target page",
                 "Verify the page loaded", "Read back what is on screen"]
    elif kind == INTENT_FILE_TASK:
        steps = ["Resolve the paths involved", "Perform the file operation",
                 "Verify the result on disk"]
    elif kind == INTENT_SYSTEM_TASK:
        steps = ["Read the requested system state", "Report it"]
    elif kind == INTENT_MEMORY:
        steps = ["Update or query local memory", "Confirm what was stored"]
    elif kind == INTENT_REPEAT:
        steps = ["Replay the previous task", "Verify each step"]
    else:
        steps = ["Observe the screen", "Work out the next concrete action",
                 "Act and verify", "Report the outcome"]

    return Plan(goal=text or "Assist the user",
                steps=[PlanStep(description=s) for s in steps],
                source="heuristic")


# ---------------------------------------------------------------------------
_MEDIA_WORDS = {
    "play": "play", "start": "play", "resume": "play",
    "pause": "pause", "stop": "pause",
    "next": "next", "skip": "next", "forward": "next",
    "previous": "previous", "back": "previous", "prev": "previous",
    "volume": "volume", "mute": "mute", "unmute": "mute",
}


def suggest_first_action(intent: Intent, registry_names: Sequence[str]) -> Optional[Dict[str, Any]]:
    """A candidate first tool call, offered to the model as a hint.

    Returning ``None`` is normal: most requests are better served by letting the
    model look first.  This only fires when the mapping is unambiguous *and* the
    tool exists on this machine.
    """
    have = set(registry_names or ())
    text = intent.text.lower()

    if intent.kind == INTENT_MEDIA and "media_control" in have:
        action = ""
        for word in re.findall(r"[a-z]+", text):
            if word in _MEDIA_WORDS:
                action = _MEDIA_WORDS[word]
                break
        if action == "volume":
            m = re.search(r"\b(?:to\s+)?(\d{1,3})\s*(?:%|percent)\b", text)
            if m:
                return {"name": "set_volume", "arguments": {"percent": int(m.group(1))}} \
                    if "set_volume" in have else None
            direction = "up" if re.search(r"\b(up|louder|higher)\b", text) else "down"
            return {"name": "media_control", "arguments": {"action": "volume_" + direction}}
        if action:
            args: Dict[str, Any] = {"action": action}
            track = _extract_track(text)
            if track and action == "play":
                args["query"] = track
            return {"name": "media_control", "arguments": args}

    if intent.kind == INTENT_APP_LAUNCH:
        target = _extract_app(text)
        if not target:
            return None
        if re.search(r"\b(close|quit|exit)\b", text) and "close_application" in have:
            return {"name": "close_application", "arguments": {"name": target}}
        if "launch_application" in have:
            return {"name": "launch_application", "arguments": {"name": target}}

    if intent.kind == INTENT_SCREEN_QUESTION and "observe_screen" in have:
        return {"name": "observe_screen", "arguments": {}}

    return None


def _extract_track(text: str) -> str:
    """Pull a track/search phrase out of a media request."""
    m = re.search(r"\b(?:play|search for|find|put on)\s+(.+?)(?:\s+by\s+|\s*$)", text)
    if not m:
        return ""
    candidate = m.group(1).strip(" .,!?\"'")
    stop = {"it", "that", "this", "the music", "music", "something", "my playlist",
            "the song", "some music", "spotify"}
    if not candidate or candidate.lower() in stop or len(candidate) < 3:
        return ""
    by = re.search(r"\bby\s+(.+)$", text)
    if by:
        artist = by.group(1).strip(" .,!?\"'")
        if artist:
            candidate = f"{candidate} {artist}"
    return candidate[:120]


_STOPWORDS = {"open", "launch", "start", "close", "quit", "the", "a", "an", "app",
              "application", "program", "please", "for", "me", "my", "up", "in",
              "window", "windows"}


def _extract_app(text: str) -> str:
    m = re.search(r"\b(?:open|launch|start|close|quit|switch to)\s+(?:the\s+)?"
                  r"([a-z0-9][a-z0-9 .&+\-]{1,40})", text)
    if not m:
        return ""
    words = [w for w in m.group(1).split() if w.lower() not in _STOPWORDS]
    name = " ".join(words).strip(" .,!?")
    if len(name) < 2:
        return ""
    return name[:48]
