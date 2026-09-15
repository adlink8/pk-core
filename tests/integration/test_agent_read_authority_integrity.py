import hashlib
from pathlib import Path

import pytest

from personal_knowledge.core.project_paths import EXTERNAL_CONTEXT_DB, ROOT
from personal_knowledge.services.decision_intelligence_reads import (
    DEFAULT_ANALYSIS_DB,
    DEFAULT_CALIBRATION_DB,
    DEFAULT_PILOT_DB,
    DecisionIntelligenceReadService,
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("operation", "id_operation", "id_field", "id_source"),
    [
        ("analysis.list", "analysis.explain", "run_id", lambda d: d["items"][0]["run_id"]),
        ("pilot.list", "pilot.explain", "case_id", lambda d: d["items"][0]["case_id"]),
        ("calibration.list", "calibration.explain", "protocol_id", lambda d: d["items"][0]["protocol_id"]),
    ],
)
def test_live_authority_reads_are_bounded_and_zero_mutation(operation, id_operation, id_field, id_source):
    paths = [EXTERNAL_CONTEXT_DB, DEFAULT_ANALYSIS_DB, DEFAULT_PILOT_DB, DEFAULT_CALIBRATION_DB]
    if not all(path.exists() for path in paths):
        pytest.skip("v1.2 live authority cohort is unavailable")
    before = {path: _digest(path) for path in paths}
    service = DecisionIntelligenceReadService()

    listed = service.invoke(operation, limit=10)
    assert listed["ok"] is True
    identifier = id_source(listed["data"])
    detail = service.invoke(id_operation, **{id_field: identifier})
    assert detail["ok"] is True
    assert detail["privacy"]["writes"] == 0

    assert {path: _digest(path) for path in paths} == before


def test_live_external_read_includes_sources_snapshot_and_facts_without_mutation():
    paths = [EXTERNAL_CONTEXT_DB, DEFAULT_ANALYSIS_DB, DEFAULT_PILOT_DB, DEFAULT_CALIBRATION_DB]
    if not all(path.exists() for path in paths):
        pytest.skip("v1.2 live authority cohort is unavailable")
    before = {path: _digest(path) for path in paths}
    result = DecisionIntelligenceReadService().invoke("external.list", limit=10)
    assert result["ok"] is True
    assert len(result["data"]["sources"]) == 2
    assert result["data"]["snapshot"]["snapshot_id"].startswith("exs_")
    assert result["data"]["facts"]
    assert {path: _digest(path) for path in paths} == before


def test_live_calibration_preserves_honest_boundary():
    if not DEFAULT_CALIBRATION_DB.exists():
        pytest.skip("v1.2 calibration authority is unavailable")
    service = DecisionIntelligenceReadService()
    listed = service.invoke("calibration.list")
    assert listed["ok"] is True
    protocol_id = listed["data"]["items"][0]["protocol_id"]
    result = service.invoke("calibration.explain", protocol_id=protocol_id)
    assert result["ok"] is True
    assert result["data"]["causal_claim"] is False
    assert result["data"]["promotion_available"] is False
    assert result["data"]["external_action_available"] is False
