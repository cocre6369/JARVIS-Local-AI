"""A tiny, thread-safe publish/subscribe bus.

The agent, the speech engines and the resource guard all run on worker
threads.  They never touch Qt widgets directly - they publish events, and the
UI subscribes and marshals them onto the GUI thread.  This keeps the interface
responsive no matter how long the model takes to think.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


class EventType(str, Enum):
    # --- agent state ---------------------------------------------------
    STATE_CHANGED = "state_changed"          # idle/listening/thinking/executing/speaking/error
    TASK_STARTED = "task_started"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    THOUGHT = "thought"                      # model reasoning text
    STREAM_TOKEN = "stream_token"            # incremental assistant text
    # --- conversation --------------------------------------------------
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    # --- tools ---------------------------------------------------------
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    # --- security ------------------------------------------------------
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    ACTION_BLOCKED = "action_blocked"
    AUDIT = "audit"
    # --- observation ---------------------------------------------------
    OBSERVATION = "observation"
    SCREENSHOT = "screenshot"
    # --- speech --------------------------------------------------------
    STT_PARTIAL = "stt_partial"
    STT_FINAL = "stt_final"
    STT_ERROR = "stt_error"
    LISTENING_STARTED = "listening_started"
    LISTENING_STOPPED = "listening_stopped"
    WAKEWORD_DETECTED = "wakeword_detected"
    TTS_STARTED = "tts_started"
    TTS_FINISHED = "tts_finished"
    TTS_LEVEL = "tts_level"                  # audio-reactive visualiser
    AUDIO_LEVEL = "audio_level"              # microphone level
    # --- system --------------------------------------------------------
    OLLAMA_STATUS = "ollama_status"
    MODELS_CHANGED = "models_changed"
    MODEL_SELECTED = "model_selected"
    METRICS = "metrics"                      # cpu/ram/vram/gpu
    RESOURCE_WARNING = "resource_warning"
    NOTIFICATION = "notification"
    LOG = "log"
    SETTINGS_CHANGED = "settings_changed"
    MEMORY_CHANGED = "memory_changed"


class AgentState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    EXECUTING = "executing"
    OBSERVING = "observing"
    SPEAKING = "speaking"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    ERROR = "error"


@dataclass
class Event:
    type: EventType
    data: Any = None
    ts: float = field(default_factory=time.time)
    source: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(self.data, dict):
            return self.data.get(key, default)
        return getattr(self.data, key, default)


Subscriber = Callable[[Event], None]


class EventBus:
    """Synchronous dispatch with per-subscriber error isolation.

    Subscribers are expected to be cheap (typically they just push the event
    into a Qt signal).  Expensive work belongs on a worker thread.
    """

    def __init__(self) -> None:
        self._subs: Dict[EventType, List[Subscriber]] = {}
        self._wildcard: List[Subscriber] = []
        self._lock = threading.RLock()
        self._history: queue.Queue[Event] = queue.Queue(maxsize=500)
        self.history: List[Event] = []

    def subscribe(self, event_type: Optional[EventType], handler: Subscriber) -> Callable[[], None]:
        with self._lock:
            if event_type is None:
                self._wildcard.append(handler)
            else:
                self._subs.setdefault(event_type, []).append(handler)

        def unsubscribe() -> None:
            with self._lock:
                if event_type is None:
                    if handler in self._wildcard:
                        self._wildcard.remove(handler)
                else:
                    lst = self._subs.get(event_type, [])
                    if handler in lst:
                        lst.remove(handler)

        return unsubscribe

    def publish(self, event_type: EventType, data: Any = None, source: str = "") -> Event:
        ev = Event(type=event_type, data=data, source=source)
        self._record(ev)
        with self._lock:
            handlers = list(self._subs.get(event_type, ()))
            wild = list(self._wildcard)
        for h in handlers + wild:
            try:
                h(ev)
            except Exception:  # noqa: BLE001
                log.exception("Event subscriber failed for %s", event_type)
        return ev

    def _record(self, ev: Event) -> None:
        try:
            self._history.put_nowait(ev)
        except queue.Full:
            try:
                self._history.get_nowait()
                self._history.put_nowait(ev)
            except queue.Empty:
                pass
        with self._lock:
            self.history.append(ev)
            if len(self.history) > 500:
                del self.history[:-500]

    def recent(self, limit: int = 50, event_type: Optional[EventType] = None) -> List[Event]:
        with self._lock:
            items = list(self.history)
        if event_type is not None:
            items = [e for e in items if e.type == event_type]
        return items[-limit:]

    def clear(self) -> None:
        with self._lock:
            self.history.clear()


#: Process-wide default bus (the UI and the core share it).
bus = EventBus()
