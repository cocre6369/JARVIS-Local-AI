"""Conversation session state.

The agent loop is stateless per step; this object holds the rolling message
list, the current task, and the bookkeeping that keeps the prompt inside the
model's context window.  Trimming is by *characters* rather than tokens because
counting tokens locally would cost more time than it saves.
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

MAX_MESSAGE_CHARS = 6000
MAX_TOOL_RESULT_CHARS = 3000


def new_session_id() -> str:
    return f"s-{uuid.uuid4().hex[:12]}"


def new_task_id() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


@dataclass
class Step:
    """One iteration of the loop, kept for the UI trace and the audit log."""

    index: int
    thought: str = ""
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    ok: Optional[bool] = None
    message: str = ""
    verified: Optional[bool] = None
    verify_detail: str = ""
    duration_ms: float = 0.0
    risk: str = ""
    approval: str = ""
    suggest_observe: bool = False
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "thought": self.thought, "tool": self.tool,
                "args": self.args, "ok": self.ok, "message": self.message,
                "verified": self.verified, "verify_detail": self.verify_detail,
                "duration_ms": round(self.duration_ms, 1), "risk": self.risk,
                "approval": self.approval, "ts": self.ts}


@dataclass
class TaskState:
    """Everything about the request currently being worked on."""

    task_id: str
    request: str
    session_id: str
    started_at: float = field(default_factory=time.time)
    steps: List[Step] = field(default_factory=list)
    failures: int = 0
    consecutive_failures: int = 0
    repeated_actions: int = 0
    last_action_signature: str = ""
    approvals: List[str] = field(default_factory=list)
    blocked: List[str] = field(default_factory=list)
    denied: List[str] = field(default_factory=list)
    status: str = "running"          # running | completed | failed | cancelled
    summary: str = ""
    model: str = ""
    tokens: int = 0
    cancelled: bool = False

    def elapsed_s(self) -> float:
        return time.time() - self.started_at

    def step_count(self) -> int:
        return len(self.steps)

    def record(self, step: Step) -> None:
        self.steps.append(step)

    def as_dict(self) -> Dict[str, Any]:
        return {"task_id": self.task_id, "request": self.request,
                "session_id": self.session_id, "status": self.status,
                "steps": [s.as_dict() for s in self.steps],
                "failures": self.failures, "approvals": list(self.approvals),
                "blocked": list(self.blocked), "denied": list(self.denied),
                "elapsed_s": round(self.elapsed_s(), 2), "model": self.model,
                "tokens": self.tokens, "summary": self.summary}


class Session:
    """One conversation: message history + the task in flight."""

    def __init__(self,
                 settings_provider: Callable[[], Any],
                 session_id: Optional[str] = None,
                 memory: Any = None) -> None:
        self._settings_provider = settings_provider
        self.id = session_id or new_session_id()
        self.memory = memory
        self.messages: List[Dict[str, Any]] = []
        self.task: Optional[TaskState] = None
        self._task_seq = itertools.count(1)
        self._lock = threading.RLock()
        self.created_at = time.time()
        self.turns = 0
        self.system_prompt_signature = ""
        #: Cache of the rendered system prompt for this session, invalidated when
        #: memory/context changes.  Rendering 58 tool schemas is not free.
        self.system_prompt = ""
        self.protocol = "native"
        self.last_observation_ts = 0.0
        #: The most recent screen observation, kept so a fresh one can be reused
        #: instead of re-capturing (``automation.observation_reuse_s``).
        self.last_observation: Any = None

    # ------------------------------------------------------------------
    @property
    def settings(self) -> Any:
        return self._settings_provider()

    def observation_age_s(self) -> Optional[float]:
        if not self.last_observation_ts:
            return None
        return time.time() - self.last_observation_ts

    def mark_observed(self, observation: Any = None) -> None:
        self.last_observation_ts = time.time()
        if observation is not None:
            self.last_observation = observation

    # ------------------------------------------------------------------
    def start_task(self, request: str) -> TaskState:
        with self._lock:
            self.task = TaskState(task_id=new_task_id(), request=request,
                                  session_id=self.id)
            return self.task

    def end_task(self, status: str, summary: str = "") -> Optional[TaskState]:
        with self._lock:
            task = self.task
            if task is None:
                return None
            task.status = status
            task.summary = summary
            self.task = None
            return task

    def cancel(self) -> bool:
        with self._lock:
            if self.task is None:
                return False
            self.task.cancelled = True
            return True

    # ------------------------------------------------------------------
    def set_system_prompt(self, text: str, signature: str) -> None:
        with self._lock:
            if signature != self.system_prompt_signature or not self.messages:
                self.system_prompt = text
                self.system_prompt_signature = signature
                if self.messages and self.messages[0].get("role") == "system":
                    self.messages[0] = {"role": "system", "content": text}
                else:
                    self.messages.insert(0, {"role": "system", "content": text})

    def add(self, role: str, content: str, **extra: Any) -> None:
        if content is None:
            content = ""
        content = str(content)
        if role == "tool" and len(content) > MAX_TOOL_RESULT_CHARS:
            content = content[:MAX_TOOL_RESULT_CHARS] + " …[truncated]"
        elif len(content) > MAX_MESSAGE_CHARS:
            content = content[:MAX_MESSAGE_CHARS] + " …[truncated]"
        msg: Dict[str, Any] = {"role": role, "content": content}
        msg.update(extra)
        with self._lock:
            self.messages.append(msg)
            self.turns += 1 if role == "user" else 0
            self._trim()

    def add_tool_calls(self, calls: Sequence[Dict[str, Any]], content: str = "") -> None:
        """Append an assistant message carrying native tool calls."""
        with self._lock:
            self.messages.append({"role": "assistant", "content": content or "",
                                  "tool_calls": [dict(c) for c in calls]})
            self._trim()

    def last_user_message(self) -> str:
        with self._lock:
            for msg in reversed(self.messages):
                if msg.get("role") == "user":
                    return str(msg.get("content") or "")
        return ""

    def clear_history(self, keep_system: bool = True) -> int:
        with self._lock:
            before = len(self.messages)
            if keep_system and self.messages and self.messages[0].get("role") == "system":
                self.messages = [self.messages[0]]
            else:
                self.messages = []
            return before - len(self.messages)

    def transcript(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Plain copy for the UI / memory (no system prompt, no images)."""
        with self._lock:
            out = []
            for msg in self.messages[-limit:]:
                if msg.get("role") == "system":
                    continue
                entry = {"role": msg.get("role"), "content": str(msg.get("content") or "")}
                if msg.get("tool_calls"):
                    entry["tool_calls"] = msg["tool_calls"]
                out.append(entry)
            return out

    # ------------------------------------------------------------------
    def _trim(self) -> None:
        """Keep the prompt inside the configured budget.

        Strategy: keep the system prompt, the newest messages, and any
        assistant tool_calls / tool results that belong together (Ollama rejects
        an orphaned tool reply), dropping the oldest middle section first.
        """
        budget = int(getattr(self.settings.memory, "max_context_chars", 24000) or 24000)
        max_messages = int(getattr(self.settings.memory, "max_context_messages", 24) or 24)
        msgs = self.messages
        if not msgs:
            return

        head: List[Dict[str, Any]] = []
        body = msgs
        if msgs[0].get("role") == "system":
            head, body = msgs[:1], msgs[1:]

        total = sum(len(str(m.get("content") or "")) for m in head)
        keep: List[Dict[str, Any]] = []
        for msg in reversed(body):
            cost = len(str(msg.get("content") or ""))
            if msg.get("images"):
                cost += 4000
            if keep and (len(keep) >= max_messages or total + cost > budget):
                break
            keep.append(msg)
            total += cost
        keep.reverse()

        # Never start the retained window with an orphaned tool reply.
        while keep and keep[0].get("role") == "tool":
            keep.pop(0)

        self.messages = head + keep

    # ------------------------------------------------------------------
    def persist(self, role: str, content: str, meta: Optional[Dict[str, Any]] = None) -> None:
        """Write a turn to the local memory database (never leaves the machine)."""
        if self.memory is None or not content:
            return
        try:
            self.memory.add_message(self.id, role, content, meta=meta or {})
        except Exception:  # noqa: BLE001 - memory must never break a conversation
            pass
