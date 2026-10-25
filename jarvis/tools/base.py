"""Tool primitives.

Every capability JARVIS can exercise on the computer is a :class:`Tool`.  A tool
declares:

* a JSON-schema so Ollama can call it,
* a **category** and a **baseline risk** used by the security layer,
* a :meth:`Tool.run` implementation, and
* a :meth:`Tool.verify` hook that produces *evidence* the action worked.

Verification is not optional decoration: the agent loop feeds
``Verification.succeeded`` back into the model's next decision, which is how
JARVIS avoids the classic failure mode of clicking a button, assuming it worked
and then typing into the wrong place.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..security.policy import Risk

log = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    #: read_only | navigation | input | file_read | file_write | file_delete |
    #: file_move | file_create | application | system | communication |
    #: download | media | shell | memory | query
    category: str = "general"
    #: Code-defined baseline.  The policy engine can only ever tighten it.
    risk: Risk = Risk.ALLOWED
    reversible: bool = True
    #: Subsystems this tool needs (matched against platform capabilities).
    requires: Sequence[str] = ()
    #: Approximate cost - used to shed load under memory pressure.
    cost: str = "low"          # low | medium | high
    #: Hidden tools are not offered to the model (UI-only or internal).
    hidden: bool = False
    examples: List[Dict[str, Any]] = field(default_factory=list)

    def as_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


@dataclass
class Verification:
    checked: bool = False
    succeeded: bool = False
    detail: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"checked": self.checked, "succeeded": self.succeeded,
                "detail": self.detail, "evidence": self.evidence}


@dataclass
class ToolResult:
    ok: bool
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    duration_ms: float = 0.0
    verification: Optional[Verification] = None
    #: Set when the tool wants the agent to look at the screen afterwards.
    suggest_observe: bool = False
    screenshot_path: str = ""

    def model_text(self, max_chars: int = 2600) -> str:
        """Compact, model-facing serialisation (goes into the tool message)."""
        import json

        payload: Dict[str, Any] = {"ok": self.ok}
        if self.message:
            payload["message"] = self.message
        if self.error:
            payload["error"] = self.error
        if self.data:
            payload["data"] = self.data
        if self.verification is not None and self.verification.checked:
            payload["verification"] = {
                "succeeded": self.verification.succeeded,
                "detail": self.verification.detail,
            }
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) > max_chars:
            # Truncate but keep it valid-ish JSON so the model is not confused.
            text = text[: max_chars - 24].rstrip().rstrip(",") + ', "truncated": true}'
        return text

    def as_event(self) -> Dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "error": self.error,
                "data": self.data, "duration_ms": round(self.duration_ms, 1),
                "verification": (self.verification.as_dict()
                                 if self.verification else None),
                "screenshot_path": self.screenshot_path}


class ToolContext:
    """Everything a tool may need.  Populated by the application bootstrap.

    Kept as a plain object (not a dataclass) so optional collaborators can be
    attached after construction without circular imports.
    """

    def __init__(self,
                 settings_provider: Callable[[], Any],
                 platform: Any,
                 bus: Any,
                 policy: Any = None,
                 audit: Any = None,
                 guard: Any = None,
                 approvals: Any = None,
                 memory: Any = None,
                 vision: Any = None,
                 ollama: Any = None,
                 router: Any = None,
                 speech: Any = None,
                 integrations: Optional[Dict[str, Any]] = None) -> None:
        self.settings_provider = settings_provider
        self.platform = platform
        self.bus = bus
        self.policy = policy
        self.audit = audit
        self.guard = guard
        self.approvals = approvals
        self.memory = memory
        self.vision = vision
        self.ollama = ollama
        self.router = router
        self.speech = speech
        self.integrations: Dict[str, Any] = integrations or {}

    # convenience -------------------------------------------------------
    @property
    def settings(self) -> Any:
        return self.settings_provider()

    def integration(self, name: str) -> Optional[Any]:
        return self.integrations.get(name)

    def emit(self, event_type: Any, data: Any = None, source: str = "tool") -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(event_type, data, source=source)
        except Exception:  # noqa: BLE001
            log.debug("event emit failed", exc_info=True)


class Tool:
    """Base class.  Subclasses set :attr:`spec` and implement :meth:`run`."""

    spec: ToolSpec = ToolSpec(name="unset", description="")

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def settings(self) -> Any:
        return self.ctx.settings

    def available(self) -> tuple:
        """``(bool, reason)`` - can this tool actually run right now?"""
        missing = []
        caps = {c.name: c.available for c in self.ctx.platform.capability_list()}
        for req in self.spec.requires:
            if req == "display" and self.ctx.platform.is_simulated:
                continue
            if not caps.get(req, False):
                missing.append(req)
        if missing:
            notes = self.ctx.platform.notes or {}
            detail = "; ".join(f"{m}: {notes.get(m, 'unavailable')}" for m in missing)
            return False, detail
        return True, ""

    def run(self, **kwargs: Any) -> ToolResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def verify(self, args: Dict[str, Any], result: ToolResult) -> Verification:
        """Default: an ``ok`` result is taken at face value *only* for read-only
        tools.  Action tools must override this."""
        if self.spec.category in ("read_only", "query", "memory", "file_read"):
            return Verification(checked=True, succeeded=result.ok,
                                detail=result.message or ("ok" if result.ok else "failed"),
                                evidence=dict(result.evidence))
        return Verification(checked=False, succeeded=result.ok,
                            detail="no independent verification available")

    # ------------------------------------------------------------------
    def execute(self, **kwargs: Any) -> ToolResult:
        """Run + verify + timing + logging.  Called by the agent loop *after*
        the policy engine has approved the call."""
        t0 = time.time()
        ok, why = self.available()
        if not ok:
            res = ToolResult(ok=False, message="",
                             error=f"{self.name} is unavailable: {why}",
                             duration_ms=(time.time() - t0) * 1000)
        else:
            try:
                res = self.run(**kwargs)
            except TypeError as exc:
                # A model inventing an argument name must not crash the loop.
                res = ToolResult(ok=False, error=f"invalid arguments for {self.name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                log.exception("Tool %s raised", self.name)
                res = ToolResult(ok=False, error=f"{self.name} failed: {exc}")
            try:
                if self.settings.automation.verify_after_action:
                    res.verification = self.verify(kwargs, res)
            except Exception:  # noqa: BLE001
                log.exception("Verification for %s raised", self.name)
                res.verification = Verification(checked=False, succeeded=res.ok,
                                                detail="verification error")
        if not res.duration_ms:
            res.duration_ms = (time.time() - t0) * 1000
        log.info("TOOL %-24s ok=%-5s %6.0fms %s", self.name, res.ok,
                 res.duration_ms, (res.message or res.error)[:110])
        return res

    # ------------------------------------------------------------------
    def _fail(self, message: str, **data: Any) -> ToolResult:
        return ToolResult(ok=False, message=message, data=data)

    def _ok(self, message: str, **data: Any) -> ToolResult:
        return ToolResult(ok=True, message=message, data=data)
