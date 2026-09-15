"""End-to-end proof for the four Phase 32 read-only authority surfaces."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from personal_knowledge.services.api_server import agent_read_rest_contract
from personal_knowledge.services.decision_intelligence_reads import (
    DEFAULT_ANALYSIS_DB,
    DEFAULT_CALIBRATION_DB,
    DEFAULT_PILOT_DB,
    DecisionIntelligenceReadService,
)
from personal_knowledge.services.mcp_server import agent_read_tool_contract
from personal_knowledge.core.project_paths import EXTERNAL_CONTEXT_DB


DATABASES = (
    Path(EXTERNAL_CONTEXT_DB),
    Path(DEFAULT_ANALYSIS_DB),
    Path(DEFAULT_PILOT_DB),
    Path(DEFAULT_CALIBRATION_DB),
)

LIST_CASES = (
    ("external.list", "external_context_list"),
    ("analysis.list", "decision_analysis_list"),
    ("pilot.list", "project_pilot_list"),
    ("calibration.list", "recommendation_calibration_list"),
)


def _fingerprints() -> dict[Path, str]:
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in DATABASES}


@pytest.fixture(scope="module")
def live_service() -> DecisionIntelligenceReadService:
    missing = [str(path) for path in DATABASES if not path.is_file()]
    if missing:
        pytest.skip(f"live Phase 28-31 authorities unavailable: {missing}")
    return DecisionIntelligenceReadService()


def _assert_success(result: dict, operation: str) -> None:
    assert result["ok"] is True
    assert result["operation"] == operation
    assert result["schema_version"] == "decision_intelligence_read_v1"
    assert result["privacy"] == {
        "metadata_only": True,
        "provider_bodies": 0,
        "credentials": 0,
        "writes": 0,
    }


def test_live_list_get_explain_are_transport_equivalent_and_read_only(live_service):
    before = _fingerprints()

    listed: dict[str, dict] = {}
    for operation, tool in LIST_CASES:
        direct = live_service.invoke(operation, limit=1)
        rest = agent_read_rest_contract(operation, {"limit": "1"}, service=live_service)
        mcp = agent_read_tool_contract(tool, {"limit": 1}, service=live_service)
        _assert_success(direct, operation)
        assert rest == mcp
        assert rest["schema_version"] == "agent_compact_envelope_v1"
        assert rest["data"] == direct["data"]
        listed[operation] = direct["data"]

    source_id = listed["external.list"]["sources"][0]["source_id"]
    run_id = listed["analysis.list"]["items"][0]["run_id"]
    case_id = listed["pilot.list"]["items"][0]["case_id"]
    protocol_id = listed["calibration.list"]["items"][0]["protocol_id"]
    detail_cases = (
        ("external.get", "external_context_get", {"resource_type": "source", "resource_id": source_id}),
        ("external.explain", "external_context_explain", {"resource_type": "source", "resource_id": source_id}),
        ("analysis.get", "decision_analysis_get", {"run_id": run_id}),
        ("analysis.explain", "decision_analysis_explain", {"run_id": run_id}),
        ("pilot.get", "project_pilot_get", {"case_id": case_id}),
        ("pilot.explain", "project_pilot_explain", {"case_id": case_id}),
        ("calibration.get", "recommendation_calibration_get", {"protocol_id": protocol_id}),
        ("calibration.explain", "recommendation_calibration_explain", {"protocol_id": protocol_id}),
    )
    for operation, tool, arguments in detail_cases:
        direct = live_service.invoke(operation, **arguments)
        rest = agent_read_rest_contract(operation, arguments, service=live_service)
        mcp = agent_read_tool_contract(tool, arguments, service=live_service)
        _assert_success(direct, operation)
        assert rest == mcp
        if not rest["truncated"]:
            assert rest["data"] == direct["data"]

    calibration = live_service.invoke("calibration.explain", protocol_id=protocol_id)
    serialized = str(calibration["data"]).lower()
    assert "causal" in serialized
    assert "promotion" in serialized
    assert _fingerprints() == before
