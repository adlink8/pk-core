from __future__ import annotations

import pytest

from dataclasses import replace

from personal_knowledge.intelligence.schema import SnapshotBinding, canonical_json
from personal_knowledge.intelligence.changes import TrendSample, derive_risk, derive_trend
from personal_knowledge.intelligence.state_projection import StateKey
from personal_knowledge.intelligence.state_projection import (
    ProjectionError,
    normalize_candidates,
    plan_projection_run,
    project_current_state,
)
from tests.unit.test_personal_state_projection import _candidate, _run, _validated


def _snapshot() -> SnapshotBinding:
    return SnapshotBinding(
        snapshot_id="ss1",
        snapshot_hash="hash1",
        members={
            "canonical_knowledge": {
                "artifact_version_id": "av1",
                "privacy_class": "R4",
            }
        },
    )


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"derivation": "synthesis", "provenance_class": "fact"}, "provenance_rule_violation"),
        (
            {
                "assertion_kind": "observation",
                "derivation": "canonical_fact",
                "provenance_class": "fact",
            },
            "observation_as_fact",
        ),
        ({"derivation": "occurrence", "provenance_class": "inference"}, "provenance_rule_violation"),
    ],
)
def test_provenance_classes_cannot_be_silently_promoted(
    overrides: dict, code: str
) -> None:
    with pytest.raises(ProjectionError) as error:
        normalize_candidates([_candidate(**overrides)], snapshot=_snapshot())
    assert error.value.code == code


def test_candidate_and_evidence_must_share_one_snapshot() -> None:
    with pytest.raises(ProjectionError) as candidate:
        normalize_candidates([_candidate(snapshot_id="ss2")], snapshot=_snapshot())
    assert candidate.value.code == "mixed_snapshot"

    row = _candidate()
    row["evidence"][0]["snapshot_hash"] = "other"
    with pytest.raises(ProjectionError) as evidence:
        normalize_candidates([row], snapshot=_snapshot())
    assert evidence.value.code == "mixed_snapshot"


def test_snapshot_member_version_is_authoritative() -> None:
    row = _candidate()
    row["evidence"][0]["artifact_version_id"] = "av-other"
    with pytest.raises(ProjectionError) as error:
        normalize_candidates([row], snapshot=_snapshot())
    assert error.value.code == "evidence_version_mismatch"


def test_only_explicit_canonical_facts_retain_fact_provenance() -> None:
    normalized = normalize_candidates(
        [
            _candidate(
                assertion_kind="constraint",
                derivation="canonical_fact",
                provenance_class="fact",
            )
        ],
        snapshot=_snapshot(),
    )
    assert normalized[0].provenance_class == "fact"


@pytest.mark.parametrize("field", ["snapshot_id", "provenance_class", "observed_at"])
def test_required_lineage_and_temporal_fields_fail_closed(field: str) -> None:
    row = _candidate()
    row.pop(field)
    with pytest.raises(ProjectionError) as error:
        normalize_candidates([row], snapshot=_snapshot())
    assert error.value.code == "missing_field"
    assert error.value.detail == field


def test_pending_lifecycle_proposal_has_zero_projection_effect() -> None:
    assertion = _validated(
        "a1",
        value="D",
        valid_from="2026-06-01T00:00:00Z",
        observed_at="2026-06-01T01:00:00Z",
    )
    base = project_current_state(
        [_run("run", assertion)], as_of="2026-07-18T00:00:00Z"
    )
    pending = project_current_state(
        [_run("run", assertion)],
        as_of="2026-07-18T00:00:00Z",
        history_rows=[
            {
                "unit_id": "ku-pending",
                "subject": "user",
                "status": "pending",
                "decision": "pending",
                "supersedes_id": "ku-old",
                "lifecycle_events": [
                    {
                        "event_id": "not-applied",
                        "event_type": "conflict",
                        "reason": "proposal only",
                    }
                ],
            }
        ],
    )
    assert canonical_json(base) == canonical_json(pending)


