"""MCP agent 只读域 handler：external_context / decision_analysis / project_pilot /
recommendation_calibration 工具。

从 services/mcp_server.py 原样拆出的 agent_read_tool_contract。
"""

from __future__ import annotations

from personal_knowledge.services.decision_intelligence_reads import (  # noqa: E402
    DecisionIntelligenceReadService,
)
from personal_knowledge.services.agent_contract import compact_envelope  # noqa: E402
from personal_knowledge.mcp_tools.handlers._format import _json_contract  # noqa: E402

TOOL_NAMES = frozenset({
    "external_context_list",
    "external_context_get",
    "external_context_explain",
    "decision_analysis_list",
    "decision_analysis_get",
    "decision_analysis_explain",
    "project_pilot_list",
    "project_pilot_get",
    "project_pilot_explain",
    "recommendation_calibration_list",
    "recommendation_calibration_get",
    "recommendation_calibration_explain",
})


def agent_read_tool_contract(
    name: str, arguments: dict, *, service: DecisionIntelligenceReadService | None = None,
) -> dict:
    """Thin stdio MCP adapter over the Phase 32 shared read service."""
    operation = {
        "external_context_list": "external.list", "external_context_get": "external.get",
        "external_context_explain": "external.explain", "decision_analysis_list": "analysis.list",
        "decision_analysis_get": "analysis.get", "decision_analysis_explain": "analysis.explain",
        "project_pilot_list": "pilot.list", "project_pilot_get": "pilot.get",
        "project_pilot_explain": "pilot.explain", "recommendation_calibration_list": "calibration.list",
        "recommendation_calibration_get": "calibration.get",
        "recommendation_calibration_explain": "calibration.explain",
    }.get(name)
    target = service or DecisionIntelligenceReadService()
    if operation is None:
        return compact_envelope(target._error("unknown", "unknown_operation", name))
    values = {key: value for key, value in arguments.items() if value not in {None, ""}}
    return compact_envelope(target.invoke(operation, **values))


def render(name: str, arguments: dict) -> str:
    return _json_contract(agent_read_tool_contract(name, arguments))
