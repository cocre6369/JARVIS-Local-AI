"""The agent loop: intent -> plan -> execute -> observe -> verify -> recover -> report.

Design notes that matter when changing this file:

* **The model never runs a tool directly.**  Every action goes through
  :meth:`_authorise`, which asks :class:`PolicyEngine` and, when required,
  :class:`ApprovalBroker`.  A refused action is reported back to the model as a
  failure so it can recover honestly.
* **Nothing is assumed to have worked.**  Each :class:`ToolResult` carries a
  :class:`Verification`; the loop feeds that back verbatim and counts
  unverified/failed steps towards its recovery guards.
* **The loop is bounded** by ``automation.max_steps_per_task``, by an
  identical-action guard, and by a consecutive-failure guard.  When a guard
  trips the agent stops and tells the user what happened instead of spinning.
* **Latency** is treated as a feature: local intent classification picks the
  cheapest model that can do the job, prompts are cached, observations are
  reused until they are stale, tokens stream to the UI as they arrive, and a
  taught shortcut replays without any model call at all.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..events import AgentState, EventType
from ..llm.protocol import (
    native_tool_calls_to_actions,
    parse_agent_reply,
    tool_schema_for,
)
from ..security.guard import Pressure
from ..security.policy import RequestContext, Risk
from ..tools.base import ToolResult, Verification
from . import intents as intent_mod
from . import prompts
from .broker import Outcome
from .planner import Plan, heuristic_plan, plan_from_shortcuts, suggest_first_action
from .session import Session, Step

log = logging.getLogger(__name__)


@dataclass
class AgentReply:
    """What one user request produced."""

    text: str = ""
    speak_text: str = ""
    ok: bool = True
    task_id: str = ""
    session_id: str = ""
    steps: List[Step] = field(default_factory=list)
    intent: Dict[str, Any] = field(default_factory=dict)
    plan: Dict[str, Any] = field(default_factory=dict)
    model: str = ""
    tokens: int = 0
    duration_ms: float = 0.0
    approvals: List[str] = field(default_factory=list)
    blocked: List[str] = field(default_factory=list)
    denied: List[str] = field(default_factory=list)
    error: str = ""
    cancelled: bool = False
    used_shortcut: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "ok": self.ok, "task_id": self.task_id,
                "session_id": self.session_id,
                "steps": [s.as_dict() for s in self.steps],
                "intent": self.intent, "plan": self.plan, "model": self.model,
                "tokens": self.tokens, "duration_ms": round(self.duration_ms, 1),
                "approvals": list(self.approvals), "blocked": list(self.blocked),
                "denied": list(self.denied), "error": self.error,
                "cancelled": self.cancelled, "used_shortcut": self.used_shortcut}


class Agent:
    """Drives one conversation turn end to end.  Thread-safe for one task at a
    time; the UI serialises submissions through :meth:`submit`."""

    def __init__(self, runtime: Any) -> None:
        self.rt = runtime
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._busy = threading.Event()
        self._native_failed = False       # sticky: this Ollama build/model can't
        #                                     do native tool calls, use prompted
        self.current_session: Optional[Session] = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    @property
    def settings(self) -> Any:
        """Always live: settings changed in the UI take effect mid-task."""
        try:
            return self.rt.settings_provider()
        except Exception:  # noqa: BLE001
            return self.rt.settings

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def cancel(self) -> bool:
        self._cancel.set()
        if self.current_session is not None:
            self.current_session.cancel()
        self._speak_stop()
        return True

    # -- speech helpers (voice is optional; a headless run has none) ----------
    def _speak(self, text: str, interrupt: bool = True) -> bool:
        speech = getattr(self.rt, "speech", None)
        if speech is None or not text:
            return False
        try:
            return bool(speech.speak(text, interrupt=interrupt))
        except Exception:  # noqa: BLE001
            log.debug("speech failed", exc_info=True)
            return False

    def _speak_now(self, text: str, interrupt: bool = False) -> bool:
        speech = getattr(self.rt, "speech", None)
        if speech is None or not text:
            return False
        try:
            return bool(speech.speak_now(text, interrupt=interrupt))
        except Exception:  # noqa: BLE001
            return False

    def _speak_stop(self) -> None:
        speech = getattr(self.rt, "speech", None)
        if speech is None:
            return
        try:
            speech.stop()
        except Exception:  # noqa: BLE001
            pass

    def session(self, session_id: Optional[str] = None) -> Session:
        with self._lock:
            if self.current_session is None or (
                    session_id and self.current_session.id != session_id):
                self.current_session = Session(self.rt.settings_provider,
                                               session_id=session_id,
                                               memory=self.rt.memory)
            return self.current_session

    def reset(self) -> None:
        with self._lock:
            self.current_session = None
            self._native_failed = False

    def submit(self, text: str, session_id: Optional[str] = None,
               speak: Optional[bool] = None, source: str = "text",
               on_done: Optional[Callable[[AgentReply], None]] = None) -> threading.Thread:
        """Run a request on a worker thread so the UI never blocks."""
        def _worker() -> None:
            reply = self.handle(text, session_id=session_id, speak=speak, source=source)
            if on_done is not None:
                try:
                    on_done(reply)
                except Exception:  # noqa: BLE001
                    log.exception("on_done callback failed")

        t = threading.Thread(target=_worker, name="jarvis-agent", daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------------
    def handle(self, text: str, session_id: Optional[str] = None,
               speak: Optional[bool] = None, source: str = "text") -> AgentReply:
        """Run one full request.  Never raises."""
        t0 = time.time()
        text = (text or "").strip()
        if not text:
            return AgentReply(text="I did not catch that - could you say it again?",
                              speak_text="I did not catch that.", ok=False,
                              error="empty request",
                              duration_ms=(time.time() - t0) * 1000)

        self._cancel.clear()
        self._busy.set()
        # Protocol support can differ per model, and the user may have switched
        # models, so re-probe on every request rather than trusting the last one.
        self._native_failed = False
        session = self.session(session_id)
        reply = AgentReply(session_id=session.id)
        self._set_state(AgentState.THINKING)
        task = session.start_task(text)
        reply.task_id = task.task_id
        self._emit(EventType.TASK_STARTED, {"task_id": task.task_id, "request": text,
                                            "source": source})
        memory_task_id = ""
        try:
            memory_task_id = self._memory_start_task(text)
            intent = intent_mod.classify(text)
            reply.intent = intent.as_dict()
            self._emit(EventType.TASK_PROGRESS, {
                "task_id": task.task_id, "stage": "intent", "intent": intent.as_dict()})

            plan = plan_from_shortcuts(self.rt.memory, text) or heuristic_plan(intent)
            reply.plan = plan.as_dict()
            reply.used_shortcut = plan.source == "shortcut"

            if speak is None:
                speak = bool(self.settings.speech.tts_enabled)

            # -- fast path: a taught shortcut replays without any model call ----
            if plan.source == "shortcut" and self._replay_plan(plan, task, session, reply):
                answer = self._summarise_replay(plan, text)
                return self._finish(reply, task, session, answer, memory_task_id,
                                    speak=bool(speak), t0=t0)

            answer = self._run_model_loop(text, intent, plan, task, session, reply)
            return self._finish(reply, task, session, answer, memory_task_id,
                                speak=bool(speak), t0=t0)

        except Exception as exc:  # noqa: BLE001 - the loop must never crash the app
            log.exception("Agent loop failed")
            reply.ok = False
            reply.error = str(exc)
            message = ("Something went wrong inside JARVIS while working on that: "
                       f"{exc}. The details are in the log.")
            return self._finish(reply, task, session, message, memory_task_id,
                                speak=bool(speak), t0=t0, status="failed")
        finally:
            self._busy.clear()

    # ------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------
    def _run_model_loop(self, text: str, intent: Any, plan: Plan, task: Any,
                        session: Session, reply: AgentReply) -> str:
        s = self.settings
        max_steps = int(s.automation.max_steps_per_task)
        pressure = self.rt.guard.pressure if self.rt.guard else Pressure.OK
        if pressure is Pressure.HARD:
            max_steps = max(4, max_steps // 3)
            plan.add_note("System memory is under pressure, so I am limiting how "
                          "many steps I take and skipping image analysis.")

        protocol = self._protocol_for(intent)
        schemas = self._tool_schemas(protocol)
        self._prepare_session(session, text, intent, schemas, protocol, plan)

        # Look first when the request depends on what is on screen.
        obs_age = session.observation_age_s()
        if intent_mod.needs_first_observation(intent, obs_age):
            self._observe(session, reason="pre-task", attach_image=False)

        hint = suggest_first_action(intent, self.rt.registry.names())
        if hint and hint["name"] in self.rt.registry:
            session.add("user", f"(local hint: a sensible first action would be "
                                f"{hint['name']} with arguments "
                                f"{hint.get('arguments') or {}} - check it is "
                                f"appropriate before using it)")

        answer = ""
        for index in range(max_steps):
            if self._cancel.is_set():
                reply.cancelled = True
                answer = answer or "Stopped at your request."
                break

            # --- ask the model ------------------------------------------------
            self._set_state(AgentState.THINKING)
            step = Step(index=index + 1)
            outcome = self._model_turn(session, protocol, schemas, intent, step)
            reply.model = outcome.get("model", reply.model)
            reply.tokens += int(outcome.get("tokens", 0))
            if outcome.get("error"):
                answer = self._explain_model_failure(outcome["error"])
                break

            actions: List[Dict[str, Any]] = outcome.get("actions") or []
            thought = str(outcome.get("thought") or "").strip()
            if thought:
                step.thought = thought
                self._emit(EventType.THOUGHT, {"task_id": task.task_id, "thought": thought,
                                               "step": index + 1})

            if not actions:
                answer = str(outcome.get("answer") or "").strip()
                if not answer:
                    answer = self._force_answer(session, protocol, schemas, intent)
                break

            # --- run each requested action -----------------------------------
            self._set_state(AgentState.EXECUTING)
            for action in actions[:3]:        # models occasionally batch; cap it
                name = str(action.get("name") or "").strip()
                args = dict(action.get("arguments") or {})
                feedback = self._authorise_and_run(name, args, task, session, step, reply)
                session.add("tool", feedback, images=None)
                if self._cancel.is_set():
                    break

            session.add("user", self._post_step_context(session, task, plan, step))

            if step.ok is False:
                task.consecutive_failures += 1
                task.failures += 1
            elif step.ok is True:
                task.consecutive_failures = 0

            if self._guard_tripped(task, reply, plan):
                answer = self._guard_message(task, plan)
                break

            if getattr(step, "suggest_observe", False) or self._should_reobserve(step):
                # An action just ran: the screen has probably changed, so a cached
                # look is not good enough here.
                self._observe(session, reason="post-action", attach_image=False,
                              force=True)

        else:
            answer = (f"I reached my limit of {max_steps} steps without finishing. "
                      + self._progress_summary(plan, task))

        if not answer:
            answer = self._progress_summary(plan, task) or "I could not complete that."
        return answer

    #: Failures worth one automatic retry: the server hiccuped, the model was
    #: evicted, or VRAM was momentarily exhausted.  A malformed request is not.
    _RECOVERABLE_MARKERS = ("connection", "refused", "timed out", "timeout",
                            "reset by peer", "unavailable", "not found in the local",
                            "out of memory", "cuda", "temporarily", "eof",
                            "broken pipe", "no response", "502", "503", "504")

    def _recover_ollama(self, model: str, error: BaseException) -> bool:
        """Try once to bring the local server back, so a task can continue.

        Returns False when recovery is pointless, which keeps the honest error
        message for the user instead of hiding it behind a retry.
        """
        text = str(error or "").lower()
        if not text:
            return False
        if not any(marker in text for marker in self._RECOVERABLE_MARKERS):
            return False
        log.warning("Ollama call failed (%s); attempting recovery", text[:160])
        self._emit(EventType.NOTIFICATION, {
            "title": "Recovering the model",
            "message": "The local model server did not answer. JARVIS is "
                       "reconnecting and reloading the model, then retrying once."})
        try:
            self.rt.router.invalidate()
        except Exception:  # noqa: BLE001
            log.debug("router invalidate failed", exc_info=True)
        try:
            self.rt.ollama.status()          # fresh ping, clears the cached error
        except Exception:  # noqa: BLE001
            pass
        try:
            if model:
                self.rt.ollama.warm(model)
        except Exception:  # noqa: BLE001
            log.debug("warm failed", exc_info=True)
        time.sleep(0.6)
        return True

    # ------------------------------------------------------------------
    def _model_turn(self, session: Session, protocol: str,
                    schemas: Sequence[Dict[str, Any]], intent: Any,
                    step: Step, _recovered: bool = False) -> Dict[str, Any]:
        """One model call.  Returns actions / answer / thought / error."""
        out: Dict[str, Any] = {"actions": [], "answer": "", "thought": "",
                               "error": "", "model": "", "tokens": 0}
        role = intent.model_role if protocol == "native" or intent else "main"
        if role == "fast" and not self.settings.ollama.use_fast_model_for_simple_requests:
            # The user asked for one model for everything: never split the work.
            role = "main"
        resolution = self.rt.router.resolve(role, needs={"tools": protocol == "native"})
        if not resolution.usable:
            resolution = self.rt.router.resolve("main", needs={"tools": True})
        if not resolution.usable:
            out["error"] = resolution.reason or "No Ollama model is available."
            return out

        caps = resolution.capabilities or {}
        native = protocol == "native" and bool(caps.get("tools", True))
        tools = schemas if native else None
        images = self._pending_images(session)

        streamed: List[str] = []

        def on_token(chunk: str) -> None:
            if not chunk:
                return
            streamed.append(chunk)
            self._emit(EventType.STREAM_TOKEN, {"token": chunk})

        attempts = 2 if bool(self.settings.ollama.auto_recover) else 1
        res = None
        for attempt in range(attempts):
            try:
                res = self.rt.ollama.chat(
                    messages=session.messages,
                    model=resolution.model,
                    tools=tools,
                    images=images or None,
                    stream=bool(self.settings.ollama.streaming),
                    on_token=on_token,
                    options=self._model_options(native),
                )
                break
            except Exception as exc:  # noqa: BLE001
                if attempt + 1 < attempts and self._recover_ollama(resolution.model, exc):
                    continue
                out["error"] = str(exc)
                return out

        out["model"] = res.model or resolution.model
        out["tokens"] = int(getattr(res, "eval_count", 0) or 0)
        if not res.ok:
            if attempts > 1 and not _recovered and self._recover_ollama(
                    resolution.model, Exception(res.error or "")):
                retry = self._model_turn(session, protocol, schemas, intent, step,
                                         _recovered=True)
                if retry.get("error"):
                    out["error"] = res.error or "Ollama returned no response."
                    return out
                return retry
            out["error"] = res.error or "Ollama returned no response."
            return out

        content = res.content or "".join(streamed)

        if native:
            actions = native_tool_calls_to_actions(res.tool_calls)
            if actions:
                session.add_tool_calls(res.tool_calls, content=content)
                out["actions"] = actions
                out["thought"] = content.strip()
                return out
            # No native tool call.  That is the *normal* way a model finishes a
            # task, so treat any content as the answer.  Only a completely empty
            # reply means the tools API is not really working for this model,
            # and then - and only then - retry with the prompted protocol.
            if content.strip():
                parsed = parse_agent_reply(content)
                session.add("assistant", content)
                out["thought"] = parsed.get("thought") or ""
                out["answer"] = parsed.get("answer") or content.strip()
                if parsed.get("action") and not out["answer"]:
                    out["actions"] = [parsed["action"]]
                    out["answer"] = ""
                return out
            if not self._native_failed:
                self._native_failed = True
                log.warning("Native tool calling returned an empty reply; "
                            "switching to the prompted protocol.")
                session.protocol = "prompted"
                return self._model_turn(session, "prompted", schemas, intent, step,
                                        _recovered=_recovered)

        parsed = parse_agent_reply(content)
        session.add("assistant", content)
        out["thought"] = parsed.get("thought") or ""
        out["answer"] = parsed.get("answer") or ""
        if parsed.get("action"):
            out["actions"] = [parsed["action"]]
        elif not out["answer"] and parsed.get("parse_error") == "no-json":
            out["answer"] = content.strip()
        return out

    def _model_options(self, native: bool) -> Dict[str, Any]:
        s = self.settings.ollama
        opts: Dict[str, Any] = {"temperature": float(s.temperature)}
        if not native:
            # The prompted protocol depends on strict JSON; keep it cold.
            opts["temperature"] = min(float(s.temperature), 0.1)
        return opts

    def _force_answer(self, session: Session, protocol: str,
                      schemas: Sequence[Dict[str, Any]], intent: Any) -> str:
        """The model produced neither an action nor an answer: ask it plainly."""
        session.add("user", "Reply with your final answer for the user now, in one "
                            "or two sentences. If you cannot finish, say exactly "
                            "what is stopping you.")
        res = self._model_turn(session, protocol, schemas, intent, Step(index=0))
        return str(res.get("answer") or "").strip()

    # ------------------------------------------------------------------
    # authorisation + execution
    # ------------------------------------------------------------------
    def _authorise_and_run(self, name: str, args: Dict[str, Any], task: Any,
                           session: Session, step: Step, reply: AgentReply) -> str:
        """Policy-check one action, get approval if needed, run it, verify it."""
        tool = self.rt.registry.get(name)
        step.tool = name
        step.args = {k: v for k, v in list(args.items())[:12]}
        self._emit(EventType.TOOL_CALL, {"task_id": task.task_id, "tool": name,
                                         "args": step.args, "step": step.index})

        if tool is None:
            known = ", ".join(self.rt.registry.names()[:14])
            step.ok = False
            step.message = f"unknown tool '{name}'"
            feedback = prompts.step_feedback(ToolResult(
                ok=False, error=f"There is no tool called '{name}'. Available tools "
                                f"include: {known}. Choose one of those."))
            self._emit(EventType.TOOL_RESULT, {"task_id": task.task_id, "tool": name,
                                               "ok": False, "error": step.message})
            task.record(step)
            return feedback

        spec = tool.spec
        baseline = self._baseline_risk(tool, args)
        ctx = RequestContext(
            tool=name, args=args, category=spec.category, baseline=baseline,
            task_id=task.task_id, description=self._describe(tool, args),
            reversible=bool(spec.reversible),
            target_label=self._target_label(args)).analyse()
        step.risk = baseline.value if hasattr(baseline, "value") else str(baseline)

        decision = self.rt.policy.evaluate(ctx)
        outcome = Outcome.NOT_REQUIRED
        if decision.risk is not Risk.ALLOWED:
            self._set_state(AgentState.WAITING_FOR_APPROVAL)
            outcome = self.rt.broker.request_approval(ctx, decision, task_id=task.task_id)
            step.approval = outcome.value
            if outcome is Outcome.APPROVED or outcome is Outcome.APPROVED_FOR_SESSION:
                reply.approvals.append(name)
                task.approvals.append(name)
            elif outcome is Outcome.BLOCKED:
                reply.blocked.append(name)
                task.blocked.append(f"{name}: {decision.reason}")
            elif outcome in (Outcome.DENIED, Outcome.TIMEOUT):
                reply.denied.append(name)
                task.denied.append(name)

        if outcome is Outcome.BLOCKED:
            step.ok = False
            step.message = decision.reason
            feedback = prompts.blocked_notice(name, decision.reason)
            self._emit(EventType.TOOL_RESULT, {"task_id": task.task_id, "tool": name,
                                               "ok": False, "blocked": True,
                                               "reason": decision.reason})
            task.record(step)
            return feedback

        if outcome in (Outcome.DENIED, Outcome.TIMEOUT):
            step.ok = False
            note = "the user did not respond in time" if outcome is Outcome.TIMEOUT else ""
            step.message = "denied by user" if not note else "approval timed out"
            feedback = prompts.denied_notice(name, note)
            self._emit(EventType.TOOL_RESULT, {"task_id": task.task_id, "tool": name,
                                               "ok": False, "denied": True})
            task.record(step)
            return feedback

        # --- run it ----------------------------------------------------------
        before = self._pre_action_capture(name)
        t0 = time.time()
        result = self.rt.registry.execute(name, args)
        step.duration_ms = (time.time() - t0) * 1000
        step.ok = bool(result.ok)
        step.message = (result.message or result.error or "")[:400]
        if result.verification is not None:
            step.verified = bool(result.verification.succeeded) if \
                result.verification.checked else None
            step.verify_detail = result.verification.detail[:300]
        step.suggest_observe = bool(result.suggest_observe)  # type: ignore[attr-defined]

        self._emit(EventType.TOOL_RESULT, {"task_id": task.task_id, "tool": name,
                                           "step": step.index,
                                           "before_screenshot": before,
                                           **result.as_event()})
        task.record(step)

        if self.settings.speech.speak_progress_updates and \
                self.settings.speech.speak_tool_actions and result.message:
            self._speak_now(result.message[:120], interrupt=False)

        feedback = prompts.step_feedback(result)
        hint = self._recovery_hint(name, args, result, task)
        if hint:
            feedback += "\n" + hint
        return feedback

    def _baseline_risk(self, tool: Any, args: Dict[str, Any]) -> Risk:
        """The code-defined floor for this action.  Prompts cannot lower it."""
        candidates = [tool.spec.risk]
        for attr in ("dynamic_risk", "risk_for"):
            fn = getattr(tool, attr, None)
            if callable(fn):
                try:
                    extra = fn(args)
                    if isinstance(extra, Risk):
                        candidates.append(extra)
                except Exception:  # noqa: BLE001
                    log.debug("%s.%s raised", tool.name, attr, exc_info=True)
        return max(candidates, key=lambda r: r.rank)

    def _describe(self, tool: Any, args: Dict[str, Any]) -> str:
        try:
            return (tool.spec.description or tool.name)[:220]
        except Exception:  # noqa: BLE001
            return tool.name

    def _target_label(self, args: Dict[str, Any]) -> str:
        for key in ("path", "url", "name", "app", "description", "element",
                    "query", "text", "command"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:160]
        return ""

    def _recovery_hint(self, name: str, args: Dict[str, Any], result: ToolResult,
                       task: Any) -> str:
        v = result.verification
        if result.ok and (v is None or not v.checked or v.succeeded):
            return ""
        reason = (result.error or (v.detail if v else "") or "").lower()
        hints: List[str] = []
        if "not found" in reason or "could not find" in reason or "no control" in reason:
            hints.append("The control was not found. Re-observe the screen and try a "
                         "different description, a keyboard shortcut, or the "
                         "application's own menu.")
        if "unavailable" in reason:
            hints.append("That capability is not available on this machine - check "
                         "the Automation page report and choose a different route.")
        if "password" in reason:
            hints.append("Credential fields are off limits. Ask the user to type the "
                         "password themselves, then continue.")
        if task.consecutive_failures >= 1:
            hints.append(prompts.recovery_hint(task.consecutive_failures + 1,
                                               task.repeated_actions))
        return " ".join(h for h in hints if h).strip()

    # ------------------------------------------------------------------
    # guards
    # ------------------------------------------------------------------
    def _guard_tripped(self, task: Any, reply: AgentReply, plan: Plan) -> bool:
        s = self.settings.automation
        if task.consecutive_failures >= int(s.max_consecutive_failures):
            plan.add_note("Stopped: too many consecutive failures.")
            return True
        if task.repeated_actions >= int(s.max_identical_actions):
            plan.add_note("Stopped: the same action kept having no effect.")
            return True
        return False

    def _guard_message(self, task: Any, plan: Plan) -> str:
        if task.repeated_actions >= int(self.settings.automation.max_identical_actions):
            return ("I kept trying the same action and it did not have the effect I "
                    "expected, so I stopped rather than repeating it. "
                    + self._progress_summary(plan, task))
        return ("Several actions in a row failed, so I stopped. "
                + self._progress_summary(plan, task))

    def _progress_summary(self, plan: Plan, task: Any) -> str:
        done = [s for s in task.steps if s.ok]
        failed = [s for s in task.steps if s.ok is False]
        parts: List[str] = []
        if done:
            parts.append("Completed: " + "; ".join(
                f"{s.tool} ({s.message[:60]})" for s in done[-3:]))
        if failed:
            parts.append("Failed: " + "; ".join(
                f"{s.tool} - {s.message[:60]}" for s in failed[-2:]))
        if task.blocked:
            parts.append("Refused by security policy: " + ", ".join(task.blocked[-2:]))
        if task.denied:
            parts.append("You declined: " + ", ".join(task.denied[-2:]))
        return " ".join(parts)

    def _summarise_replay(self, plan: Plan, text: str) -> str:
        done = [s for s in plan.steps if s.done and s.ok]
        failed = [s for s in plan.steps if s.done and not s.ok]
        if failed:
            return (f"I replayed your shortcut but {len(failed)} step(s) did not work: "
                    + "; ".join(s.description for s in failed[:2]) + ".")
        if done:
            return "Done - I repeated the steps you taught me and verified each one."
        return "I could not replay that shortcut; nothing matched."

    # ------------------------------------------------------------------
    # shortcut replay (no model calls)
    # ------------------------------------------------------------------
    def _replay_plan(self, plan: Plan, task: Any, session: Session,
                     reply: AgentReply) -> bool:
        """Execute a saved shortcut directly.  Returns True when every step ran.

        Every step still goes through the policy engine and is still verified, so
        replaying is not a way to bypass approval.
        """
        if not plan.steps:
            return False
        all_ok = True
        for i, step_plan in enumerate(plan.steps):
            if self._cancel.is_set():
                reply.cancelled = True
                return False
            if not step_plan.tool or step_plan.tool not in self.rt.registry:
                all_ok = False
                plan.add_note(f"Step '{step_plan.description}' needs a tool that is "
                              "not available here.")
                break
            step = Step(index=i + 1, tool=step_plan.tool, args=dict(step_plan.args))
            self._authorise_and_run(step_plan.tool, dict(step_plan.args), task,
                                    session, step, reply)
            plan.complete(i, bool(step.ok), note=step.message[:120])
            if not step.ok:
                all_ok = False
                break
            if self._should_reobserve(step):
                self._observe(session, reason="shortcut-step", attach_image=False,
                              force=True)
        return all_ok

    # ------------------------------------------------------------------
    # observation
    # ------------------------------------------------------------------
    def _observe(self, session: Session, reason: str = "",
                 attach_image: bool = False, force: bool = False) -> Any:
        if self.rt.vision is None:
            return None
        if self.rt.guard is not None and not self.rt.guard.may_run_vision():
            log.info("Skipping observation: resource guard says no")
            return None
        reuse_s = float(getattr(self.settings.automation, "observation_reuse_s", 0.0)
                        or 0.0)
        if reuse_s > 0 and not force:
            age = time.time() - float(getattr(session, "last_observation_ts", 0.0) or 0.0)
            if 0 <= age < reuse_s:
                # A recent look is still valid; re-observing would only cost time
                # and tokens.  ``force`` is used when the screen must have changed.
                log.debug("Reusing observation from %.2fs ago (< %.2fs)", age, reuse_s)
                return getattr(session, "last_observation", None)
        self._set_state(AgentState.OBSERVING)
        try:
            obs = self.rt.vision.observe(reason=reason or "agent",
                                         include_image=True)
        except Exception:  # noqa: BLE001
            log.exception("observation failed")
            return None
        session.mark_observed(obs)
        if obs is not None:
            session.add("user", "[screen update]\n" + prompts.observation_block(obs))
            if attach_image and obs.image_b64:
                session.messages[-1]["images"] = [obs.image_b64]
        return obs

    #: Categories whose effects are visible on screen, so a "before" frame is
    #: worth keeping.  Reading a file or recalling a memory changes no pixels.
    _VISUAL_CATEGORIES = ("input", "application", "media", "navigation", "system")

    def _pre_action_capture(self, name: str) -> str:
        """Grab a 'before' frame for actions that change what is on screen.

        Verification compares the screen after an action; having the frame from
        just before it makes a failed action diagnosable ("the window never
        opened") instead of merely reported.  Skipped for non-visual tools, when
        the setting is off, and whenever the resource guard says vision is too
        expensive right now.
        """
        if not bool(self.settings.automation.take_screenshot_before_action):
            return ""
        if self.rt.vision is None:
            return ""
        if self.rt.guard is not None and not self.rt.guard.may_run_vision():
            return ""
        tool = self.rt.registry.get(name)
        category = str(getattr(getattr(tool, "spec", None), "category", "") or "")
        if category not in self._VISUAL_CATEGORIES:
            return ""
        try:
            obs = self.rt.vision.observe(include_image=True, include_elements=False,
                                         include_ocr=False, force=True,
                                         reason=f"before:{name}")
            return str(getattr(obs, "image_path", "") or "")
        except Exception:  # noqa: BLE001
            log.debug("pre-action capture failed", exc_info=True)
            return ""

    def _should_reobserve(self, step: Step) -> bool:
        if not self.settings.vision.observe_after_action:
            return False
        return bool(getattr(step, "suggest_observe", False)) or step.ok is False

    def _pending_images(self, session: Session) -> List[str]:
        """Collect (and consume) base64 screenshots queued for the next call."""
        images: List[str] = []
        for msg in session.messages:
            queued = msg.pop("images", None)
            if queued:
                images.extend(queued)
        if not images:
            return []
        if not self.rt.router.resolve("vision", needs={"vision": True}).usable:
            return []            # no vision model: don't waste tokens on pixels
        return images[-1:]

    # ------------------------------------------------------------------
    # prompt assembly
    # ------------------------------------------------------------------
    def _protocol_for(self, intent: Any) -> str:
        if self._native_failed:
            return "prompted"
        resolution = self.rt.router.resolve(intent.model_role if intent else "main",
                                            needs={"tools": True})
        caps = resolution.capabilities or {}
        if caps.get("tools"):
            return "native"
        return "prompted"

    def _tool_schemas(self, protocol: str) -> List[Dict[str, Any]]:
        specs = self.rt.registry.visible_specs(only_available=True)
        if protocol == "native":
            return tool_schema_for(specs)
        return [spec.as_schema() for spec in specs]

    def _prepare_session(self, session: Session, text: str, intent: Any,
                         schemas: Sequence[Dict[str, Any]], protocol: str,
                         plan: Plan) -> None:
        s = self.settings
        memory_text = self._memory_context(session.id, text)
        caps_text = prompts.capabilities_text(
            self.rt.platform,
            extra_notes=[f"Plan: {plan.progress_text()}"] + [f"- {n}" for n in plan.notes[:3]])
        vision_model_ok = self.rt.router.resolve("vision", needs={"vision": True}).usable
        system = prompts.system_prompt(s, schemas, caps_text, memory_text=memory_text,
                                       protocol=protocol)
        system += "\n" + prompts.vision_instruction(vision_model_ok, False) + "\n"
        system += prompts.final_answer_instruction(bool(s.speech.tts_enabled))

        signature = f"{len(schemas)}|{protocol}|{hash(memory_text) & 0xffff}|" \
                    f"{hash(caps_text) & 0xffff}"
        session.protocol = protocol
        session.set_system_prompt(system, signature)
        session.add("user", text)
        session.persist("user", text, meta={"intent": intent.kind})

    def _memory_context(self, session_id: str, query: str) -> str:
        if self.rt.memory is None:
            return ""
        try:
            block = self.rt.memory.context_block(
                session_id, query,
                max_messages=int(self.settings.memory.max_context_messages),
                max_chars=int(self.settings.memory.max_context_chars),
                max_facts=int(self.settings.memory.max_facts_in_prompt))
        except Exception:  # noqa: BLE001
            return ""
        parts: List[str] = []
        if block.get("facts_text"):
            parts.append("Things you know about this user:\n" + block["facts_text"])
        if block.get("shortcuts_text"):
            parts.append(block["shortcuts_text"])
        if block.get("tasks_text"):
            parts.append("Similar past tasks:\n" + block["tasks_text"])
        return "\n\n".join(parts)

    def _post_step_context(self, session: Session, task: Any, plan: Plan,
                           step: Step) -> str:
        """Cheap, local guidance appended after each step (no model call)."""
        parts: List[str] = []
        remaining = plan.remaining()
        if remaining:
            parts.append("Plan still to do: " + "; ".join(
                s.description for s in remaining[:3]))
        if task.consecutive_failures:
            parts.append(prompts.recovery_hint(task.consecutive_failures,
                                               task.repeated_actions))
        pressure = self.rt.guard.pressure if self.rt.guard else Pressure.OK
        if pressure is not Pressure.OK:
            parts.append(f"System resources are under {pressure.value} pressure: "
                         "prefer the cheapest tool that works and skip optional "
                         "screenshots.")
        if not parts:
            parts.append("Continue with the next step, or give your final answer.")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # finishing
    # ------------------------------------------------------------------
    def _finish(self, reply: AgentReply, task: Any, session: Session, answer: str,
                memory_task_id: str, speak: bool, t0: float,
                status: Optional[str] = None) -> AgentReply:
        answer = (answer or "").strip() or "I could not complete that."
        reply.text = answer
        reply.speak_text = self._speakable(answer)
        reply.steps = list(task.steps)
        reply.plan = self._plan_dict(task)
        reply.ok = bool(status != "failed") and not reply.blocked and \
            not any(s.ok is False for s in task.steps[-1:])
        if task.cancelled or reply.cancelled:
            status = status or "cancelled"
        status = status or ("completed" if reply.ok else "failed")

        session.add("assistant", answer)
        session.persist("assistant", answer, meta={"task_id": task.task_id})
        task.status = status
        task.summary = answer[:400]
        session.end_task(status, task.summary)
        self._memory_finish_task(memory_task_id, status, answer, task)

        reply.duration_ms = (time.time() - t0) * 1000
        self._emit(EventType.ASSISTANT_MESSAGE, {"task_id": task.task_id,
                                                 "text": answer})
        self._emit(EventType.TASK_COMPLETED if status == "completed"
                   else EventType.TASK_FAILED,
                   {"task_id": task.task_id, "status": status, "summary": answer[:400],
                    "steps": len(task.steps), "duration_ms": round(reply.duration_ms, 1),
                    "blocked": list(task.blocked), "denied": list(task.denied)})
        spoken = False
        if speak and reply.speak_text:
            self._set_state(AgentState.SPEAKING)
            spoken = self._speak(reply.speak_text)
        if not spoken:
            # Nothing was queued (voice disabled, or no engine): go straight back
            # to idle.  When speech *is* queued the runtime returns to idle on
            # the TTS_FINISHED event instead of cutting the voice short.
            self._set_state(AgentState.IDLE)
        log.info("TASK %s %s steps=%d %.0fms model=%s", task.task_id, status,
                 len(task.steps), reply.duration_ms, reply.model)
        return reply

    def _plan_dict(self, task: Any) -> Dict[str, Any]:
        return {"goal": task.request, "steps": [s.as_dict() for s in task.steps],
                "blocked": list(task.blocked), "denied": list(task.denied),
                "approvals": list(task.approvals)}

    def _speakable(self, text: str) -> str:
        """Strip markdown/formatting so the TTS voice reads sentences, not syntax."""
        import re

        out = text.strip()
        out = re.sub(r"```.*?```", " ", out, flags=re.S)
        out = re.sub(r"`([^`]*)`", r"\1", out)
        out = re.sub(r"^\s*[-*+]\s+", "", out, flags=re.M)
        out = re.sub(r"\*\*|__|~~|#{1,6}\s*", "", out)
        out = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", out)
        out = re.sub(r"\s+", " ", out).strip()
        if len(out) > 700:
            cut = out[:700]
            stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
            out = cut[:stop + 1] if stop > 200 else cut
        return out

    def _explain_model_failure(self, error: str) -> str:
        low = error.lower()
        if "no ollama model selected" in low or "no model" in low:
            return ("No Ollama model is selected yet. Open the Models page and pick "
                    "one - JARVIS cannot reason without it.")
        if "connection refused" in low or "unreachable" in low or "cannot connect" in low:
            return ("I cannot reach Ollama on this machine. Start Ollama, then try "
                    "again; the General page shows the address I am using.")
        if "timeout" in low or "timed out" in low:
            return ("The model took too long to answer. Try a smaller or faster model "
                    "on the Models page, or ask me something shorter.")
        return f"The model could not answer: {error[:300]}"

    # ------------------------------------------------------------------
    # memory / events
    # ------------------------------------------------------------------
    def _memory_start_task(self, text: str) -> str:
        if self.rt.memory is None:
            return ""
        try:
            rec = self.rt.memory.start_task(text, model=self.settings.ollama.main_model)
            return rec.id
        except Exception:  # noqa: BLE001
            return ""

    def _memory_finish_task(self, task_id: str, status: str, answer: str,
                            task: Any) -> None:
        if self.rt.memory is None or not task_id:
            return
        try:
            self.rt.memory.finish_task(
                task_id, status, summary=answer[:400],
                steps=[s.as_dict() for s in task.steps][:40],
                duration_ms=task.elapsed_s() * 1000)
        except Exception:  # noqa: BLE001
            log.debug("finish_task failed", exc_info=True)
        self._remember_shortcut(task, status)

    def _remember_shortcut(self, task: Any, status: str) -> None:
        """Teach JARVIS a finished flow so the same words need no model call.

        Only genuinely reusable flows qualify: completed, at least two tool
        steps, every step verified-or-OK, nothing blocked or denied.  Replay
        still goes through the policy engine and re-verifies each step, so a
        remembered shortcut can never become a back door.
        """
        if status != "completed":
            return
        # A flow that stumbled (any failed step, or the same action repeated)
        # is not a flow worth replaying blind: a flailing session that happened
        # to end "completed" once taught JARVIS to re-open the same wrong apps.
        if int(getattr(task, "failures", 0) or 0) > 0 \
                or int(getattr(task, "repeated_actions", 0) or 0) > 0:
            return
        request = str(getattr(task, "request", "") or "").strip()
        if not request or len(request) > 140:
            return
        steps = [s for s in getattr(task, "steps", []) if getattr(s, "ok", False)]
        if len(steps) < 2 or getattr(task, "blocked", None) or getattr(task, "denied", None):
            return
        action = [{"tool": s.tool, "args": dict(getattr(s, "args", {}) or {})}
                  for s in steps][:12]
        try:
            self.rt.memory.add_shortcut(request, action)
            log.info("Remembered shortcut for %r (%d steps)", request, len(action))
        except Exception:  # noqa: BLE001
            log.debug("add_shortcut failed", exc_info=True)

    def _emit(self, event_type: EventType, data: Any) -> None:
        try:
            self.rt.bus.publish(event_type, data, source="agent")
        except Exception:  # noqa: BLE001
            log.debug("event emit failed", exc_info=True)

    def _set_state(self, state: AgentState) -> None:
        try:
            self.rt.set_state(state)
        except Exception:  # noqa: BLE001
            log.debug("state change failed", exc_info=True)
