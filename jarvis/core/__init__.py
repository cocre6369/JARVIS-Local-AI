"""The brain: agent loop, session state, planning, approvals and wiring.

Import order here is deliberately small - ``runtime`` pulls in the platform,
vision, tools and speech subsystems, so anything that only needs the agent
should import :mod:`jarvis.core.agent` directly.
"""

from __future__ import annotations

from .agent import Agent, AgentReply
from .broker import ApprovalBroker, ApprovalRequest, Outcome
from .intents import Intent, classify
from .planner import Plan, PlanStep, heuristic_plan, plan_from_shortcuts
from .session import Session, Step, TaskState, new_session_id, new_task_id

__all__ = [
    "Agent", "AgentReply", "ApprovalBroker", "ApprovalRequest", "Intent",
    "Outcome", "Plan", "PlanStep", "Session", "Step", "TaskState", "classify",
    "heuristic_plan", "new_session_id", "new_task_id", "plan_from_shortcuts",
]


def build_runtime(**kwargs):
    """Lazy import so ``import jarvis.core`` stays cheap."""
    from .runtime import build_runtime as _build

    return _build(**kwargs)


def JarvisRuntime(**kwargs):  # noqa: N802 - mirrors the class name for callers
    from .runtime import JarvisRuntime as _Runtime

    return _Runtime(**kwargs)
