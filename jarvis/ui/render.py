"""Qt-free rendering helpers for the conversation panel.

The widgets are thin: the HTML for each line is built here so it can be unit
tested without a display server, and so the CLI can reuse the same text
formatting rules.
"""

from __future__ import annotations

import html
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

ROLE_LABELS = {
    "user": "You",
    "assistant": "JARVIS",
    "system": "System",
    "tool": "Tool",
    "thought": "Reasoning",
}

ROLE_COLOUR_KEYS = {
    "user": "accent",
    "assistant": "text",
    "system": "warning",
    "tool": "text_dim",
    "thought": "text_muted",
}

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def escape(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def timestamp_label(ts: Optional[float] = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts or time.time()))


def inline_format(text: str) -> str:
    """Escape, then re-apply the two bits of markdown people actually type."""
    out = escape(text)
    out = _INLINE_CODE.sub(r"<span style='font-family:monospace'>\1</span>", out)
    out = _BOLD.sub(r"<b>\1</b>", out)
    return out.replace("\n", "<br/>")


def render_line(role: str, text: str, palette: Dict[str, str],
                ts: Optional[float] = None, task_id: str = "",
                streaming: bool = False) -> str:
    """One conversation bubble as an HTML fragment."""
    label = ROLE_LABELS.get(role, role.title())
    colour = palette.get(ROLE_COLOUR_KEYS.get(role, "text"), palette.get("text", "#fff"))
    dim = palette.get("text_muted", "#5d7288")
    body = inline_format(text)
    if streaming:
        body += f"<span style='color:{dim}'>▌</span>"
    when = timestamp_label(ts)
    return (
        f"<div style='margin:6px 0'>"
        f"<span style='color:{colour};font-weight:600'>{escape(label)}</span>"
        f"<span style='color:{dim};font-size:9pt'> &nbsp;{when}</span>"
        f"<div style='color:{palette.get('text', '#e6f1ff')};margin-top:2px'>{body}</div>"
        f"</div>"
    )


def render_tool_line(tool: str, ok: Optional[bool], message: str,
                     verified: Optional[bool], detail: str,
                     palette: Dict[str, str], duration_ms: float = 0.0) -> str:
    """A compact tool-trace line for the conversation panel."""
    if ok is None:
        mark, colour_key = "○", "text_dim"
    elif ok:
        mark, colour_key = ("✓", "success") if verified is not False else ("•", "info")
    else:
        mark, colour_key = "✗", "danger"
    colour = palette.get(colour_key, palette.get("text_dim", "#8ea3b8"))
    dim = palette.get("text_muted", "#5d7288")
    parts = [f"<span style='color:{colour}'>{mark}</span>",
             f"<b>{escape(tool)}</b>"]
    if message:
        parts.append(escape(message[:220]))
    if verified is True and detail:
        parts.append(f"<span style='color:{palette.get('success', '#34d399')}'>"
                     f"verified: {escape(detail[:160])}</span>")
    elif verified is False and detail:
        parts.append(f"<span style='color:{palette.get('warning', '#fbbf24')}'>"
                     f"not verified: {escape(detail[:160])}</span>")
    elif detail:
        parts.append(f"<span style='color:{dim}'>{escape(detail[:160])}</span>")
    if duration_ms:
        parts.append(f"<span style='color:{dim}'>{duration_ms:.0f} ms</span>")
    return (f"<div style='margin:2px 0 2px 10px;color:{dim};font-size:9.5pt'>"
            + " &nbsp; ".join(parts) + "</div>")


def render_notice(title: str, message: str, palette: Dict[str, str],
                  kind: str = "info") -> str:
    colour_key = {"info": "info", "warn": "warning", "danger": "danger",
                  "ok": "success"}.get(kind, "info")
    colour = palette.get(colour_key, palette.get("accent", "#22d3ee"))
    return (f"<div style='margin:6px 0;padding:6px 10px;border-left:2px solid {colour};"
            f"background-color:{palette.get('bg_raised', '#0d1520')}'>"
            f"<b style='color:{colour}'>{escape(title)}</b>"
            f"<div style='color:{palette.get('text_dim', '#8ea3b8')}'>"
            f"{inline_format(message)}</div></div>")


def approval_text(request: Dict[str, Any]) -> str:
    """Plain-text body of an approval dialog (also used by the CLI)."""
    decision = request.get("decision") or {}
    lines: List[str] = [
        f"JARVIS wants to: {decision.get('reason') or request.get('tool', 'an action')}",
        f"Tool: {request.get('tool', '?')}",
        f"Target: {decision.get('target') or '(not specified)'}",
        f"Category: {decision.get('category') or 'general'}",
        f"Consequences: {decision.get('consequences') or 'see above'}",
        f"Reversible: {'yes' if decision.get('reversible', True) else 'no - this cannot be undone'}",
        f"Rule: {decision.get('rule') or ''}",
    ]
    args = request.get("args") or {}
    if args:
        rendered = ", ".join(f"{k}={str(v)[:80]}" for k, v in list(args.items())[:6])
        lines.append(f"Arguments: {rendered}")
    return "\n".join(line for line in lines if line.strip())


def question_text(request: Dict[str, Any]) -> str:
    options = request.get("options") or []
    text = request.get("question") or ""
    if options:
        text += "\n\nOptions: " + "; ".join(str(o) for o in options[:6])
    return text


def format_bytes(value: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def metric_line(label: str, value: str, budget: str = "") -> str:
    return f"{label} {value}" + (f" / {budget}" if budget else "")


def truncate(text: Any, limit: int = 200) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def tool_trace_rows(steps: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalise agent steps for the trace table."""
    rows: List[Dict[str, Any]] = []
    for step in steps or ():
        rows.append({
            "index": step.get("index"),
            "tool": step.get("tool", ""),
            "ok": step.get("ok"),
            "verified": step.get("verified"),
            "message": truncate(step.get("message", ""), 200),
            "detail": truncate(step.get("verify_detail", ""), 200),
            "duration_ms": step.get("duration_ms", 0.0),
            "risk": step.get("risk", ""),
            "approval": step.get("approval", ""),
            "thought": truncate(step.get("thought", ""), 200),
        })
    return rows


def capability_rows(summary: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalise platform capabilities for the Automation page table."""
    rows: List[Dict[str, Any]] = []
    for item in summary or ():
        rows.append({"name": item.get("name", ""),
                     "available": bool(item.get("available")),
                     "backend": item.get("backend", ""),
                     "detail": truncate(item.get("detail", ""), 220)})
    return rows