def test_reviewed_lifecycle_history_is_metadata_only_and_explainable() -> None:
    assertion = _validated(
        "a1",
        value="D",
        valid_from="2026-06-01T00:00:00Z",
        observed_at="2026-06-01T01:00:00Z",
    )
    state = project_current_state(
        [_run("run", assertion)],
        as_of="2026-07-18T00:00:00Z",
        history_rows=[
            {
                "unit_id": "ku-new",
                "subject": "user",
                "status": "current",
                "supersedes_id": "ku-missing-from-window",
                "lifecycle_events": [
                    {
                        "event_id": "event-1",
                        "event_type": "supersede",
                        "lifecycle_before": "current",
                        "lifecycle_after": "superseded",
                        "reviewer_id_hash": "reviewer-hash",
                        "actor_id": "human-operator",
                        "reason": "reviewed correction reason",
                        "created_at": "2026-07-01T00:00:00Z",
                    }
                ],
            }
        ],
    ).states[0]
    assert state.lifecycle_path[0].event_id == "event-1"
    assert not hasattr(state.lifecycle_path[0], "reason")
    assert "missing_predecessor" in state.uncertainty


def test_projection_rejects_cross_snapshot_evidence_versions() -> None:
    assertion = _validated(
        "a1",
        value="D",
        valid_from="2026-06-01T00:00:00Z",
        observed_at="2026-06-01T01:00:00Z",
        artifact_version_id="av-other",
    )
    with pytest.raises(ProjectionError) as error:
        project_current_state(
            [_run("run", assertion)], as_of="2026-07-18T00:00:00Z"
        )
    assert error.value.code == "evidence_snapshot_mismatch"


def test_projection_rejects_mixed_run_snapshots() -> None:
    assertion = _validated(
        "a1",
        value="D",
        valid_from="2026-06-01T00:00:00Z",
        observed_at="2026-06-01T01:00:00Z",
    )
    first = _run("run-1", assertion)
    other_snapshot = replace(
        first.snapshot, snapshot_id="ss2", snapshot_hash="hash2"
    )
    second = replace(first, run_id="run-2", snapshot=other_snapshot)
    with pytest.raises(ProjectionError) as error:
        project_current_state(
            [first, second], as_of="2026-07-18T00:00:00Z"
        )
    assert error.value.code == "mixed_snapshot"


def test_projection_planning_delegates_to_atomic_run_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    captured: dict = {}
    marker = object()

    def fake_plan_run(db_path, assertions, **kwargs):
        captured.update(
            {
                "db_path": db_path,
                "assertions": tuple(assertions),
                **kwargs,
            }
        )
        return marker

    monkeypatch.setattr(
        "personal_knowledge.intelligence.state_projection.plan_run", fake_plan_run
    )
    result = plan_projection_run(
        tmp_path / "unused.sqlite",
        [_candidate()],
        snapshot=_snapshot(),
        producer_version="projection-v1",
        input_manifest={"source": "fixture"},
        resolver="resolver-marker",
    )
    assert result is marker
    assert captured["snapshot_id"] == "ss1"
    assert captured["producer_version"] == "projection-v1"
    assert captured["resolver"] == "resolver-marker"
    assert len(captured["assertions"]) == 1


def test_trend_and_risk_outputs_can_never_claim_fact_provenance() -> None:
    key = StateKey("constraint", "user", "health", "weekly", "load")
    samples = tuple(
        TrendSample(
            assertion_id=f"a{index}",
            key=key,
            value=float(index),
            unit="count",
            observed_at=f"2026-0{index}-01T00:00:00Z",
            evidence_refs=(f"cm|{index}",),
            evidence_eligible=True,
            confidence=0.9,
        )
        for index in range(1, 4)
    )
    trend = derive_trend(samples)
    risk = derive_risk(trend, rule_id="increasing_constraint_pressure")
    assert trend.provenance_class == "inference"
    assert risk.provenance_class == "inference"
    assert "fact" not in canonical_json((trend, risk))


def test_ineligible_evidence_vetoes_trend_and_downstream_risk() -> None:
    key = StateKey("constraint", "user", "health", "weekly", "load")
    samples = tuple(
        TrendSample(
            assertion_id=f"a{index}",
            key=key,
            value=float(index),
            unit="count",
            observed_at=f"2026-0{index}-01T00:00:00Z",
            evidence_refs=(f"cm|{index}",),
            evidence_eligible=index != 2,
            confidence=0.9,
        )
        for index in range(1, 4)
    )
    trend = derive_trend(samples)
    risk = derive_risk(trend, rule_id="increasing_constraint_pressure")
    assert trend.result_status == "uncertain"
    assert "evidence_ineligible" in trend.uncertainty
    assert risk.result_status == "uncertain"
    assert risk.severity is None
