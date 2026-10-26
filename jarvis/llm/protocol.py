"""The structured action protocol.

Native Ollama tool calling works well on ``qwen2.5``, ``llama3.1+``,
``mistral-nemo`` and friends - but a large share of the models people actually
have installed locally (``llama3.2:3b``, ``phi3``, ``gemma2``, older ``llava``)
either ignore the ``tools`` field or emit malformed arguments.

JARVIS therefore supports two interchangeable protocols:

``native``  the tool schema is passed to ``/api/chat`` and the model returns
            ``message.tool_calls``.
``prompted`` the same schema is rendered into the system prompt and the model
             must answer with a single JSON object.

:class:`jarvis.core.agent.AgentLoop` picks ``native`` when the selected model
advertises tool support and silently falls back to ``prompted`` the first time
a native call comes back empty or unparseable.  The parsing here is deliberately
tolerant: fences, prose before/after the JSON, single quotes, trailing commas
and ``arguments`` serialised as a string are all handled.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*([\s\S]*?)```")


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
def extract_json(text: str) -> Optional[Any]:
    """Pull the first JSON value out of a model reply.  Returns None on failure."""
    if not text:
        return None
    text = text.strip()

    # 1. plain parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. fenced block
    for m in _FENCE_RE.finditer(text):
        candidate = m.group(1).strip()
        parsed = _try_parse(candidate)
        if parsed is not None:
            return parsed

    # 3. first balanced object / array in the raw text
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            obj = _balanced(text, start, opener, closer)
            if obj:
                parsed = _try_parse(obj)
                if parsed is not None:
                    return parsed
                # also try the *last* balanced span starting here
            start = text.find(opener, start + 1)
            if start > 20000:
                break

    # 4. last-resort: greedy substring between the first { and the last }
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        parsed = _try_parse(text[first:last + 1])
        if parsed is not None:
            return parsed
    return None


def _balanced(text: str, start: int, opener: str, closer: str) -> Optional[str]:
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _try_parse(candidate: str) -> Optional[Any]:
    candidate = candidate.strip()
    if not candidate:
        return None
    for attempt in (candidate, _repair(candidate)):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            continue
    return None


def _repair(text: str) -> str:
    """Cheap, well-understood JSON repairs (never guesses at semantics)."""
    out = text
    out = re.sub(r",\s*([}\]])", r"\1", out)          # trailing commas
    out = re.sub(r"//[^\n]*", "", out)                # line comments
    out = re.sub(r"^\s*`+\s*", "", out)               # stray fences
    out = re.sub(r"\s*`+\s*$", "", out)
    out = out.replace("\r\n", "\n")
    # Python literals -> JSON literals
    out = re.sub(r"\bTrue\b", "true", out)
    out = re.sub(r"\bFalse\b", "false", out)
    out = re.sub(r"\bNone\b", "null", out)
    # Unclosed trailing string / brace.
    if out.count("{") > out.count("}"):
        out += "}" * (out.count("{") - out.count("}"))
    if out.count("[") > out.count("]"):
        out += "]" * (out.count("[") - out.count("]"))
    return out


def coerce_arguments(raw: Any) -> Dict[str, Any]:
    """``arguments`` may arrive as a dict, a JSON string, or a list of pairs."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        parsed = extract_json(raw)
        if isinstance(parsed, dict):
            return parsed
        if parsed is not None:
            return {"value": parsed}
        return {"text": raw}
    if isinstance(raw, (list, tuple)):
        out: Dict[str, Any] = {}
        for item in raw:
            if isinstance(item, dict):
                out.update(item)
        return out
    return {"value": raw}


# ---------------------------------------------------------------------------
# Prompted protocol
# ---------------------------------------------------------------------------
ACTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {"type": "string",
                    "description": "One short sentence on what you just learned "
                                   "and what you will do next."},
        "action": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact tool name."},
                "arguments": {"type": "object",
                              "description": "Tool arguments as key/value pairs."},
            },
            "required": ["name", "arguments"],
        },
        "answer": {"type": "string",
                   "description": "FINAL reply to the user. Only set this when "
                                  "the task is finished or you must give up. "
                                  "Leave empty when you are calling a tool."},
        "speak": {"type": "string",
                  "description": "Optional short spoken progress note "
                                 "(max ~8 words). Leave empty usually."},
    },
    "required": ["thought"],
}


