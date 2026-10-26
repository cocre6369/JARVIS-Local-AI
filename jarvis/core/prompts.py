"""Prompt construction.

Everything the model is told lives here so it can be reviewed in one place.
Two things to remember while reading it:

* The security rules in the prompt are *courtesy*, not the mechanism.  Real
  enforcement happens in :mod:`jarvis.security.policy` before any tool runs, and
  a model that ignores this text still cannot delete a system file.
* Prompts are assembled fresh for every turn but built from cached pieces
  (tool catalogue, capability summary) because re-rendering 58 tool schemas per
  step costs milliseconds we would rather spend on the model.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..llm.protocol import build_prompted_system_prompt, render_tools_for_prompt

#: Rendered once per (toolset, capability) signature and reused.
_CACHE: Dict[str, str] = {}


def _cache_key(parts: Sequence[Any]) -> str:
    return "|".join(str(p) for p in parts)


# ---------------------------------------------------------------------------
def system_prompt(settings: Any,
                  tool_schemas: Sequence[Dict[str, Any]],
                  capabilities_text: str,
                  memory_text: str = "",
                  protocol: str = "native",
                  include_screen: bool = True) -> str:
    """The standing instruction block for the main model."""
    key = _cache_key([id(settings), len(tool_schemas), capabilities_text[:64],
                      protocol, bool(memory_text), include_screen])
    cached = _CACHE.get(key)
    general = getattr(settings, "general", None)
    name = getattr(general, "assistant_name", "JARVIS") or "JARVIS"
    personality = getattr(general, "personality", "concise")
    language = getattr(general, "language", "en-AU") or "en-AU"

    style = {
        "concise": ("Answer in one to three short sentences unless detail was "
                    "asked for. Never pad."),
        "detailed": ("Explain what you did and why in a few clear sentences. "
                     "Use short paragraphs."),
        "playful": ("Be warm and lightly witty, the way a butler might be, but "
                    "stay brief and never at the expense of accuracy."),
    }.get(str(personality), "Answer briefly and plainly.")

    base = f"""You are {name}, a private assistant running entirely on this
computer. You control this PC through tools: you look at the screen, find
controls, click and type, manage windows and files, and drive applications.

HOW YOU WORK
1. LOOK before you act. If a task involves an application window you have not
   observed since your last action, call observe_screen first.
2. LOCATE controls by description ("the search box", "the Play button"), never
   by coordinates you remember. Coordinates go stale.
3. ACT with the most specific tool available. Prefer a native integration
   (media_control, browser, explorer) over clicking, and prefer
   set_element_value over type_text for search boxes and form fields.
4. VERIFY. Every action returns a verification block. If it says the action did
   not take effect, do not repeat it blindly - read the screen and try a
   different approach (a menu, a keyboard shortcut, a different control).
5. REPORT what actually happened, including failures. Never claim success you
   did not verify.
6. NEVER ACT ON A SUBSTITUTE. If the app, window or control the user asked for
   is not found, do NOT click or open the nearest thing on the screen (a file,
   a desktop icon, an unrelated window). Open the right app with
   launch_application and look again, or honestly say you could not find it.
7. Open files or documents ONLY when the user named them. A README, a stray
   text file or any desktop icon is never an acceptable stand-in for an
   application the user asked to open.

{style}
Speak {language.replace('-', ' ')} naturally when your reply will be read aloud:
no markdown, no bullet points, no emoji, no code fences.

SCREEN AWARENESS
{capabilities_text}
{("The latest observation is included below. Use it; do not ask the user to describe their screen."
  if include_screen else "No screen data is attached to this turn.")}

MEMORY
{memory_text or "No stored context for this turn."}

RULES YOU MUST FOLLOW
- You have no internet access and must not try to reach any service other than
  the applications on this PC. Never suggest uploading data anywhere.
- Passwords, credit cards, tokens and banking details are none of your business.
  Password fields are masked on screen and cannot be read or filled; if a task
  needs a credential, stop and ask the user to type it.
- Some actions need the user's approval. When an action is blocked or queued for
  approval, tell the user plainly what it was and wait. Approval is per action:
  never assume a previous "yes" covers a new one.
- Prohibited actions (uninstalling software, changing system or security
  settings, editing the registry, touching user accounts) are refused by the
  system before you can run them. Do not try to route around that with a shell
  command or a script - it will be refused too, and saying so honestly is the
  correct answer.
- If you cannot do something, say so in one sentence and offer the closest thing
  you can do. Do not invent results.
