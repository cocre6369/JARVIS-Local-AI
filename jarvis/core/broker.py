"""The user-approval broker.

This is the *only* channel through which an approval can be granted.  It is
deliberately **not** registered as a tool, so the language model has no way to
call it, and it ignores anything the model may have written into the
conversation ("the user already said do whatever you need" is not consent for a
specific destructive action - section 15.C).

Behaviour:

* A request blocks the agent's worker thread on a :class:`threading.Event`.
* The UI (or a test harness via :meth:`set_responder`) resolves it with
  approve / approve-for-session / deny.
* **Fail closed.**  If nobody answers within the timeout, or no UI is attached,
  the answer is "denied".
* Every request and outcome is written to the audit log with the arguments
  redacted.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from ..events import EventType
from ..security.audit import AuditLog
from ..security.policy import PolicyDecision, RequestContext, Risk, SessionGrants

log = logging.getLogger(__name__)


class Outcome(str, Enum):
    APPROVED = "approved"
    APPROVED_FOR_SESSION = "approved_for_session"
    DENIED = "denied"
    TIMEOUT = "timeout"
    NOT_REQUIRED = "not_required"
    BLOCKED = "blocked"


@dataclass
class ApprovalRequest:
    id: int
    tool: str
    decision: PolicyDecision
    args: Dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    created_ts: float = field(default_factory=time.time)
    resolved: Optional[Outcome] = None
    resolved_ts: float = 0.0
    note: str = ""
    kind: str = "approval"          # approval | question
    question: str = ""
    options: List[str] = field(default_factory=list)
    answer: str = ""

    def as_dict(self) -> Dict[str, Any]:
        from ..security.redaction import redact_dict

        return {
            "id": self.id, "tool": self.tool, "kind": self.kind,
            "question": self.question, "options": list(self.options),
            "task_id": self.task_id, "created_ts": self.created_ts,
            "risk": self.decision.risk.value, "rule": self.decision.rule,
            "reason": self.decision.reason, "target": self.decision.target,
            "category": self.decision.category,
            "consequences": self.decision.consequences,
            "reversible": self.decision.reversible,
            "arguments": redact_dict(self.args),
            "resolved": self.resolved.value if self.resolved else None,
            "note": self.note, "answer": self.answer,
        }


Responder = Callable[[ApprovalRequest], Optional[Outcome]]


class ApprovalBroker:
    def __init__(self,
                 bus: Any,
                 grants: SessionGrants,
                 audit: Optional[AuditLog] = None,
                 timeout_s: float = 600.0) -> None:
        self.bus = bus
        self.grants = grants
        self.audit = audit
        self.timeout_s = float(timeout_s)
        self._seq = itertools.count(1)
        self._pending: Dict[int, ApprovalRequest] = {}
        self._events: Dict[int, threading.Event] = {}
        self._lock = threading.RLock()
        self._responder: Optional[Responder] = None
        self.history: List[ApprovalRequest] = []

    # ------------------------------------------------------------------
    def set_responder(self, responder: Optional[Responder]) -> None:
        """Attach an automatic responder (tests, CLI mode, headless runs).

        When no responder is attached and no UI resolves the request, the
        request times out and is **denied**.
        """
        self._responder = responder

    # ------------------------------------------------------------------
    def request_approval(self, ctx: RequestContext, decision: PolicyDecision,
                         task_id: str = "") -> Outcome:
        """Block until the user decides.  Never raises."""
        if decision.risk is Risk.ALLOWED:
            return Outcome.NOT_REQUIRED
        if decision.risk is Risk.PROHIBITED:
            self._audit(ctx, decision, Outcome.BLOCKED, task_id)
            self._emit(EventType.ACTION_BLOCKED, {
                "tool": ctx.tool, "rule": decision.rule, "reason": decision.reason,
                "target": decision.target, "category": decision.category})
            return Outcome.BLOCKED

        req = ApprovalRequest(id=next(self._seq), tool=ctx.tool, decision=decision,
                              args=dict(ctx.args or {}), task_id=task_id,
                              kind="approval")
        event = threading.Event()
        with self._lock:
            self._pending[req.id] = req
            self._events[req.id] = event
            self.history.append(req)
            if len(self.history) > 500:
                del self.history[:-500]

        log.info("APPROVAL REQUIRED #%d tool=%s rule=%s target=%s",
                 req.id, ctx.tool, decision.rule, decision.target)
        self._emit(EventType.APPROVAL_REQUESTED, req.as_dict())

        # Automated responder first (tests / CLI).  It runs on a side thread so
        # the blocking wait below stays uniform.
        if self._responder is not None:
            def _auto() -> None:
                try:
                    outcome = self._responder(req)  # type: ignore[misc]
                except Exception:  # noqa: BLE001
                    log.exception("approval responder raised")
                    outcome = None
                if outcome is not None:
                    self.resolve(req.id, outcome)

            threading.Thread(target=_auto, name=f"jarvis-approval-{req.id}",
                             daemon=True).start()

        answered = event.wait(self.timeout_s)
        with self._lock:
            self._pending.pop(req.id, None)
            self._events.pop(req.id, None)
            outcome = req.resolved or (Outcome.TIMEOUT if not answered else Outcome.DENIED)
        self._audit(ctx, decision, outcome, task_id, note=req.note)
        self._emit(EventType.APPROVAL_RESOLVED, {
            "id": req.id, "tool": ctx.tool, "outcome": outcome.value, "note": req.note})
        if outcome in (Outcome.APPROVED, Outcome.APPROVED_FOR_SESSION):
            if outcome is Outcome.APPROVED_FOR_SESSION:
                self.grants.grant(SessionGrants.key(ctx.tool, decision.target))
            return Outcome.APPROVED
        return outcome

    # ------------------------------------------------------------------
    def ask(self, question: str, options: Optional[List[str]] = None,
            task_id: str = "", timeout_s: Optional[float] = None) -> str:
        """Ask the user a free-form question.  Returns "" when unanswered."""
        req = ApprovalRequest(id=next(self._seq), tool="ask_user",
                              decision=PolicyDecision(risk=Risk.APPROVAL,
                                                      rule="QUESTION",
                                                      reason=question),
                              task_id=task_id, kind="question",
                              question=question, options=list(options or []))
        event = threading.Event()
        with self._lock:
            self._pending[req.id] = req
            self._events[req.id] = event
            self.history.append(req)
        self._emit(EventType.APPROVAL_REQUESTED, req.as_dict())
        if self._responder is not None:
            def _auto() -> None:
                try:
                    outcome = self._responder(req)  # type: ignore[misc]
                except Exception:  # noqa: BLE001
                    outcome = None
                if outcome is not None:
                    self.resolve(req.id, outcome)

            threading.Thread(target=_auto, daemon=True).start()
        event.wait(float(timeout_s if timeout_s is not None else self.timeout_s))
        with self._lock:
            self._pending.pop(req.id, None)
            self._events.pop(req.id, None)
            answer = req.answer
        self._emit(EventType.APPROVAL_RESOLVED, {
            "id": req.id, "tool": "ask_user", "outcome": "answered",
            "answer": answer})
        return answer

    # ------------------------------------------------------------------
    def resolve(self, request_id: int, outcome: Outcome, note: str = "",
                answer: str = "") -> bool:
        """Called by the UI.  ``outcome`` may be APPROVED / DENIED /
        APPROVED_FOR_SESSION."""
        with self._lock:
            req = self._pending.get(int(request_id))
            event = self._events.get(int(request_id))
        if req is None:
            log.warning("Approval #%s is no longer pending", request_id)
            return False
        req.resolved = outcome
        req.note = note or ""
        if answer:
            req.answer = answer
        req.resolved_ts = time.time()
        if event is not None:
            event.set()
        return True

    def cancel_all(self, reason: str = "cancelled") -> int:
        with self._lock:
            ids = list(self._pending)
        for rid in ids:
            self.resolve(rid, Outcome.DENIED, note=reason)
        return len(ids)

    def pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [r.as_dict() for r in self._pending.values()]

    def recent(self, limit: int = 40) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self.history)
        return [r.as_dict() for r in items[-limit:]]

    # ------------------------------------------------------------------
    def _audit(self, ctx: RequestContext, decision: PolicyDecision,
               outcome: Outcome, task_id: str, note: str = "") -> None:
        if self.audit is None:
            return
        try:
            self.audit.log_decision(
                tool=ctx.tool, risk=decision.risk.value, decision=outcome.value,
                rule=decision.rule, reason=decision.reason,
                category=decision.category, target=decision.target,
                args=dict(ctx.args or {}), task_id=task_id,
                actor=("user" if outcome in (Outcome.APPROVED,
                                             Outcome.APPROVED_FOR_SESSION,
                                             Outcome.DENIED) else "policy"),
                success=outcome in (Outcome.APPROVED, Outcome.APPROVED_FOR_SESSION,
                                    Outcome.NOT_REQUIRED))
        except Exception:  # noqa: BLE001
            log.exception("audit write failed")

    def _emit(self, event_type: EventType, data: Any) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(event_type, data, source="broker")
        except Exception:  # noqa: BLE001
            log.debug("broker event emit failed", exc_info=True)


def started_at_label(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))
