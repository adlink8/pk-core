"""MCP intelligence 域 handler：personal_state_* 工具。

从 services/mcp_server.py 原样拆出的 intelligence_tool_contract。
"""

from __future__ import annotations

from pathlib import Path

from personal_knowledge.intelligence.service import IntelligenceService  # noqa: E402
from personal_knowledge.core.project_paths import UNIFIED_DB  # noqa: E402
from personal_knowledge.mcp_tools.handlers._format import _json_contract  # noqa: E402

TOOL_NAMES = frozenset({
    "personal_state_current",
    "personal_state_history",
    "personal_changes_recent",
    "personal_state_explain",
})


def intelligence_tool_contract(
    name: str,
    arguments: dict,
    *,
    db_path: Path | None = None,
    resolver=None,
) -> dict:
    """Thin MCP adapter over exactly the same service used by CLI and REST."""
    operation_by_tool = {
        "personal_state_current": "state.current",
        "personal_state_history": "state.history",
        "personal_changes_recent": "changes.recent",
        "personal_state_explain": "state.explain",
    }
    operation = operation_by_tool.get(name)
    if operation is None:
        return IntelligenceService._error("unknown", "unknown_operation", name)
    values = {key: value for key, value in arguments.items() if value not in {None, ""}}
    return IntelligenceService(db_path or UNIFIED_DB, resolver=resolver).invoke(
        operation, **values
    )


def render(name: str, arguments: dict) -> str:
    return _json_contract(intelligence_tool_contract(name, arguments))
