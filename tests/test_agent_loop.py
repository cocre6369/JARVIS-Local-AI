"""The end-to-end agent loop, on a simulated desktop.

These are the acceptance scenarios from the design document, executed through
the *real* agent: intent classification, planning, observation, tool
authorisation, execution, verification and the final report.
"""

from __future__ import annotations

import json

import pytest

from conftest import final_answer, tool_call


def test_spotify_flow_plays_the_requested_song(runtime):
    scripted = runtime.ollama
    scripted.queue(
        tool_call("launch_application", name="Spotify"),
        tool_call("set_element_value", description="search",
                  text="Violent Crimes Kanye West"),
        tool_call("press_keys", keys="enter"),
        tool_call("click_element", description="Violent Crimes - Kanye West"),
        tool_call("click_element", description="Play"),
        tool_call("media_control", action="status"),
        final_answer("Violent Crimes by Kanye West is playing on Spotify."),
    )

    reply = runtime.agent.handle("Open Spotify and play Violent Crimes by Kanye West")

    assert reply.ok, reply.error or reply.text
    assert len(reply.steps) >= 5
    for step in reply.steps:
        assert step.ok, f"{step.tool}: {step.message}"
    assert not reply.blocked and not reply.denied

    window = runtime.platform.simulated.window_for_app("Spotify")
    state = window.state
    now_playing = state.get("now_playing") or {}
    assert now_playing.get("title") == "Violent Crimes"
    assert now_playing.get("artist") == "Kanye West"
    assert state.get("playing") is True
    assert "Violent Crimes" in reply.text

    # Every executed step reported verification: the loop never assumes success.
    verified = [s for s in reply.steps if s.verified is not None]
    assert verified, "no step produced a verification"
    assert all(s.verified for s in verified)


def test_prohibited_action_is_refused_without_running(runtime):
    scripted = runtime.ollama
    scripted.queue(
        tool_call("run_command", command="reg delete HKLM\\Software\\Microsoft\\Windows /f",
                  reason="the user asked"),
        final_answer("I cannot do that: the registry is off limits."),
    )

    reply = runtime.agent.handle("delete that registry key for me")

    assert "run_command" in reply.blocked
    assert not reply.approvals
    executed = [s.tool for s in reply.steps if s.ok]
    assert "run_command" not in executed
    # The audit log must contain the refusal.
    rows = runtime.audit.recent(limit=10)
    assert any(row.get("decision") in ("blocked", "denied") for row in rows)


def test_approval_gated_delete_runs_only_when_approved(runtime, auto_approve):
    scripted = runtime.ollama
    scripted.queue(
        tool_call("delete_file", path="~/Downloads/old.txt"),
        final_answer("old.txt is gone."),
    )

    reply = runtime.agent.handle("delete old.txt from my downloads")

    assert "delete_file" in reply.approvals
    delete_steps = [s for s in reply.steps if s.tool == "delete_file"]
    assert delete_steps and delete_steps[0].approval == "approved"


def test_declined_approval_stops_the_action(runtime, deny_all):
    scripted = runtime.ollama
    scripted.queue(
        tool_call("delete_file", path="~/Downloads/old.txt"),
        final_answer("You said no, so I left it alone."),
    )

    reply = runtime.agent.handle("delete old.txt from my downloads")

    assert "delete_file" in reply.denied
    assert not any(s.tool == "delete_file" and s.ok for s in reply.steps)


def test_unknown_tool_is_corrected_not_crashed(runtime):
    runtime.ollama.queue(
        __import__("conftest").ScriptedResult(
            "", [{"function": {"name": "definitely_not_a_tool", "arguments": {}}}]),
        final_answer("I used the wrong tool name first; nothing happened."),
    )

    reply = runtime.agent.handle("do something impossible")

    assert reply.steps
    assert reply.steps[0].ok is False
    assert "unknown tool" in reply.steps[0].message


def test_transient_model_error_recovers_then_reports(runtime):
    """auto_recover retries once; a server that stays down must be reported."""
    from conftest import ScriptedResult

    runtime.ollama.queue(ScriptedResult(error="connection refused"),
                         ScriptedResult(error="connection refused"))
    reply = runtime.agent.handle("list my open windows")
    # Honest reporting: the user is told the model server is unreachable, and
    # nothing was executed.  ok may stay True because a clear explanation is a
    # valid answer - what must never happen is silent success with side effects.
    assert "ollama" in (reply.text or "").lower()
    assert not any(s.ok for s in reply.steps)
    assert not reply.approvals


def test_single_transient_error_is_retried(runtime):
    from conftest import ScriptedResult

    runtime.ollama.queue(ScriptedResult(error="connection refused"),
                         final_answer("Recovered and answered."))
    reply = runtime.agent.handle("what time is it")
    assert reply.ok is True
    assert len(runtime.ollama.calls) >= 2


def test_session_memory_records_the_exchange(runtime):
    runtime.ollama.queue(final_answer("I remember that."))
    runtime.agent.handle("my favourite colour is teal")
    sessions = runtime.memory.sessions(limit=5)
    assert sessions, "no session was persisted"
    history = runtime.memory.history(sessions[0]["session_id"], limit=10)
    roles = [row.get("role") for row in history]
    assert "user" in roles and "assistant" in roles


def test_shortcut_replay_skips_the_model(runtime):
    """A remembered flow must run without another model call."""
    scripted = runtime.ollama
    scripted.queue(
        tool_call("launch_application", name="Notepad"),
        tool_call("read_screen_text"),
        final_answer("Notepad is open."),
    )
    first = runtime.agent.handle("open notepad please")
    assert first.ok
    assert runtime.memory.shortcuts(), "the finished flow was not remembered"

    calls_before = len(scripted.calls)
    scripted.queue(final_answer("Notepad is open again."))
    second = runtime.agent.handle("open notepad please")
    assert second.ok
    assert second.used_shortcut is True
    assert len(scripted.calls) == calls_before, "shortcut replay made a model call"
