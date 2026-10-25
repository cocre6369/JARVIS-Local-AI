"""Tool registry.

The registry holds every :class:`~jarvis.tools.base.Tool`, renders the JSON
schema handed to Ollama and executes calls.  It deliberately does **not** make
permission decisions - that is :class:`~jarvis.security.policy.PolicyEngine`'s
job, invoked by the agent loop between "the model asked for this" and "the
registry ran it".
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .base import Tool, ToolContext, ToolResult, ToolSpec

log = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self._tools: Dict[str, Tool] = {}
        self._order: List[str] = []
        self._lock = threading.RLock()
        self._availability_cache: Dict[str, tuple] = {}
        self._availability_ts: float = 0.0

    # ------------------------------------------------------------------
    def register(self, tool: Tool) -> Tool:
        with self._lock:
            name = tool.spec.name
            if name in self._tools:
                log.warning("Replacing already-registered tool %s", name)
            else:
                self._order.append(name)
            self._tools[name] = tool
        return tool

    def register_many(self, tools: Iterable[Tool]) -> None:
        for t in tools:
            self.register(t)

    def get(self, name: str) -> Optional[Tool]:
        with self._lock:
            return self._tools.get((name or "").strip())

    def names(self, include_hidden: bool = False) -> List[str]:
        with self._lock:
            return [n for n in self._order
                    if include_hidden or not self._tools[n].spec.hidden]

    def all(self) -> List[Tool]:
        with self._lock:
            return [self._tools[n] for n in self._order]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return str(name) in self._tools

    # ------------------------------------------------------------------
    def availability(self, max_age_s: float = 30.0) -> Dict[str, tuple]:
        """Cached capability probe so the schema is not rebuilt every turn."""
        import time

        now = time.time()
        with self._lock:
            if self._availability_cache and now - self._availability_ts < max_age_s:
                return dict(self._availability_cache)
        out: Dict[str, tuple] = {}
        for tool in self.all():
            try:
                out[tool.name] = tool.available()
            except Exception as exc:  # noqa: BLE001
                out[tool.name] = (False, str(exc))
        with self._lock:
            self._availability_cache = out
            self._availability_ts = now
        return out

    def invalidate_availability(self) -> None:
        with self._lock:
            self._availability_cache.clear()
            self._availability_ts = 0.0

    # ------------------------------------------------------------------
    def visible_specs(self, only_available: bool = True) -> List[ToolSpec]:
        avail = self.availability() if only_available else {}
        out: List[ToolSpec] = []
        for tool in self.all():
            if tool.spec.hidden:
                continue
            if only_available:
                ok, _why = avail.get(tool.name, (True, ""))
                if not ok:
                    continue
            out.append(tool.spec)
        return out

    def schema(self, only_available: bool = True) -> List[Dict[str, Any]]:
        """Ollama ``tools`` payload."""
        return [s.as_schema() for s in self.visible_specs(only_available)]

    def prompt_catalogue(self, only_available: bool = True) -> str:
        from ..llm.protocol import render_tools_for_prompt

        return render_tools_for_prompt(self.schema(only_available))

    def unavailable_report(self) -> List[Dict[str, str]]:
        out = []
        for name, (ok, why) in sorted(self.availability().items()):
            if not ok:
                out.append({"tool": name, "reason": why})
        return out

    def by_category(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for tool in self.all():
            out.setdefault(tool.spec.category, []).append(tool.name)
        return {k: sorted(v) for k, v in sorted(out.items())}

    # ------------------------------------------------------------------
    def execute(self, name: str, args: Optional[Dict[str, Any]] = None) -> ToolResult:
        """Execute a tool that has already passed the policy check."""
        tool = self.get(name)
        if tool is None:
            known = ", ".join(self.names()[:12])
            return ToolResult(
                ok=False,
                error=(f"Unknown tool '{name}'. Available tools include: {known}. "
                       "Use only tools from the provided list."))
        args = dict(args or {})
        args = _coerce_arguments(tool.spec, args)
        return tool.execute(**args)

    def preview(self, name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """What the user would be asked to approve (used by the UI dialog)."""
        tool = self.get(name)
        if tool is None:
            return {"tool": name, "known": False}
        spec = tool.spec
        ok, why = tool.available()
        return {
            "tool": name, "known": True, "description": spec.description,
            "category": spec.category, "risk": spec.risk.value,
            "reversible": spec.reversible, "available": ok,
            "availability_reason": why, "arguments": dict(args or {}),
        }


# ---------------------------------------------------------------------------
def _coerce_arguments(spec: ToolSpec, args: Dict[str, Any]) -> Dict[str, Any]:
    """Models routinely send ``"3"`` where an int is expected.  Coerce using the
    declared schema instead of failing the call, and drop unknown keys."""
    props = ((spec.parameters or {}).get("properties") or {})
    if not props:
        return args
    out: Dict[str, Any] = {}
    for key, value in args.items():
        target = props.get(key)
        if target is None:
            # tolerate a couple of common aliases
            alias = _alias_for(key, props)
            if alias is None:
                log.debug("Dropping unknown argument %s for %s", key, spec.name)
                continue
            key, target = alias, props[alias]
        out[key] = _coerce_value(value, target)
    return out


def _alias_for(key: str, props: Dict[str, Any]) -> Optional[str]:
    k = key.lower().replace("-", "_")
    for name in props:
        if name.lower() == k:
            return name
    synonyms = {
        "x_pos": "x", "y_pos": "y", "pos_x": "x", "pos_y": "y",
        "text_to_type": "text", "value": "text", "string": "text",
        "app": "name", "application": "name", "app_name": "name",
        "program": "name", "window": "title", "window_title": "title",
        "element": "description", "target": "description", "label": "description",
        "seconds": "duration_s", "timeout": "timeout_s", "keys": "key_combo",
        "key": "key_combo", "shortcut": "key_combo", "folder": "path",
        "directory": "path", "file": "path", "filepath": "path",
        "file_path": "path", "url": "address", "link": "address",
        "query": "description", "amount": "clicks",
    }
    return synonyms.get(k)


def _coerce_value(value: Any, schema: Dict[str, Any]) -> Any:
    if value is None:
        return None
    t = (schema or {}).get("type", "")
    if isinstance(t, list):
        t = t[0] if t else ""
    try:
        if t == "integer":
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (int, float)):
                return int(value)
            s = str(value).strip()
            if s.lower() in ("true", "false"):
                return int(s.lower() == "true")
            return int(float(re_first_number(s)))
        if t == "number":
            return float(re_first_number(str(value)))
        if t == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "y", "on")
            return bool(value)
        if t == "array":
            if isinstance(value, (list, tuple)):
                return list(value)
            if isinstance(value, str):
                return [p.strip() for p in value.split(",") if p.strip()]
            return [value]
        if t == "string":
            if isinstance(value, str):
                return value
            if isinstance(value, (int, float, bool)):
                return str(value)
            return str(value)
    except Exception as exc:  # noqa: BLE001
        log.debug("argument coercion failed for %r -> %s: %s", value, t, exc)
    return value


def re_first_number(text: str) -> str:
    import re

    m = re.search(r"-?\d+(?:\.\d+)?", text or "")
    return m.group(0) if m else "0"


def build_default_registry(ctx: ToolContext) -> ToolRegistry:
    """Instantiate the full tool set."""
    from . import (assistant_tools, file_tools, input_tools, media_tools,
                   system_tools, vision_tools, web_tools, window_tools)

    registry = ToolRegistry(ctx)
    for module in (vision_tools, window_tools, input_tools, file_tools,
                   system_tools, media_tools, web_tools, assistant_tools):
        registry.register_many(module.build(ctx))
    log.info("Registered %d tools (%d categories)", len(registry),
             len(registry.by_category()))
    return registry
