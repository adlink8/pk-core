from __future__ import annotations

from copy import deepcopy

import pytest

from personal_knowledge.intelligence.schema import (
    PersonalStateRun,
    SnapshotBinding,
    ValidatedAssertion,
    ValidatedEvidence,
    canonical_json,
    checksum,
)
from personal_knowledge.intelligence.state_projection import (
    ProjectionError,
    StateKey,
    normalize_candidates,
    project_current_state,
)


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


def _candidate(**overrides: object) -> dict:
    value = {
        "snapshot_id": "ss1",
        "snapshot_hash": "hash1",
        "assertion_kind": "goal",
        "provenance_class": "observation",
        "derivation": "occurrence",
        "subject": "user",
        "domain": "work",
        "scope": "personal",
        "predicate": "complete_target",
        "value": "D",
        "valid_from": "2026-07-17T00:00:00Z",
        "valid_to": None,
        "observed_at": "2026-07-17T00:01:00Z",
        "confidence": 0.9,
        "uncertainty_reason": "explicit user statement",
        "evidence": [
            {
                "snapshot_id": "ss1",
                "snapshot_hash": "hash1",
                "ref": "ku2",
                "artifact_type": "knowledge_unit",
                "serving_role": "canonical_knowledge",
                "artifact_version_id": "av1",
                "privacy_class": "R4",
            },
            {
                "snapshot_id": "ss1",
                "snapshot_hash": "hash1",
                "ref": "ku1",
                "artifact_type": "knowledge_unit",
                "serving_role": "canonical_knowledge",
                "artifact_version_id": "av1",
                "privacy_class": "R4",
            },
        ],
    }
    value.update(overrides)
    return value


def test_normalization_is_order_and_evidence_order_deterministic() -> None:
    first = _candidate(predicate="first", value=1)
    second = _candidate(predicate="second", value=2)
    reversed_evidence = deepcopy(first)
    reversed_evidence["evidence"].reverse()
    left = normalize_candidates([first, second], snapshot=_snapshot())
    right = normalize_candidates([second, reversed_evidence], snapshot=_snapshot())
    assert canonical_json(left) == canonical_json(right)
    assert [item.ref for item in left[0].evidence] == ["ku1", "ku2"]


def test_invalid_interval_and_missing_evidence_fail_with_stable_codes() -> None:
    with pytest.raises(ProjectionError) as interval:
        normalize_candidates(
            [_candidate(valid_to="2026-07-16T00:00:00Z")], snapshot=_snapshot()
        )
    assert interval.value.code == "invalid_time_interval"

    with pytest.raises(ProjectionError) as evidence:
        normalize_candidates([_candidate(evidence=[])], snapshot=_snapshot())
    assert evidence.value.code == "evidence_required"


def test_source_bodies_and_secret_like_values_are_rejected() -> None:
    with pytest.raises(ProjectionError) as body:
        normalize_candidates([_candidate(content="raw source body")], snapshot=_snapshot())
    assert body.value.code == "private_payload"

    with pytest.raises(ProjectionError) as secret:
        normalize_candidates(
            [_candidate(value="password=do-not-store")], snapshot=_snapshot()
        )
    assert secret.value.code == "secret_payload"


def _validated(
    assertion_id: str,
    *,
    value: object,
    valid_from: str,
    observed_at: str,
    assertion_kind: str = "goal",
    predicate: str = "complete_target",
    confidence: float = 0.9,
    lifecycle: str = "current",
    artifact_version_id: str = "av1",
) -> ValidatedAssertion:
    return ValidatedAssertion(
        assertion_id=assertion_id,
        assertion_kind=assertion_kind,
        provenance_class="observation",
        subject="user",
        domain="work",
        scope="personal",
        predicate=predicate,
        value=value,
        valid_from=valid_from,
        valid_to=None,
        observed_at=observed_at,
        confidence=confidence,
        uncertainty="fixture evidence",
        lifecycle=lifecycle,
        evidence=(
            ValidatedEvidence(
                ref=f"ku-{assertion_id}",
                artifact_type="knowledge_unit",
                serving_role="canonical_knowledge",
                artifact_version_id=artifact_version_id,
                evidence_checksum=checksum(assertion_id),
                privacy_class="R4",
            ),
        ),
        payload_checksum=checksum([assertion_id, value]),
    )


def _run(run_id: str, *assertions: ValidatedAssertion) -> PersonalStateRun:
    input_manifest = {"run": run_id}
    output_manifest = {"assertions": [item.assertion_id for item in assertions]}
    return PersonalStateRun(
        run_id=run_id,
        registry_id="a.personal_change",
        snapshot=_snapshot(),
        producer_version="projection-test",
        input_manifest=input_manifest,
        input_manifest_checksum=checksum(input_manifest),
        output_manifest=output_manifest,
        output_manifest_checksum=checksum(output_manifest),
        assertions=tuple(assertions),
    )


