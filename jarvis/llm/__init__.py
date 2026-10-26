"""Local language-model layer (Ollama only)."""

from .ollama_client import (  # noqa: F401
    ChatResult,
    ModelInfo,
    OllamaClient,
    OllamaError,
    OllamaModelError,
    OllamaTimeout,
    OllamaUnavailable,
    detect_capabilities,
    is_ollama_installed,
)
from .protocol import (  # noqa: F401
    ACTION_SCHEMA,
    build_prompted_system_prompt,
    coerce_arguments,
    extract_json,
    native_tool_calls_to_actions,
    parse_agent_reply,
    render_tools_for_prompt,
    tool_schema_for,
)
from .router import (  # noqa: F401
    ROLE_FAST,
    ROLE_MAIN,
    ROLE_VISION,
    ModelRouter,
    RoleResolution,
)

__all__ = [
    "ChatResult", "ModelInfo", "OllamaClient", "OllamaError", "OllamaModelError",
    "OllamaTimeout", "OllamaUnavailable", "detect_capabilities",
    "is_ollama_installed", "ACTION_SCHEMA", "build_prompted_system_prompt",
    "coerce_arguments", "extract_json", "native_tool_calls_to_actions",
    "parse_agent_reply", "render_tools_for_prompt", "tool_schema_for",
    "ModelRouter", "RoleResolution", "ROLE_MAIN", "ROLE_VISION", "ROLE_FAST",
]
