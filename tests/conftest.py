"""Shared fixtures.

Everything runs against a **simulated desktop** (``JARVIS_DEMO=1``) inside a
temporary data directory, and against a *scripted* stand-in for Ollama.  That
keeps the suite hermetic: no model download, no network, no display, and no
chance of touching a real machine's files.

The scripted client is not a fake feature - the real agent loop, policy engine,
approval broker, verification and simulated platform all execute exactly as
they would in the application; only the model's replies are supplied by the
test so the behaviour is deterministic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("JARVIS_DEMO", "1")


class ScriptedResult:
    """Minimal stand-in for :class:`jarvis.llm.ollama_client.ChatResult`."""

    def __init__(self, content: str = "", tool_calls: Optional[List[Dict[str, Any]]] = None,
                 error: str = "") -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.model = "scripted:latest"
        self.error = error
        self.eval_count = 12
        self.prompt_eval_count = 34
        self.total_duration_ms = 25.0
        self.tokens_per_second = 480.0
        self.done_reason = "stop"
        self.images_used = 0
        self.used_tools_api = bool(self.tool_calls)

    @property
    def ok(self) -> bool:
        return not self.error


class ScriptedOllama:
    """Replies from a queue; records every call the agent made."""

    def __init__(self, script: Optional[List[ScriptedResult]] = None) -> None:
        self.script = list(script or [])
        self.calls: List[Dict[str, Any]] = []
        self._default = ScriptedResult('{"answer": "Done."}')

    def queue(self, *results: ScriptedResult) -> None:
        self.script.extend(results)

    def chat(self, messages, model=None, tools=None, images=None, stream=False,
             on_token=None, options=None, keep_alive=None, response_format=None,
             timeout=None, **_kw: Any) -> ScriptedResult:
        self.calls.append({"model": model, "tools": bool(tools),
                           "messages": len(messages), "images": len(images or [])})
        result = self.script.pop(0) if self.script else self._default
        if on_token and result.content:
            on_token(result.content)
        return result

    # The runtime pokes these during start-up and status refreshes.
    def status(self) -> Dict[str, Any]:
        return {"connected": True, "error": "", "base_url": "http://127.0.0.1:11434",
                "version": "scripted", "cached": False}

    def list_models(self, force: bool = False, **_kw: Any) -> List[Any]:
        return []

    def running_models(self) -> List[Any]:
        return []

    def ping(self) -> bool:
        return True

    def version(self) -> str:
        return "scripted"

    def unload(self, model: str) -> bool:
        return True

    def warm(self, model: str) -> bool:
        return True


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> Path:
    """An isolated settings/memory/log directory per test."""
    target = tmp_path / "jarvis-data"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(target))
    from jarvis import config as config_mod

    config_mod._cached = None
    config_mod.load(force=True)
    yield target
    config_mod._cached = None


@pytest.fixture()
def runtime(data_dir):
    """A demo-mode runtime wired to a scripted model."""
    from jarvis import config as config_mod
    from jarvis.core.runtime import build_runtime
    from jarvis.llm.router import RoleResolution

    def _select(settings: Any) -> None:
        settings.ollama.main_model = "scripted:latest"
        settings.ollama.fast_model = "scripted:latest"
        settings.speech.stt_enabled = False
        settings.speech.tts_enabled = False
        settings.general.auto_check_ollama_on_start = False
        settings.vision.observation_interval_ms = 4000

    config_mod.update(_select)

    instance = build_runtime(with_speech=False)
    scripted = ScriptedOllama()
    instance.ollama = scripted
    instance.ctx.ollama = scripted

    def _resolve(role, needs=None):
        return RoleResolution(role=role, model="scripted:latest", ok=True,
                              capabilities={"tools": True, "vision": True})

    instance.router.resolve = _resolve
    instance.start(background=False)
    yield instance
    try:
        instance.shutdown()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture()
def auto_approve(runtime):
    """Answer every approval request with 'yes', like a user clicking Approve."""
    from jarvis.core.broker import Outcome

    runtime.broker.set_responder(lambda request: Outcome.APPROVED)
    yield
    runtime.broker.set_responder(None)


@pytest.fixture()
def deny_all(runtime):
    """Answer every approval request with 'no'."""
    from jarvis.core.broker import Outcome

    runtime.broker.set_responder(lambda request: Outcome.DENIED)
    yield
    runtime.broker.set_responder(None)


def tool_call(tool_name: str, **arguments: Any) -> ScriptedResult:
    """One scripted model turn that emits a single native tool call."""
    return ScriptedResult("", [{"function": {"name": tool_name,
                                             "arguments": arguments}}])


def final_answer(text: str) -> ScriptedResult:
    return ScriptedResult('{"thought": "finishing", "answer": "%s"}'
                          % text.replace('"', "'"))


__all__ = ["ScriptedOllama", "ScriptedResult", "final_answer", "tool_call"]