def test_current_state_and_formation_path_replay_deterministically() -> None:
    old = _validated(
        "a-old",
        value="A",
        valid_from="2026-01-01T00:00:00Z",
        observed_at="2026-01-01T01:00:00Z",
    )
    current = _validated(
        "a-current",
        value="D",
        valid_from="2026-06-01T00:00:00Z",
        observed_at="2026-06-01T01:00:00Z",
    )
    left = project_current_state(
        [_run("run-new", current), _run("run-old", old)],
        as_of="2026-07-18T00:00:00Z",
    )
    right = project_current_state(
        [_run("run-old", old), _run("run-new", current)],
        as_of="2026-07-18T00:00:00Z",
    )
    assert canonical_json(left) == canonical_json(right)
    state = left.current_goals[0]
    assert state.current_value == "D"
    assert [step.assertion_id for step in state.formation_path] == ["a-old", "a-current"]
    assert state.evidence[0].ref == "ku-a-current"


def test_unknown_expired_and_low_confidence_remain_explicit() -> None:
    expired = _validated(
        "expired",
        value="old",
        valid_from="2026-01-01T00:00:00Z",
        observed_at="2026-01-01T01:00:00Z",
        lifecycle="expired",
    )
    low = _validated(
        "low",
        value="tentative",
        valid_from="2026-06-01T00:00:00Z",
        observed_at="2026-06-01T01:00:00Z",
        predicate="tentative_goal",
        confidence=0.4,
    )
    missing = StateKey("constraint", "user", "work", "personal", "budget")
    projection = project_current_state(
        [_run("run", expired, low)],
        as_of="2026-07-18T00:00:00Z",
        expected_keys=[missing],
    )
    by_predicate = {row.key.predicate: row for row in projection.states}
    assert by_predicate["complete_target"].status == "expired"
    assert "no_current_evidence" in by_predicate["complete_target"].uncertainty
    assert by_predicate["tentative_goal"].status == "uncertain"
    assert "low_confidence" in by_predicate["tentative_goal"].uncertainty
    assert by_predicate["budget"].status == "unknown"
    assert by_predicate["budget"].uncertainty == ("unknown_no_evidence",)


def test_simultaneous_contradiction_is_not_silently_selected() -> None:
    first = _validated(
        "a1",
        value="A",
        valid_from="2026-06-01T00:00:00Z",
        observed_at="2026-06-01T01:00:00Z",
    )
    second = _validated(
        "a2",
        value="B",
        valid_from="2026-06-01T00:00:00Z",
        observed_at="2026-06-01T01:00:00Z",
    )
    state = project_current_state(
        [_run("run", first, second)], as_of="2026-07-18T00:00:00Z"
    ).states[0]
    assert state.status == "conflict"
    assert state.current_assertion_id is None
    assert state.current_value is None
    assert "unresolved_conflict" in state.uncertainty


def test_historical_conflict_does_not_override_newer_current_evidence() -> None:
    old_conflict = _validated(
        "old-conflict",
        value="A",
        valid_from="2026-01-01T00:00:00Z",
        observed_at="2026-01-01T01:00:00Z",
        lifecycle="conflict",
    )
    current = _validated(
        "current",
        value="D",
        valid_from="2026-06-01T00:00:00Z",
        observed_at="2026-06-01T01:00:00Z",
    )
    state = project_current_state(
        [_run("run", old_conflict, current)], as_of="2026-07-18T00:00:00Z"
    ).states[0]
    assert state.status == "current"
    assert state.current_assertion_id == "current"


# ---------------------------------------------------------------------------
# Plan 61-09 Task 1 RED contract: the normalization/validation path behind the
# versioned personal-model projection (HARNESS-07).
#
# state_projection is the ONLY normalization/validation path through which
# confirmed accepted review material may become a projection (D-20/D-21/D-22).
# These unit tests pin the invariants Plan 61-09 Task 2 must preserve and
# extend:
#   - inference content derives only from synthesis and is never a fact;
#   - draft/ignored/pending lifecycle states are invalid projection input;
#   - mixed-snapshot candidates/evidence and private/secret payloads reject;
#   - a derived projection stays evidence-bound, deterministic and reproducible.
# The RED surface test additionally requires the derived projection result to be
# versioned and time-aware with supersession, freshness and limitations.
# ---------------------------------------------------------------------------


def test_inference_derivation_requires_synthesis_and_never_fact() -> None:
    """An inference projection input comes only from synthesis, never fact."""
    accepted = normalize_candidates(
        [_candidate(provenance_class="inference", derivation="synthesis")],
        snapshot=_snapshot(),
    )
    assert accepted[0].provenance_class == "inference"
    assert accepted[0].assertion_kind == "goal"

    with pytest.raises(ProjectionError) as fact:
        normalize_candidates(
            [_candidate(provenance_class="fact", derivation="synthesis")],
            snapshot=_snapshot(),
        )
    assert fact.value.code == "provenance_rule_violation"

    with pytest.raises(ProjectionError) as wrong_derivation:
        normalize_candidates(
            [_candidate(provenance_class="inference", derivation="occurrence")],
            snapshot=_snapshot(),
        )
    assert wrong_derivation.value.code == "provenance_rule_violation"