def render_tools_for_prompt(tools: Sequence[Dict[str, Any]]) -> str:
    """Compact, token-cheap rendering of the tool catalogue."""
    lines: List[str] = []
    for t in tools:
        fn = t.get("function", t)
        name = fn.get("name", "?")
        desc = (fn.get("description") or "").strip().split("\n")[0]
        params = (fn.get("parameters") or {}).get("properties") or {}
        required = set((fn.get("parameters") or {}).get("required") or [])
        if not params:
            lines.append(f"- {name}(): {desc}")
            continue
        rendered = []
        for pname, spec in params.items():
            ptype = spec.get("type", "any")
            if ptype == "string" and spec.get("enum"):
                ptype = "|".join(str(e) for e in spec["enum"])
            mark = "" if pname in required else "?"
            pdesc = spec.get("description", "")
            rendered.append(f"{pname}{mark}:{ptype}" + (f" ({pdesc})" if pdesc else ""))
        lines.append(f"- {name}({', '.join(rendered)}): {desc}")
    return "\n".join(lines)


PROMPTED_PROTOCOL_HEADER = """\
You control the computer by emitting ONE JSON object per reply.

Reply format (and nothing else - no prose outside the JSON, no markdown fences):
{"thought": "...", "action": {"name": "tool_name", "arguments": {...}}, "answer": "", "speak": ""}

Rules:
- Emit exactly ONE action per reply, then wait for its result.
- "answer" is only used on your FINAL reply, when the task is complete or you
  genuinely cannot continue. When you set "answer", do not set "action".
- Never invent tool names or arguments that are not listed below.
- Never claim success you have not verified with an observation tool.
- Keep "thought" to one sentence. Never include passwords or secrets.
"""


def build_prompted_system_prompt(base_prompt: str, tools: Sequence[Dict[str, Any]]) -> str:
    return (
        base_prompt.rstrip()
        + "\n\n" + PROMPTED_PROTOCOL_HEADER
        + "\nAvailable tools:\n" + render_tools_for_prompt(tools)
    )


def parse_agent_reply(text: str) -> Dict[str, Any]:
    """Normalise a prompted-protocol reply into a uniform action dict.

    Returns ``{"thought", "action"|"answer", "speak", "raw", "parse_error"}``.
    """
    out: Dict[str, Any] = {"thought": "", "action": None, "answer": "",
                           "speak": "", "raw": text, "parse_error": ""}
    data = extract_json(text)
    if not isinstance(data, dict):
        # Some small models answer in prose.  Treat that as a final answer.
        out["answer"] = (text or "").strip()
        out["parse_error"] = "no-json"
        return out

    out["thought"] = str(data.get("thought") or data.get("reasoning") or "").strip()
    out["speak"] = str(data.get("speak") or data.get("say") or "").strip()

    answer = data.get("answer")
    if answer is None:
        answer = data.get("final_answer") or data.get("response") or data.get("reply")
    if isinstance(answer, str) and answer.strip():
        out["answer"] = answer.strip()

    action = data.get("action") or data.get("tool_call") or data.get("tool")
    if isinstance(action, str):
        action = {"name": action, "arguments": {}}
    if isinstance(action, list) and action:
        action = action[0]
    if isinstance(action, dict):
        name = action.get("name") or action.get("tool") or action.get("function")
        if isinstance(name, dict):
            name = name.get("name")
        args = coerce_arguments(action.get("arguments") or action.get("args")
                                or action.get("parameters") or action.get("input"))
        if name:
            out["action"] = {"name": str(name).strip(), "arguments": args}
    elif data.get("name") and not out["answer"]:
        # Flat shape: {"name": "...", "arguments": {...}}
        out["action"] = {"name": str(data["name"]).strip(),
                         "arguments": coerce_arguments(data.get("arguments"))}

    if not out["action"] and not out["answer"]:
        out["answer"] = out["thought"] or (text or "").strip()
        out["parse_error"] = "no-action-no-answer"
    return out


def native_tool_calls_to_actions(result_tool_calls: Sequence[Dict[str, Any]]
                                 ) -> List[Dict[str, Any]]:
    """Normalise native ``message.tool_calls`` entries."""
    out = []
    for tc in result_tool_calls or []:
        name = tc.get("name") or (tc.get("function") or {}).get("name")
        args = tc.get("arguments")
        if args is None:
            args = (tc.get("function") or {}).get("arguments")
        if not name:
            continue
        out.append({"name": str(name).strip(), "arguments": coerce_arguments(args)})
    return out


def tool_schema_for(specs: Sequence[Any]) -> List[Dict[str, Any]]:
    """Convert internal ToolSpec objects into Ollama's tool schema format."""
    out = []
    for s in specs:
        out.append({
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters or {"type": "object", "properties": {}},
            },
        })
    return out
