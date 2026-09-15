"""MCP proactive 域 handler：proactive_* 只读工具。

从 services/mcp_server.py 原样拆出的 proactive_tool_contract。
"""

from __future__ import annotations

from pathlib import Path

from personal_knowledge.intelligence.proactive.service import ProactiveIntelligenceService  # noqa: E402
from personal_knowledge.core.project_paths import UNIFIED_DB  # noqa: E402
from personal_knowledge.mcp_tools.handlers._format import _json_contract  # noqa: E402

TOOL_NAMES = frozenset({
    "proactive_inbox",
    "proactive_digest",
    "proactive_candidate_get",
    "proactive_candidate_explain",
    "proactive_controls_status",
    "proactive_metrics",
})


def proactive_tool_contract(name: str, arguments: dict, *, db_path: Path | None = None) -> dict:
    """Thin MCP adapter over proactive read operations only."""
    operation = {
        "proactive_inbox": "inbox.list", "proactive_digest": "digest.get",
        "proactive_candidate_get": "candidates.get", "proactive_candidate_explain": "candidates.explain",
        "proactive_controls_status": "controls.status", "proactive_metrics": "metrics.get",
    }.get(name)
    if operation is None:
        return ProactiveIntelligenceService._error("unknown", "unknown_operation", name)
    values = {key: value for key, value in arguments.items() if value not in {None, ""}}
    return ProactiveIntelligenceService(db_path or UNIFIED_DB).invoke(operation, **values)


def render(name: str, arguments: dict) -> str:
    return _json_contract(proactive_tool_contract(name, arguments))
