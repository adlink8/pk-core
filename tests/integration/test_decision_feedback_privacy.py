from __future__ import annotations

import pytest

from personal_knowledge.intelligence.decision.effectiveness import OutcomeObservation
from personal_knowledge.intelligence.decision.state_machine import DecisionStateError, validate_outcome_metadata
from personal_knowledge.intelligence.decision.service import DecisionFeedbackService
from tests.integration.test_decision_feedback_concurrency import _published


def test_outcome_vetoes_private_bodies_secrets_and_untyped_evidence() -> None:
    for metadata, code in (
        ({"raw_body": "private text"}, "forbidden_outcome_field"),
        ({"credentials": {"token": "secret"}}, "forbidden_outcome_field"),
        ({"note": "unrestricted prose"}, "forbidden_outcome_field"),
    ):
        with pytest.raises(DecisionStateError, match=code):
            validate_outcome_metadata(metadata, ())
    with pytest.raises(DecisionStateError, match="typed_evidence_ref_required"):
        validate_outcome_metadata({}, ("plain-id",))


def test_outcome_record_is_metadata_only_and_cannot_claim_fact() -> None:
    fields = set(OutcomeObservation.__dataclass_fields__)
    assert not ({"body", "content", "note", "secret", "fact", "causal_claim"} & fields)


def test_shared_reads_never_expose_recommendation_target_or_private_body(tmp_path) -> None:
    db, rec = _published(tmp_path)
    service = DecisionFeedbackService(db)
    for operation, params in (
        ("recommendations.list", {}),
        ("recommendations.get", {"recommendation_id": rec.recommendation_id}),
        ("recommendations.history", {"recommendation_id": rec.recommendation_id}),
        ("recommendations.outcomes", {"recommendation_id": rec.recommendation_id}),
        ("recommendations.effectiveness", {"recommendation_id": rec.recommendation_id}),
    ):
        serialized = str(service.invoke(operation, **params)).lower()
        assert "close_target_d" not in serialized
        assert not any(token in serialized for token in ("raw_body", "credentials", "private text"))
