from __future__ import annotations

from datetime import datetime, timezone

from personal_knowledge.intelligence.decision.recommendations import (
    RecommendationInput,
    RecommendationRule,
    evaluate_rule,
)
from personal_knowledge.intelligence.decision.schema import CognitionReference


def _ref(*, cognitive_type: str = "fact", uncertainty: str = "") -> CognitionReference:
    return CognitionReference(
        cognitive_type=cognitive_type,
        authority_id="a.personal_change",
        record_id=f"psa_{cognitive_type}",
        source_run_id="psr_fixture",
        source_run_checksum="a" * 64,
        source_publication_sequence=1,
        snapshot_id="ss_fixture",
        snapshot_hash="snapshot-hash",
        provenance_class=cognitive_type,
        evidence_status="eligible",
        uncertainty=uncertainty,
        record_checksum="b" * 64,
    )


def _rule(*, version: str = "v1") -> RecommendationRule:
    return RecommendationRule(
        rule_id="bounded-next-step",
        version=version,
        eligible_cognition_types=frozenset({"fact", "observation"}),
        minimum_evidence=1,
        max_evidence_age_seconds=86_400,
        domain="work",
        recommendation_kind="next_step",
        uncertainty_behavior="abstain",
        contraindications=frozenset({"human_gate_unresolved"}),
        expiry_seconds=3_600,
    )


def _input(**changes: object) -> RecommendationInput:
    values = dict(
        subject="user",
        scope="personal",
        target="review_target_d_gap",
        horizon="next_session",
        expected_benefit="reduce unresolved scope",
        rationale_codes=("goal_gap",),
        costs_constraints=("human review remains required",),
        assumptions=("phase25 input remains published",),
        support=(_ref(),),
        observed_at="2026-07-18T00:00:00Z",
        uncertainty="",
        conflicting=False,
        contraindications=(),
    )
    values.update(changes)
    return RecommendationInput(**values)


def test_rule_replay_is_deterministic_and_version_bound() -> None:
    now = datetime(2026, 7, 18, 0, 30, tzinfo=timezone.utc)
    first = evaluate_rule(_rule(), _input(), now=now)
    replay = evaluate_rule(_rule(), _input(), now=now)
    changed = evaluate_rule(_rule(version="v2"), _input(), now=now)
    assert first == replay
    assert first.reason_code == "eligible" and first.draft is not None
    assert first.policy_version == "v1"
    assert changed.policy_version == "v2"
    assert changed != first


def test_rule_abstains_on_insufficient_stale_conflicting_uncertain_or_contraindicated_input() -> None:
    now = datetime(2026, 7, 20, tzinfo=timezone.utc)
    cases = (
        (_input(support=()), "insufficient_evidence"),
        (_input(), "stale_evidence"),
        (_input(observed_at="2026-07-20T00:00:00Z", conflicting=True), "conflicting_evidence"),
        (_input(observed_at="2026-07-20T00:00:00Z", uncertainty="material"), "uncertain_evidence"),
        (_input(observed_at="2026-07-20T00:00:00Z", contraindications=("human_gate_unresolved",)), "contraindicated"),
        (_input(observed_at="2026-07-20T00:00:00Z", support=(_ref(cognitive_type="inference"),)), "ineligible_cognition_type"),
    )
    for input_value, reason in cases:
        result = evaluate_rule(_rule(), input_value, now=now)
        assert result.draft is None
        assert result.reason_code == reason

