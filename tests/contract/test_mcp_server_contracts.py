"""P0: mcp_server tool 列表与格式化契约 smoke。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "integration" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import personal_knowledge.services.mcp_server as mcp  # noqa: E402


REQUIRED_CORE_TOOLS = {
    "search_semantic",
    "stats",
    "knowledge_status",
    "list_google_assertions",
    "get_google_assertion",
    "get_memory_profile",
    "get_memory_by_subject",
    "data_list_events",
    "data_list_memories",
    "data_list_relations",
    "data_aggregate",
    "data_export",
    "data_get_event_by_id",
    "data_get_memory_by_id",
    "data_quality_report",
}

FULL_ONLY_TOOLS = {
    "query_events",
    "get_event_detail",
    "list_categories",
    "data_timeline",
}

AGENT_READ_TOOLS = {
    "external_context_list", "external_context_get", "external_context_explain",
    "decision_analysis_list", "decision_analysis_get", "decision_analysis_explain",
    "project_pilot_list", "project_pilot_get", "project_pilot_explain",
    "recommendation_calibration_list", "recommendation_calibration_get",
    "recommendation_calibration_explain",
}


def test_tools_list_has_required_names_and_schemas() -> None:
    names = {t.name for t in mcp.active_tools()}
    missing = REQUIRED_CORE_TOOLS - names
    assert not missing, f"missing tools: {missing}"
    # default profile is core: no legacy aliases
    assert not (FULL_ONLY_TOOLS & names)
    assert "data_export_all" not in names
    assert "data_export_query" not in names
    assert len(names) >= 14
    assert AGENT_READ_TOOLS <= names
    ku = next(t for t in mcp.active_tools() if t.name == "knowledge_status")
    assert "active" in (ku.description or "").lower() or "知识" in (ku.description or "")

    for tool in mcp.active_tools():
        assert tool.name
        assert tool.description
        schema = tool.inputSchema
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"
        assert "properties" in schema


def test_full_profile_exposes_compat_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_DATA_MCP_PROFILE", "full")
    names = {t.name for t in mcp.active_tools()}
    assert FULL_ONLY_TOOLS <= names
    assert REQUIRED_CORE_TOOLS <= names


def test_search_semantic_requires_query() -> None:
    tool = next(t for t in mcp.TOOLS if t.name == "search_semantic")
    assert "query" in tool.inputSchema.get("required", [])


def test_format_stats_and_query_helpers() -> None:
    stats_text = mcp._format_stats(
        {
            "sqlite": {"events": 10, "memories": 2},
            "chroma": {"available": True, "collections": {"personal_events": 5}},
        }
    )
    assert isinstance(stats_text, str)
    assert stats_text

    query_text = mcp._format_query(
        [
            {
                "event_id": "e1",
                "source": "Agent",
                "title": "t",
                "event_time": "2026-01-01",
            }
        ]
    )
    assert "e1" in query_text or "Agent" in query_text or "t" in query_text


def test_json_contract_roundtrip() -> None:
    payload = {"ok": True, "items": [{"id": 1}], "total": 1}
    text = mcp._json_contract(payload)
    parsed = json.loads(text)
    assert parsed["ok"] is True
    assert parsed["total"] == 1


def test_event_contract_args_maps_aliases() -> None:
    args = mcp._event_contract_args(
        {
            "source": "Agent",
            "category_v2": "编程",
            "query": "pytest",
            "start_time": "2026-01-01",
            "end_time": "2026-01-31",
        }
    )
    assert args["source"] == "Agent"
    assert args["category"] == "编程"
    assert args["keyword"] == "pytest"
    assert args["time_from"] == "2026-01-01"
    assert args["time_to"] == "2026-01-31"


@pytest.mark.asyncio
async def test_list_tools_handler_returns_tools() -> None:
    tools = await mcp.handle_list_tools()
    assert tools is mcp.TOOLS or len(tools) == len(mcp.TOOLS)
