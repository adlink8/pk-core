"""MCP decision 域 handler：decision_recommendation* 工具。

从 services/mcp_server.py 原样拆出的 decision_tool_contract。
"""

from __future__ import annotations

from pathlib import Path

from personal_knowledge.intelligence.decision.service import DecisionFeedbackService  # noqa: E402
from personal_knowledge.core.project_paths import UNIFIED_DB  # noqa: E402
from personal_knowledge.mcp_tools.handlers._format import _json_contract  # noqa: E402

TOOL_NAMES = frozenset({
    "decision_recommendations_list",
    "decision_recommendations_get",
    "decision_recommendation_history",
    "decision_recommendation_outcomes",
    "decision_recommendation_effectiveness",
})


def decision_tool_contract(
    name: str,
    arguments: dict,
    *,
    db_path: Path | None = None,
) -> dict:
    """Thin read-only MCP adapter over the shared decision service."""
    operation_by_tool = {
        "decision_recommendations_list": "recommendations.list",
        "decision_recommendations_get": "recommendations.get",
        "decision_recommendation_history": "recommendations.history",
        "decision_recommendation_outcomes": "recommendations.outcomes",
        "decision_recommendation_effectiveness": "recommendations.effectiveness",
    }
    operation = operation_by_tool.get(name)
    if operation is None:
        return DecisionFeedbackService._error("unknown", "unknown_operation", name)
    values = {key: value for key, value in arguments.items() if value not in {None, ""}}
    return DecisionFeedbackService(db_path or UNIFIED_DB).invoke(operation, **values)


def render(name: str, arguments: dict) -> str:
    return _json_contract(decision_tool_contract(name, arguments))