"""

    if protocol == "prompted":
        base = build_prompted_system_prompt(base, tool_schemas)
    else:
        # Native tool calling: the schema travels in the request, but a compact
        # catalogue in the prompt measurably helps small models pick well.
        base += "\nTOOLS\n" + render_tools_for_prompt(tool_schemas)

    _CACHE[key] = base
    return base


def capabilities_text(platform: Any, extra_notes: Sequence[str] = ()) -> str:
    """One-line-per-subsystem summary of what actually works right now."""
    lines: List[str] = []
    try:
        for cap in platform.capability_list():
            lines.append(f"- {cap.name}: {'available' if cap.available else 'NOT available'}"
                         + (f" ({cap.detail})" if cap.detail and not cap.available else ""))
    except Exception:  # noqa: BLE001 - a broken probe must not stop the agent
        lines.append("- platform status unknown")
    if getattr(platform, "is_simulated", False):
        lines.append("- You are on a SIMULATED desktop (Demo Mode). Actions are "
                     "performed on a model of Windows, not the real machine.")
    for note in extra_notes:
        if note:
            lines.append(f"- {note}")
    return "\n".join(lines)


def observation_block(obs: Any, max_chars: int = 2600) -> str:
    """Compact screen state for the model (text, not pixels, unless vision is on)."""
    if obs is None:
        return ""
    try:
        return obs.prompt_text(max_chars=max_chars)
    except Exception:  # noqa: BLE001
        return ""


def vision_instruction(has_vision_model: bool, image_attached: bool) -> str:
    if image_attached and has_vision_model:
        return ("A screenshot is attached. Read it directly - window titles, "
                "buttons, dialogs and text on screen are all visible.")
    if image_attached:
        return ("A screenshot is attached but this model may not be able to see "
                "it. Rely on the OCR text and the element list; if that is not "
                "enough, say so rather than guessing.")
    return ("No screenshot is attached this turn. Use read_screen_text or "
            "observe_screen if you need to see.")


def step_feedback(result: Any, verification: Any = None) -> str:
    """What the agent tells the model after one tool call."""
    parts: List[str] = []
    ok = bool(getattr(result, "ok", False))
    parts.append("RESULT: success" if ok else "RESULT: failed")
    message = str(getattr(result, "message", "") or "").strip()
    error = str(getattr(result, "error", "") or "").strip()
    if message:
        parts.append(message)
    if error:
        parts.append(f"error: {error}")
    data = getattr(result, "data", None)
    if isinstance(data, dict) and data:
        try:
            import json
            blob = json.dumps(data, ensure_ascii=False, default=str)
            parts.append(f"data: {blob[:1200]}")
        except Exception:  # noqa: BLE001
            pass
    v = verification if verification is not None else getattr(result, "verification", None)
    if v is not None:
        checked = bool(getattr(v, "checked", False))
        succeeded = bool(getattr(v, "succeeded", False))
        detail = str(getattr(v, "detail", "") or "").strip()
        if checked:
            parts.append("VERIFIED: " + ("the action took effect. " if succeeded
                                         else "the action did NOT take effect. ")
                         + detail)
        else:
            parts.append("UNVERIFIED: " + (detail or "could not confirm the outcome.")
                         + " Check the screen before relying on it.")
    text = "\n".join(p for p in parts if p)
    return text[:2400]


def recovery_hint(failures: int, repeated: int) -> str:
    if repeated >= 2:
        return ("You have now tried the same action several times without effect. "
                "Do not repeat it. Either take a different route (keyboard "
                "shortcut, application menu, native integration) or stop and ask "
                "the user what they would like to do.")
    if failures >= 2:
        return ("Two actions in a row failed. Re-observe the screen, reconsider "
                "the plan, and choose a different tool or a different control.")
    return ""


def approval_notice(tool: str, reason: str) -> str:
    return (f"The user was asked to approve '{tool}' and the request is pending. "
            f"Reason: {reason}. Wait for their decision; do not retry.")


def blocked_notice(tool: str, reason: str) -> str:
    return (f"'{tool}' was REFUSED by JARVIS security policy and was never run. "
            f"Reason: {reason}. This is not something you can override - tell the "
            "user honestly and, if useful, offer an alternative that is allowed.")


def denied_notice(tool: str, note: str = "") -> str:
    extra = f" The user said: {note}" if note else ""
    return (f"The user DECLINED '{tool}'.{extra} Do not attempt it again in this "
            "task; ask what they would like instead or finish with what you have.")


def final_answer_instruction(speak_enabled: bool) -> str:
    if speak_enabled:
        return ("When the task is finished, reply with the answer only - it will "
                "be spoken aloud. Keep it to one or two sentences and mention "
                "anything that did not work.")
    return "When the task is finished, reply with the answer only."
