"""Local intent classification.

This exists for *speed*, not for shortcuts around the model.  A 7B model on a
laptop GPU takes a second or two to produce its first token; a large part of
that is prompt processing.  Classifying the request locally (microseconds) lets
JARVIS:

* pick the cheapest model that can still do the job (fast model for one-shot
  media/app commands, main model for multi-step computer control),
* decide whether a screenshot is worth capturing and encoding at all,
* pre-warm the model that is about to be used,
* recognise "do that again" and similar repeat requests.

The model still chooses every action.  Nothing here executes anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Which model role should handle the request, and how much context it needs.
INTENT_CHITCHAT = "chitchat"
INTENT_CAPABILITY = "capability"
INTENT_SCREEN_QUESTION = "screen_question"
INTENT_MEDIA = "media"
INTENT_APP_LAUNCH = "app_launch"
INTENT_FILE_TASK = "file_task"
INTENT_SYSTEM_TASK = "system_task"
INTENT_WEB_TASK = "web_task"
INTENT_REPEAT = "repeat"
INTENT_MEMORY = "memory"
INTENT_COMPLEX = "complex"

#: Ordered: first match wins.  (intent, regex, needs_screen, needs_tools)
_PATTERNS: Tuple[Tuple[str, str, bool, bool], ...] = (
    (INTENT_REPEAT,
     r"\b(do (that|it) again|repeat that|same (again|thing)|run that again|"
     r"one more time|as before)\b", False, True),
    (INTENT_SCREEN_QUESTION,
     r"\b(what (am i|'m i|are we)? ?(looking at|seeing)|what'?s on (my |the )?screen|"
     r"read (the|my) screen|describe (the|my) screen|whats on screen|can you see|"
     r"what does (this|that) say|screenshot)\b", True, True),
    (INTENT_MEDIA,
     r"\b(play|pause|resume|stop|next (track|song)|previous (track|song)|skip|"
     r"shuffle|repeat|volume|mute|unmute|turn (it|the music) (up|down)|"
     r"what'?s playing|now playing|song)\b", False, True),
    (INTENT_APP_LAUNCH,
     r"\b(open|launch|start|close|quit|minimi[sz]e|maximi[sz]e|switch to|"
     r"bring .{0,24} to the front)\b", False, True),
    (INTENT_WEB_TASK,
     r"\b(search (the web|online|google|duckduckgo|bing|for)|google |look up|"
     r"open .{0,30}\.(com|org|net|io|gov|edu)|browse to|go to (the )?website|"
     r"youtube)\b", True, True),
    (INTENT_FILE_TASK,
     r"\b(file|files|folder|folders|directory|documents?|downloads?|desktop|"
     r"move|copy|rename|delete|organise|organize|save .{0,20} (to|in)|find .{0,20} file)\b",
     False, True),
    (INTENT_SYSTEM_TASK,
     r"\b(battery|cpu|gpu|ram|memory usage|disk|storage|volume level|brightness|"
     r"dark mode|night light|display settings|bluetooth|wifi|network status|"
     r"system info|specs|temperature)\b", False, True),
    (INTENT_MEMORY,
     r"\b(remember|don'?t forget|note that|what do you (know|remember)|"
     r"forget (that|this|everything)|my (preference|preferences)|you said)\b",
     False, True),
    (INTENT_CAPABILITY,
     r"\b(can you|could you|are you able|do you know how|what can you do|"
     r"how do i|help me understand|what are your)\b", False, False),
    (INTENT_CHITCHAT,
     r"^(hi|hello|hey|yo|good (morning|afternoon|evening)|thanks|thank you|"
     r"cheers|nice|ok|okay|cool|sorry|who are you|what are you)\b", False, False),
)

_COMPILED = [(intent, re.compile(pattern, re.I), screen, tools)
             for intent, pattern, screen, tools in _PATTERNS]

#: Words that mean the request will need several coordinated steps.
_COMPLEX_MARKERS = (
    "then", "after that", "and then", "once", "before you", "make sure",
    "for each", "all of them", "everything in", "step by step", "set up",
    "install", "configure", "compare", "summarise", "summarize", "download and",
)


@dataclass
class Intent:
    """Result of local classification."""

    kind: str = INTENT_COMPLEX
    text: str = ""
    confidence: float = 0.0
    needs_screen: bool = False
    needs_tools: bool = True
    matched: str = ""
    hints: List[str] = field(default_factory=list)

    #: Which Ollama model role should answer (``main`` / ``fast``).
    @property
    def model_role(self) -> str:
        # One-shot, unambiguous, single-tool requests go to the fast model when
        # the user has configured one.  Anything multi-step stays on the main
        # model - being wrong there costs far more time than it saves.
        if self.kind in (INTENT_MEDIA, INTENT_APP_LAUNCH, INTENT_CHITCHAT,
                         INTENT_CAPABILITY, INTENT_SYSTEM_TASK) and \
                self.confidence >= 0.75 and not self.multi_step:
            return "fast"
        return "main"

    @property
    def multi_step(self) -> bool:
        return bool(self.hints and "multi-step" in self.hints)

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "confidence": round(self.confidence, 3),
                "needs_screen": self.needs_screen, "needs_tools": self.needs_tools,
                "matched": self.matched, "model_role": self.model_role,
                "hints": list(self.hints)}


def classify(text: str) -> Intent:
    """Classify a user request locally.  Never raises."""
    raw = (text or "").strip()
    if not raw:
        return Intent(kind=INTENT_CHITCHAT, text=raw, confidence=0.0)

    low = raw.lower()
    words = len(re.findall(r"[a-z0-9']+", low))
    hints: List[str] = []

    for marker in _COMPLEX_MARKERS:
        if marker in low:
            hints.append("multi-step")
            break
    if words > 26:
        hints.append("multi-step")

    best: Optional[Intent] = None
    for kind, pattern, needs_screen, needs_tools in _COMPILED:
        m = pattern.search(low)
        if not m:
            continue
        # Confidence: how much of the request the pattern explains, and how
        # early it appears.  "play something" is clearly media; "play around
        # with the file permissions until it works" is not.
        span = len(m.group(0))
        coverage = span / max(1, len(low))
        position = 1.0 - (m.start() / max(1, len(low)))
        confidence = min(0.99, 0.45 + coverage * 2.4 + position * 0.25)
        if kind in (INTENT_MEDIA, INTENT_APP_LAUNCH) and words > 18:
            confidence -= 0.25
        if "multi-step" in hints:
            confidence -= 0.15
        candidate = Intent(kind=kind, text=raw, confidence=max(0.05, confidence),
                           needs_screen=needs_screen, needs_tools=needs_tools,
                           matched=m.group(0), hints=list(hints))
        if best is None or candidate.confidence > best.confidence:
            best = candidate

    if best is None:
        best = Intent(kind=INTENT_COMPLEX, text=raw,
                      confidence=0.4 if words > 6 else 0.6,
                      needs_screen=False, needs_tools=True, hints=list(hints))
    if "multi-step" in best.hints:
        best.needs_tools = True
        if best.kind in (INTENT_CHITCHAT, INTENT_CAPABILITY):
            best.kind = INTENT_COMPLEX
    return best


def needs_first_observation(intent: Intent, last_observation_age_s: Optional[float],
                            max_age_s: float = 8.0) -> bool:
    """Should the agent look at the screen before the model's first move?

    Looking costs a screenshot + OCR round trip, so it is only done when the
    request plausibly depends on what is on screen and the cached observation is
    stale.
    """
    if intent.kind in (INTENT_CHITCHAT, INTENT_MEMORY, INTENT_CAPABILITY):
        return False
    if not intent.needs_screen and intent.kind in (INTENT_MEDIA, INTENT_APP_LAUNCH):
        # Media and app launching are driven by their own integrations first;
        # the screen is only needed if those fail.
        return False
    if last_observation_age_s is None:
        return True
    return last_observation_age_s > max_age_s


def shortcut_phrases() -> Sequence[str]:
    """Local phrases that mean 'repeat the last successful task'."""
    return ("do that again", "do it again", "repeat that", "same again",
            "run that again", "one more time")