def test_draft_ignored_and_pending_lifecycles_are_invalid_projection_input() -> None:
    """Draft/ignored/pending review states can never enter a projection."""
    for lifecycle in ("draft", "ignored", "pending", "proposed"):
        with pytest.raises(ProjectionError) as invalid:
            normalize_candidates(
                [_candidate(provenance_class="inference", derivation="synthesis",
                            lifecycle=lifecycle)],
                snapshot=_snapshot(),
            )
        assert invalid.value.code == "invalid_assertion_lifecycle", lifecycle


def test_mixed_snapshot_candidate_and_evidence_are_rejected() -> None:
    """Every candidate and evidence row must bind the same snapshot."""
    with pytest.raises(ProjectionError) as foreign_candidate:
        normalize_candidates(
            [_candidate(provenance_class="inference", derivation="synthesis",
                        snapshot_hash="hash-foreign")],
            snapshot=_snapshot(),
        )
    assert foreign_candidate.value.code == "mixed_snapshot"

    mixed_evidence = _candidate(provenance_class="inference", derivation="synthesis")
    mixed_evidence["evidence"] = [{**mixed_evidence["evidence"][0], "snapshot_hash": "hash-foreign"}]
    with pytest.raises(ProjectionError) as foreign_evidence:
        normalize_candidates([mixed_evidence], snapshot=_snapshot())
    assert foreign_evidence.value.code == "mixed_snapshot"


def _inference_assertion(assertion_id: str, *, value: object, confidence: float = 0.6) -> ValidatedAssertion:
    """A confirmed-accepted review-derived inference assertion (61-09 shape)."""
    return ValidatedAssertion(
        assertion_id=assertion_id,
        assertion_kind="goal",
        provenance_class="inference",
        subject="user",
        domain="work",
        scope="personal",
        predicate="complete_target",
        value=value,
        valid_from="2026-06-01T00:00:00Z",
        valid_to=None,
        observed_at="2026-06-01T01:00:00Z",
        confidence=confidence,
        uncertainty="accepted review synthesis",
        lifecycle="current",
        evidence=(
            ValidatedEvidence(
                ref=f"ku-{assertion_id}",
                artifact_type="knowledge_unit",
                serving_role="canonical_knowledge",
                artifact_version_id="av1",
                evidence_checksum=checksum(assertion_id),
                privacy_class="R4",
            ),
        ),
        payload_checksum=checksum([assertion_id, value]),
    )


def test_inference_projection_preserves_provenance_confidence_and_evidence_refs() -> None:
    """A derived inference projection stays evidence-bound and reproducible."""
    inference = _inference_assertion("inf-proj-1", value="D")
    state = project_current_state(
        [_run("run-inf", inference)], as_of="2026-07-18T00:00:00Z"
    ).current_goals[0]
    assert state.status == "current"
    assert state.provenance_class == "inference", "a projection is an inference, never a fact"
    assert state.confidence == 0.6
    assert state.evidence[0].ref == "ku-inf-proj-1"
    assert state.formation_path[0].uncertainty == ("source:accepted review synthesis",)
    assert [step.assertion_id for step in state.formation_path] == ["inf-proj-1"]

    again = project_current_state(
        [_run("run-inf", inference)], as_of="2026-07-18T00:00:00Z"
    ).current_goals[0]
    assert canonical_json(state) == canonical_json(again), "the projection replays deterministically"


try:
    _probe = project_current_state(
        [_run("run-red", _validated("red-a", value="D",
                                    valid_from="2026-06-01T00:00:00Z",
                                    observed_at="2026-06-01T01:00:00Z"))],
        as_of="2026-07-18T00:00:00Z",
    )
    _VERSIONED_PROJECTION_SURFACE = all(
        hasattr(_probe, name) for name in ("version", "supersession", "freshness", "limitations")
    )
except Exception:  # noqa: BLE001 - a broken surface is RED evidence, not a syntax error
    _VERSIONED_PROJECTION_SURFACE = False


def test_derived_projection_carries_version_supersession_freshness_and_limitations() -> None:
    """The derived projection is versioned and time-aware with supersession.

    Plan 61-09 (D-21): personal understanding is a time-aware projection, not a
    fixed profile document. Each derived projection must carry a version, its
    supersession record, source/snapshot/freshness binding and limitations.
    """
    if not _VERSIONED_PROJECTION_SURFACE:
        pytest.fail(
            "RED: state_projection must expose a versioned projection surface "
            "(version/supersession/freshness/limitations on the projection "
            "result) before a derived projection can be served by "
            "personal.model_projection.get (expected for 61-09 Task 1 RED)",
            pytrace=False,
        )
