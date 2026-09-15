"""MCP orchestration 域 handler：agent_session_* 受控转换工具。

从 services/mcp_server.py 原样拆出的 orchestration_tool_contract。
"""

from __future__ import annotations

from personal_knowledge.services.orchestration_service import (  # noqa: E402
    GuardedOrchestrationInterface,
)
from personal_knowledge.services.agent_contract import compact_envelope  # noqa: E402
from personal_knowledge.mcp_tools.handlers._format import _json_contract  # noqa: E402

TOOL_NAMES = frozenset({
    "agent_session_prepare",
    "agent_session_confirm",
    "agent_session_preview",
    "agent_session_generate",
    "agent_session_publish",
    "agent_session_decide",
    "agent_session_preregister",
    "agent_session_action_start",
    "agent_session_action_complete",
    "agent_session_observe",
    "agent_session_calibrate",
    "agent_session_resume",
    "agent_session_explain",
})


def orchestration_tool_contract(
    name: str, arguments: dict, *, service: GuardedOrchestrationInterface | None = None,
) -> dict:
    """Thin stdio MCP adapter over the guarded orchestration interface."""
    if name == "agent_session_prepare":
        operation = "session.prepare"
    elif name == "agent_session_confirm":
        operation = "session.confirm"
    elif name == "agent_session_preview":
        operation = "session.preview"
    elif name in {"agent_session_resume", "agent_session_explain"}:
        operation = "session." + name.rsplit("_", 1)[-1]
    elif name.startswith("agent_session_"):
        operation = "session.execute"
        expected = name.removeprefix("agent_session_")
        if (arguments.get("preview") or {}).get("operation") != expected:
            return compact_envelope(GuardedOrchestrationInterface._envelope(operation, ok=False, code="route_operation_mismatch"))
    else:
        operation = "unknown"
    try:
        target = service or GuardedOrchestrationInterface()
    except Exception as exc:
        code = str(getattr(exc, "code", "") or str(exc) or "service_unavailable").split(":", 1)[0]
        return compact_envelope(GuardedOrchestrationInterface._envelope(operation, ok=False, code=code))
    return compact_envelope(target.invoke(operation, **arguments))


def render(name: str, arguments: dict) -> str:
    return _json_contract(orchestration_tool_contract(name, arguments))
