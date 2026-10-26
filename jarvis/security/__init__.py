"""Security, permissions and resource governance.

This package is the enforcement layer required by section 15 of the project
specification.  It is deliberately independent from the language model: the
model proposes actions, this layer decides whether they may run.

Modules
-------
policy    - prohibited / approval-required / allowed classification
audit     - append-only local audit log (SQLite)
guard     - RAM / VRAM budget enforcement (12 GB / 8 GB hard ceilings)
netguard  - local-only network policy (no cloud AI, no uploads)
redaction - scrubbing of secrets from text, images and logs
"""

from .policy import (  # noqa: F401
    PolicyDecision,
    PolicyEngine,
    RequestContext,
    Risk,
    SessionGrants,
)
from .audit import AuditLog  # noqa: F401
from .guard import ResourceGuard, ResourceSnapshot  # noqa: F401
from .netguard import NetworkPolicyViolation, assert_local_endpoint  # noqa: F401

__all__ = [
    "PolicyDecision",
    "PolicyEngine",
    "RequestContext",
    "Risk",
    "SessionGrants",
    "AuditLog",
    "ResourceGuard",
    "ResourceSnapshot",
    "NetworkPolicyViolation",
    "assert_local_endpoint",
]
