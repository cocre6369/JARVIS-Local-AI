"""Finding an interface element from a natural-language description.

This is the piece that stops JARVIS depending on hardcoded screen coordinates.
A query like *"the search box"*, *"Settings button"*, *"the first search
result"* or *"Play"* is scored against every discovered control using:

* exact / prefix / substring name matching,
* a synonym table for common UI vocabulary,
* ``difflib`` similarity plus token overlap (so "adress bar" still finds
  "Address and search bar"),
* control-type priors ("button" wants a Button, "field" wants an Edit),
* positional priors (a "search bar" is usually in the top third),
* and penalties for disabled / off-screen / password controls.

The ranked result is returned to the model with the score and the reason, so
when the match is weak the model can look at the screen again instead of
clicking something it merely hopes is right.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..platform.base import OcrWord, Rect, UIElement

# ---------------------------------------------------------------------------
SYNONYMS: Dict[str, Sequence[str]] = {
    "search": ("search", "find", "look up", "lookup", "query", "search box",
               "search bar", "search field", "magnifier", "magnifying glass",
               "type to search", "search or enter", "address and search"),
    "settings": ("settings", "preferences", "options", "option", "config",
                 "configuration", "gear", "cog", "customize", "personalisation",
                 "personalization"),
    "play": ("play", "playback", "start playing", "resume"),
    "pause": ("pause", "stop playing"),
    "next": ("next", "skip", "forward", "next track"),
    "previous": ("previous", "back", "prev", "previous track"),
    "close": ("close", "dismiss", "x", "cancel"),
    "ok": ("ok", "okay", "confirm", "yes", "accept", "continue", "done",
           "apply", "submit", "go"),
    "open": ("open", "launch", "browse", "select file"),
    "save": ("save", "write", "keep"),
    "new": ("new", "create", "add", "compose", "new composition", "new item"),
    "delete": ("delete", "remove", "trash", "erase"),
    "login": ("sign in", "signin", "log in", "login", "log on"),
    "address": ("address bar", "url bar", "omnibox", "location bar",
                "address and search bar"),
    "fullscreen": ("full screen", "fullscreen", "full-screen", "theater mode",
                   "cinema mode"),
    "menu": ("menu", "hamburger", "more", "more options", "three dots",
             "overflow"),
    "inbox": ("inbox", "unread", "notifications", "mentions"),
    "volume": ("volume", "sound", "speaker", "mute"),
    "minimise": ("minimize", "minimise"),
    "maximise": ("maximize", "maximise"),
    "tab": ("tab", "tabs"),
    "result": ("result", "results", "match", "suggestion", "item"),
    "song": ("track", "song", "result", "item"),
    "field": ("field", "input", "box", "text box", "textbox", "edit"),
    "button": ("button", "btn", "control"),
    "checkbox": ("checkbox", "check box", "tick"),
    "dropdown": ("dropdown", "combo", "combobox", "select", "picker",
                 "list box"),
    "link": ("link", "hyperlink", "anchor"),
}

#: Control types that can receive typed text.  Used by tools that set a value
#: so that, when an app has both a "Search" *button* and a "Search" *field*,
#: the field is the one that gets the text.
EDITABLE_TYPES: Sequence[str] = ("Edit", "Search", "ComboBox", "Document",
                                 "Spinner", "MaskedEdit")

CONTROL_TYPE_HINTS: Dict[str, Sequence[str]] = {
    "button": ("Button", "Hyperlink", "MenuItem", "SplitButton", "ToggleButton"),
    "field": EDITABLE_TYPES,
    "textbox": EDITABLE_TYPES,
    "input": ("Edit", "Document"),
    "box": ("Edit", "ComboBox", "List", "Pane"),
    "checkbox": ("CheckBox", "RadioButton", "ToggleButton"),
    "tab": ("TabItem", "Tab", "ListItem"),
    "menu": ("MenuItem", "Menu", "MenuBar", "Button"),
    "dropdown": ("ComboBox", "List", "Menu"),
    "list": ("List", "DataGrid", "Table", "Tree"),
    "result": ("ListItem", "List", "Hyperlink", "Text"),
    "song": ("ListItem", "List", "Hyperlink"),
    "slider": ("Slider", "ProgressBar"),
    "link": ("Hyperlink", "Text"),
    "icon": ("Image", "Button", "Pane"),
}

_ORDINALS = {
    "first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3, "fifth": 4, "5th": 4, "last": -1, "final": -1,
    "top": 0, "bottom": -1,
}


@dataclass
class ElementMatch:
    element: UIElement
    score: float
    reason: str
    matched_text: str = ""

    def as_dict(self) -> Dict[str, Any]:
        d = self.element.as_dict()
        d["score"] = round(self.score, 3)
        d["reason"] = self.reason
        d["center"] = {"x": self.element.center.x, "y": self.element.center.y}
        return d


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _tokens(text: str) -> set:
    return {t for t in _norm(text).split() if t}


def expand_synonyms(query: str) -> List[str]:
    """All surface forms implied by a query ("search box" -> search/find/...)."""
    q = _norm(query)
    forms = [q]
    for key, words in SYNONYMS.items():
        if key in q or any(w in q for w in words):
            forms.extend(words)
    return sorted({f for f in forms if f})


def _control_type_bonus(query: str, element: UIElement) -> float:
    q = _norm(query)
    ct = (element.control_type or "").lower()
    for hint, types in CONTROL_TYPE_HINTS.items():
        if hint in q.split() or hint in q:
            if any(t.lower() == ct for t in types):
                return 0.12
            return -0.05
    return 0.0


def _positional_bonus(query: str, element: UIElement, screen: Rect) -> float:
    q = _norm(query)
    if not screen.width or not screen.height:
        return 0.0
    rel_y = (element.rect.y - screen.y) / float(screen.height)
    rel_x = (element.rect.x - screen.x) / float(screen.width)
    if ("bar" in q or "top" in q) and rel_y < 0.25:
        return 0.06
    if "bottom" in q and rel_y > 0.7:
        return 0.06
    if ("sidebar" in q or "left" in q) and rel_x < 0.25:
        return 0.05
    if "right" in q and rel_x > 0.7:
        return 0.05
    return 0.0


def _ordinal_pick(query: str, matches: List[ElementMatch]) -> List[ElementMatch]:
    q = _norm(query)
    for word, idx in _ORDINALS.items():
        if re.search(rf"\b{re.escape(word)}\b", q):
            if not matches:
                return matches
            ordered = sorted(matches, key=lambda m: (m.element.rect.y, m.element.rect.x))
            if idx == -1:
                return [ordered[-1]]
            if idx < len(ordered):
                return [ordered[idx]]
            return [ordered[-1]]
    return matches


def score_element(query: str, element: UIElement,
                  screen: Optional[Rect] = None,
                  prefer_types: Sequence[str] = (),
                  require_pattern: Optional[str] = None) -> Optional[ElementMatch]:
    """Score one element against one query; ``None`` means not a candidate.

    ``prefer_types`` lets the caller state intent ("I want something you can
    type into"), which resolves ambiguous names — Spotify has a ``Search``
    *button* in its toolbar and a ``Search`` *text field* in the sidebar.
    ``require_pattern`` penalises controls whose signature cannot perform the
    requested action.
    """
    q = _norm(query)
    if not q:
        return None
    forms = expand_synonyms(query)
    name = _norm(element.name)
    value = _norm(getattr(element, "value", "") or "")
    aid = _norm(element.automation_id)
    cls = _norm(element.class_name)
    haystacks = [(name, "name"), (value, "value"), (aid, "automation-id"),
                 (cls, "class")]

    best = 0.0
    best_reason = ""
    best_text = ""
    best_label = ""
    for text, label in haystacks:
        if not text:
            continue
        for form in forms:
            f = _norm(form)
            if not f:
                continue
            if f == text:
                cand, reason = 1.0, f"exact {label} match"
            elif text.startswith(f) and len(f) >= 3:
                cand, reason = 0.90, f"{label} starts with '{form}'"
            elif f in text:
                cand, reason = 0.80, f"'{form}' appears in {label}"
            elif text in f and len(text) >= 3:
                cand, reason = 0.72, f"{label} '{text}' appears in the query"
            else:
                ratio = difflib.SequenceMatcher(None, f, text).ratio()
                qt, tt = _tokens(form), _tokens(text)
                overlap = (len(qt & tt) / len(qt)) if qt else 0.0
                cand = max(ratio, overlap * 0.95)
                reason = f"fuzzy {label} similarity {ratio:.2f}/overlap {overlap:.2f}"
                if cand < 0.55:
                    continue
            if cand > best:
                best, best_reason, best_text, best_label = cand, reason, text, label

    if best <= 0:
        return None

    score = best

    # A control's *value* is the content it currently holds, not its identity.
    # After typing "Violent Crimes Kanye West" into Spotify's search field, that
    # field's value out-matched the search *result* the user wanted clicked.
    # Identity matches (name / automation id / class) therefore always rank above
    # content matches, whatever the control type.
    if best_label == "value":
        score -= 0.12
        best_reason += " (matched on transient value, not on identity)"
    score += _control_type_bonus(query, element)
    if screen is not None:
        score += _positional_bonus(query, element, screen)

    reasons = [best_reason]

    # Caller intent ------------------------------------------------------
    # Two different controls can share a name (Spotify has a "Search" button in
    # its toolbar AND a "Search" edit field in its sidebar).  The caller states
    # which kind of control it needs, so the right one wins.
    if prefer_types:
        if element.control_type in prefer_types:
            score += 0.22
            reasons.append(f"{element.control_type} can take typed text")
        else:
            score -= 0.30
            reasons.append(f"{element.control_type} cannot take typed text")
    if require_pattern:
        patterns = {p.lower() for p in (element.patterns or ())}
        if require_pattern.lower() in patterns:
            score += 0.10
            reasons.append(f"supports {require_pattern}")
        else:
            score -= 0.30
            reasons.append(f"does not support {require_pattern}")

    # Penalties ----------------------------------------------------------
    if not element.is_enabled:
        score -= 0.35
        reasons.append("control is disabled")
    if not element.is_visible:
        score -= 0.45
        reasons.append("control is off-screen")
    if element.is_password:
        # Still clickable (JARVIS may be asked to focus the field) but never
        # readable, and never chosen when a non-password alternative exists.
        score -= 0.25
        reasons.append("password field (contents are never read)")
    if element.rect.width <= 0 or element.rect.height <= 0:
        score -= 0.4
        reasons.append("no bounding box")
    if element.is_focused:
        # A small nudge only: the control JARVIS just typed into is focused, and
        # that must not outweigh the control whose *name* was asked for.
        score += 0.03
        reasons.append("already focused")
    # Prefer semantically rich controls over raw text when both match.
    if element.control_type in ("Text", "Image"):
        score -= 0.06
    else:
        score += 0.04
    # Larger interactive controls are more likely to be the real target.
    area = element.rect.width * element.rect.height
    if 0 < area < 400:
        score -= 0.05

    return ElementMatch(element=element, score=round(max(0.0, min(1.2, score)), 4),
                        reason="; ".join(reasons), matched_text=best_text)


def match_elements(query: str, elements: Sequence[UIElement],
                   screen: Optional[Rect] = None,
                   min_score: float = 0.45,
                   limit: int = 8,
                   prefer_types: Sequence[str] = (),
                   require_pattern: Optional[str] = None) -> List[ElementMatch]:
    """Rank every element against ``query``."""
    matches: List[ElementMatch] = []
    for el in elements or []:
        m = score_element(query, el, screen, prefer_types=prefer_types,
                          require_pattern=require_pattern)
        if m is not None and m.score >= min_score:
            matches.append(m)
    matches.sort(key=lambda m: (-m.score, m.element.rect.y, m.element.rect.x))
    matches = _ordinal_pick(query, matches)
    return matches[:limit]


def ocr_words_to_elements(words: Sequence[OcrWord]) -> List[UIElement]:
    """Turn OCR words into pseudo-elements (used when UIA is unavailable)."""
    out: List[UIElement] = []
    for w in words or []:
        text = (w.text or "").strip()
        if not text:
            continue
        out.append(UIElement(name=text[:120], control_type="Text", rect=w.rect,
                             value=text[:200], source="ocr",
                             patterns=["invoke"]))
    return out


def describe_elements(matches: Sequence[ElementMatch], limit: int = 5) -> str:
    if not matches:
        return "No matching interface element was found."
    lines = []
    for m in matches[:limit]:
        el = m.element
        lines.append(
            f"{el.control_type or 'Element'} \"{el.name or el.value}\" at "
            f"({el.center.x},{el.center.y}) size {el.rect.width}x{el.rect.height}"
            f" score={m.score:.2f} [{m.reason}]")
    return "\n".join(lines)


def summarise_elements(elements: Sequence[UIElement], limit: int = 40,
                       max_name: int = 60) -> List[Dict[str, Any]]:
    """Compact element list for the model context (interactive controls first)."""
    interactive = ("Button", "Edit", "CheckBox", "ComboBox", "ListItem",
                   "MenuItem", "TabItem", "Hyperlink", "Slider", "RadioButton",
                   "ToggleButton", "SplitButton", "Search", "Document")
    ranked = sorted(
        elements or [],
        key=lambda e: (0 if (e.control_type in interactive) else 1,
                       -int(e.is_enabled), e.rect.y, e.rect.x))
    out = []
    for el in ranked[:limit]:
        d = {
            "type": el.control_type or "?",
            "name": (el.name or "")[:max_name],
            "id": (el.automation_id or "")[:30],
            "at": [el.center.x, el.center.y],
            "size": [el.rect.width, el.rect.height],
            "src": el.source,
        }
        if el.is_password:
            d["value"] = "[PASSWORD-FIELD-MASKED]"
        elif el.value:
            d["value"] = el.value[:80]
        if el.is_focused:
            d["focused"] = True
        if not el.is_enabled:
            d["disabled"] = True
        out.append(d)
    return out
