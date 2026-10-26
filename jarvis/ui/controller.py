"""The bridge between the engine and the interface.

Two rules shape this module:

1. **Events arrive on worker threads; Qt objects live on the GUI thread.**  So
   every event is turned into a plain :class:`UiUpdate` and queued.  The window
   drains the queue from a timer and is the only thing that touches widgets.
2. **The UI never does engine work inline.**  Submitting a command, refreshing
   models or loading a speech engine all hand off to the runtime, which runs
   them on its own threads and reports back through the queue.

It is Qt-free on purpose: the whole flow can be driven from a test (or the CLI)
without a display server.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.broker import Outcome
from ..events import AgentState, EventType

log = logging.getLogger(__name__)

#: Update kinds the widgets understand.
K_STATE = "state"
K_MESSAGE = "message"               # a conversation line
K_STREAM = "stream"                 # incremental assistant text
K_THOUGHT = "thought"
K_TOOL = "tool"                     # tool call / result for the trace panel
K_APPROVAL = "approval"             # a dialog must be shown
K_APPROVAL_RESOLVED = "approval_resolved"
K_QUESTION = "question"
K_OLLAMA = "ollama"
K_MODELS = "models"
K_METRICS = "metrics"
K_SPEECH = "speech"
K_STT = "stt"
K_WAKE = "wake"
K_LEVEL = "level"                   # microphone level
K_TTS_LEVEL = "tts_level"
K_OBSERVATION = "observation"
K_NOTIFICATION = "notification"
K_MEMORY = "memory"
K_SETTINGS = "settings"
K_LOG = "log"
K_TASK = "task"


@dataclass
class UiUpdate:
    kind: str
    data: Any = None
    ts: float = field(default_factory=time.time)


@dataclass
class ConversationLine:
    role: str                       # user | assistant | system | tool
    text: str
    ts: float = field(default_factory=time.time)
    task_id: str = ""
    tool: str = ""
    ok: Optional[bool] = None
    streaming: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "text": self.text, "ts": self.ts,
                "task_id": self.task_id, "tool": self.tool, "ok": self.ok,
                "streaming": self.streaming}


class UiController:
    """Owns the event subscriptions and the update queue."""

    def __init__(self, runtime: Any, max_queue: int = 2000) -> None:
        self.runtime = runtime
        self.bus = runtime.bus
        self._queue: "queue.Queue[UiUpdate]" = queue.Queue(maxsize=max_queue)
        self._unsubscribers: List[Callable[[], None]] = []
        self._lock = threading.RLock()
        self.state = AgentState.IDLE.value
        self.metrics: Dict[str, Any] = {}
        self.lines: List[ConversationLine] = []
        self.max_lines = 400
        self._stream_buffer: Dict[str, List[str]] = {}
        self.pending_approvals: Dict[int, Dict[str, Any]] = {}
        self.last_notification: Dict[str, Any] = {}
        self.busy = False
        self._subscribe()

    # ------------------------------------------------------------------
    def _subscribe(self) -> None:
        bus = self.bus
        subs = [
            (EventType.STATE_CHANGED, self._on_state),
            (EventType.USER_MESSAGE, self._on_user_message),
            (EventType.ASSISTANT_MESSAGE, self._on_assistant_message),
            (EventType.STREAM_TOKEN, self._on_stream_token),
            (EventType.THOUGHT, self._on_thought),
            (EventType.TASK_STARTED, self._on_task_started),
            (EventType.TASK_PROGRESS, self._on_task_progress),
            (EventType.TASK_COMPLETED, self._on_task_finished),
            (EventType.TASK_FAILED, self._on_task_finished),
            (EventType.TOOL_CALL, self._on_tool_call),
            (EventType.TOOL_RESULT, self._on_tool_result),
            (EventType.APPROVAL_REQUESTED, self._on_approval_requested),
            (EventType.APPROVAL_RESOLVED, self._on_approval_resolved),
            (EventType.ACTION_BLOCKED, self._on_action_blocked),
            (EventType.OLLAMA_STATUS, self._on_ollama),
            (EventType.MODELS_CHANGED, self._on_models),
            (EventType.METRICS, self._on_metrics),
            (EventType.RESOURCE_WARNING, self._on_resource_warning),
            (EventType.STT_FINAL, self._on_stt),
            (EventType.STT_PARTIAL, self._on_stt_partial),
            (EventType.STT_ERROR, self._on_stt_error),
            (EventType.LISTENING_STARTED, self._on_listening),
            (EventType.LISTENING_STOPPED, self._on_listening),
            (EventType.WAKEWORD_DETECTED, self._on_wake),
            (EventType.AUDIO_LEVEL, self._on_level),
            (EventType.TTS_LEVEL, self._on_tts_level),
            (EventType.TTS_STARTED, self._on_tts),
            (EventType.TTS_FINISHED, self._on_tts),
            (EventType.OBSERVATION, self._on_observation),
            (EventType.NOTIFICATION, self._on_notification),
            (EventType.MEMORY_CHANGED, self._on_memory),
            (EventType.SETTINGS_CHANGED, self._on_settings),
            (EventType.LOG, self._on_log),
        ]
        for event_type, handler in subs:
            try:
                self._unsubscribers.append(bus.subscribe(event_type, handler))
            except Exception:  # noqa: BLE001
                log.debug("could not subscribe to %s", event_type, exc_info=True)
        # The resource guard publishes metrics on its own thread; mirror the
        # latest snapshot so the status bar has something before the first poll.
        try:
            snap = self.runtime.guard.latest
            self.metrics = snap.as_dict()
        except Exception:  # noqa: BLE001
            pass

    def shutdown(self) -> None:
        for unsubscribe in self._unsubscribers:
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        self._unsubscribers.clear()

    # ------------------------------------------------------------------
    # queue plumbing
    # ------------------------------------------------------------------
    def _push(self, kind: str, data: Any = None) -> None:
        try:
            self._queue.put_nowait(UiUpdate(kind=kind, data=data))
        except queue.Full:
            # Drop the oldest rather than block a worker thread on the UI.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(UiUpdate(kind=kind, data=data))
            except Exception:  # noqa: BLE001
                pass

    def drain(self, limit: int = 60) -> List[UiUpdate]:
        """Pop up to ``limit`` pending updates (called from the GUI thread)."""
        out: List[UiUpdate] = []
        for _ in range(limit):
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out

    def pending(self) -> int:
        return self._queue.qsize()

    def add_line(self, line: ConversationLine) -> None:
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > self.max_lines:
                del self.lines[: len(self.lines) - self.max_lines]

    def history(self, limit: int = 200) -> List[ConversationLine]:
        with self._lock:
            return list(self.lines[-limit:])

    # ------------------------------------------------------------------
    # actions the UI can trigger (all hand off to runtime threads)
    # ------------------------------------------------------------------
    def submit(self, text: str, source: str = "text") -> bool:
        text = (text or "").strip()
        if not text:
            return False
        if self.runtime.agent.busy:
            self._push(K_NOTIFICATION, {"title": "Busy",
                                        "message": "Still working on the last request."})
            return False
        self._push(K_MESSAGE, ConversationLine(role="user", text=text).as_dict())
        self.busy = True
        self.runtime.agent.submit(text, source=source)
        return True

    def stop(self) -> None:
        self.runtime.agent.cancel()
        self._push(K_NOTIFICATION, {"title": "Stopped",
                                    "message": "Task cancelled."})

    def resolve_approval(self, request_id: int, outcome: Outcome, note: str = "",
                         answer: str = "") -> bool:
        return bool(self.runtime.broker.resolve(int(request_id), outcome,
                                                note=note, answer=answer))

    def approvals(self) -> List[Dict[str, Any]]:
        return self.runtime.broker.pending()

    def refresh_models(self) -> None:
        threading.Thread(target=self.runtime.refresh_models, args=(True,),
                         name="jarvis-models-refresh", daemon=True).start()

    def listen_once(self) -> None:
        speech = getattr(self.runtime, "speech", None)
        if speech is None:
            self._push(K_NOTIFICATION, {"title": "Voice unavailable",
                                        "message": "No speech subsystem is loaded."})
            return
        threading.Thread(target=self._listen_worker, args=(speech,),
                         name="jarvis-listen", daemon=True).start()

    def _listen_worker(self, speech: Any) -> None:
        transcript = speech.listen_once(timeout_s=12.0, source="button")
        if transcript.ok:
            self.submit(transcript.text, source="voice")

    def set_listening_mode(self, mode: str) -> None:
        speech = getattr(self.runtime, "speech", None)
        if speech is None:
            return
        threading.Thread(target=speech.set_mode, args=(mode,),
                         name="jarvis-speech-mode", daemon=True).start()

    def save_settings(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Apply ``{"section": {"key": value}}`` and re-sanitise on disk."""
        from .. import config as config_mod

        def _apply(settings: Any) -> None:
            for section, values in (changes or {}).items():
                target = getattr(settings, section, None)
                if target is None or not isinstance(values, dict):
                    continue
                for key, value in values.items():
                    if hasattr(target, key):
                        try:
                            setattr(target, key, value)
                        except Exception:  # noqa: BLE001
                            log.warning("Could not set %s.%s", section, key)

        try:
            settings = config_mod.update(_apply)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        self.runtime.reload_settings()
        self._push(K_SETTINGS, settings.to_dict())
        return {"ok": True, "settings": settings.to_dict()}

    def status(self) -> Dict[str, Any]:
        try:
            return self.runtime.status().as_dict()
        except Exception as exc:  # noqa: BLE001
            log.debug("status failed: %s", exc)
            return {"error": str(exc)}

    def platform_summary(self) -> str:
        try:
            return self.runtime.platform_summary()
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------
    # event handlers (worker threads)
    # ------------------------------------------------------------------
    def _on_state(self, event: Any) -> None:
        data = event.data or {}
        self.state = str(data.get("state", self.state))
        if self.state in (AgentState.IDLE.value,):
            self.busy = False
        self._push(K_STATE, {"state": self.state})

    def _on_user_message(self, event: Any) -> None:
        data = event.data or {}
        line = ConversationLine(role="user", text=str(data.get("text", "")))
        self.add_line(line)
        self._push(K_MESSAGE, line.as_dict())

    def _on_assistant_message(self, event: Any) -> None:
        data = event.data or {}
        line = ConversationLine(role="assistant", text=str(data.get("text", "")),
                                task_id=str(data.get("task_id", "")))
        self.add_line(line)
        self._stream_buffer.clear()
        self._push(K_MESSAGE, line.as_dict())

    def _on_stream_token(self, event: Any) -> None:
        data = event.data or {}
        token = str(data.get("token", ""))
        if not token:
            return
        buffer = self._stream_buffer.setdefault("current", [])
        buffer.append(token)
        # Coalesce: push at most a few times a second to keep the UI smooth.
        if len(buffer) % 4 == 0:
            self._push(K_STREAM, {"text": "".join(buffer)})

    def _on_thought(self, event: Any) -> None:
        self._push(K_THOUGHT, event.data or {})

    def _on_task_started(self, event: Any) -> None:
        self.busy = True
        self._push(K_TASK, {"stage": "started", **(event.data or {})})

    def _on_task_progress(self, event: Any) -> None:
        self._push(K_TASK, {"stage": "progress", **(event.data or {})})

    def _on_task_finished(self, event: Any) -> None:
        self.busy = False
        self._push(K_TASK, {"stage": "finished", **(event.data or {})})

    def _on_tool_call(self, event: Any) -> None:
        self._push(K_TOOL, {"phase": "call", **(event.data or {})})

    def _on_tool_result(self, event: Any) -> None:
        self._push(K_TOOL, {"phase": "result", **(event.data or {})})

    def _on_approval_requested(self, event: Any) -> None:
        data = dict(event.data or {})
        request_id = data.get("id")
        if data.get("kind") == "question":
            self.pending_approvals[int(request_id)] = data
            self._push(K_QUESTION, data)
            return
        if request_id is not None:
            self.pending_approvals[int(request_id)] = data
        self._push(K_APPROVAL, data)

    def _on_approval_resolved(self, event: Any) -> None:
        data = dict(event.data or {})
        request_id = data.get("id")
        if request_id is not None:
            self.pending_approvals.pop(int(request_id), None)
        self._push(K_APPROVAL_RESOLVED, data)

    def _on_action_blocked(self, event: Any) -> None:
        data = dict(event.data or {})
        self._push(K_NOTIFICATION, {
            "title": "Action refused",
            "message": f"{data.get('tool', 'An action')} was blocked: "
                       f"{data.get('reason', data.get('rule', ''))}"})
        line = ConversationLine(role="system",
                                text=f"Refused: {data.get('tool', '')} - "
                                     f"{data.get('reason', '')}", ok=False)
        self.add_line(line)
        self._push(K_MESSAGE, line.as_dict())

    def _on_ollama(self, event: Any) -> None:
        self._push(K_OLLAMA, event.data or {})

    def _on_models(self, event: Any) -> None:
        self._push(K_MODELS, event.data or {})

    def _on_metrics(self, event: Any) -> None:
        data = event.data or {}
        if isinstance(data, dict):
            self.metrics = data
        self._push(K_METRICS, data)

    def _on_resource_warning(self, event: Any) -> None:
        data = dict(event.data or {})
        pressure = str(data.get("pressure", "soft")).upper()
        self._push(K_NOTIFICATION, {
            "title": f"Resource pressure: {pressure}",
            "message": "JARVIS is shedding load to stay inside its memory "
                       "ceilings (12 GB RAM / 8 GB VRAM)."})
        self._push(K_METRICS, data)

    def _on_stt(self, event: Any) -> None:
        data = dict(event.data or {})
        self._push(K_STT, {"kind": "final", **data})

    def _on_stt_partial(self, event: Any) -> None:
        self._push(K_STT, {"kind": "partial", **(event.data or {})})

    def _on_stt_error(self, event: Any) -> None:
        data = dict(event.data or {})
        self._push(K_STT, {"kind": "error", **data})
        self._push(K_NOTIFICATION, {"title": "Microphone",
                                    "message": str(data.get("error", ""))[:200]})

    def _on_listening(self, event: Any) -> None:
        self._push(K_SPEECH, {"kind": "listening", **(event.data or {})})

    def _on_wake(self, event: Any) -> None:
        data = dict(event.data or {})
        self._push(K_WAKE, data)
        self._push(K_NOTIFICATION, {"title": "Wake word",
                                    "message": f"Heard '{data.get('phrase', '')}'"})

    def _on_level(self, event: Any) -> None:
        self._push(K_LEVEL, event.data or {})

    def _on_tts_level(self, event: Any) -> None:
        self._push(K_TTS_LEVEL, event.data or {})

    def _on_tts(self, event: Any) -> None:
        self._push(K_SPEECH, {"kind": "tts", **(event.data or {})})

    def _on_observation(self, event: Any) -> None:
        data = event.data or {}
        if isinstance(data, dict):
            # Screenshots can be large; the UI only wants the summary fields.
            data = {k: v for k, v in data.items()
                    if k not in ("image_b64", "elements", "prompt_text")}
        self._push(K_OBSERVATION, data)

    def _on_notification(self, event: Any) -> None:
        data = dict(event.data or {})
        self.last_notification = data
        self._push(K_NOTIFICATION, data)

    def _on_memory(self, event: Any) -> None:
        self._push(K_MEMORY, event.data or {})

    def _on_settings(self, event: Any) -> None:
        self._push(K_SETTINGS, event.data or {})

    def _on_log(self, event: Any) -> None:
        self._push(K_LOG, event.data or {})
