from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from personal_knowledge.intelligence.decision.schema import (
    COGNITIVE_TYPES,
    CognitionReference,
    DecisionSchemaError,
    RecommendationDraft,
    canonical_json,
    checksum,
)


def _reference(cognitive_type: str = "fact") -> CognitionReference:
    return CognitionReference(
        cognitive_type=cognitive_type,
        authority_id="a.personal_change",
        record_id="psa_fixture",
        source_run_id="psr_fixture",
        source_run_checksum="a" * 64,
        source_publication_sequence=1,
        snapshot_id="ss_fixture",
        snapshot_hash="snapshot-hash",
        provenance_class=cognitive_type,
        evidence_status="eligible",
        uncertainty="",
        record_checksum="b" * 64,
    )


def test_cognitive_discriminator_is_exact_required_and_frozen() -> None:
    assert COGNITIVE_TYPES == frozenset(
        {"fact", "observation", "inference", "recommendation", "user_confirmation"}
    )
    for value in ("fact", "observation", "inference"):
        assert _reference(value).cognitive_type == value
    with pytest.raises(DecisionSchemaError, match="cognitive_type_required"):
        _reference("")
    with pytest.raises(DecisionSchemaError, match="invalid_cognitive_reference"):
        _reference("recommendation")
    with pytest.raises(FrozenInstanceError):
        _reference().record_id = "changed"  # type: ignore[misc]


def test_recommendation_is_canonical_and_forbids_truth_or_execution_authority() -> None:
    draft = RecommendationDraft(
        subject="user",
        domain="work",
        scope="personal",
        recommendation_kind="next_step",
        target="review_target_d_gap",
        horizon="next_session",
        rationale_codes=("goal_gap",),
        expected_benefit="reduce unresolved scope",
        costs_constraints=("human review remains required",),
        assumptions=("phase25 input remains published",),
        contraindications=(),
        confidence=0.8,
        uncertainty="release remains blocked",
        expires_at="2026-08-01T00:00:00Z",
        support=(_reference(),),
    )
    assert checksum(draft) == checksum(draft)
    assert canonical_json(draft) == canonical_json(draft)
    for forbidden in ("fact", "knowledge_unit", "approved", "executed"):
        assert forbidden not in draft.__dataclass_fields__
