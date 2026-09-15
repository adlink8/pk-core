from __future__ import annotations

from dataclasses import replace

import pytest

from personal_knowledge.intelligence.proactive.ranking import (
    DEFAULT_NOISE_POLICY,
    DEFAULT_RANKING_POLICY,
    EvaluationContext,
    SurfaceRecord,
    evaluate_candidates,
    build_digest,
    rank_candidates,
)
from personal_knowledge.intelligence.proactive.schema import CandidateDraft, SupportReference, checksum


def _ref(record_id: str = "risk-1") -> SupportReference:
    return SupportReference("a.personal_change", "risk", record_id, checksum({"id": record_id}),
                            "psr_1", "1" * 64, "ss_1", "2" * 64)


def _draft(candidate_class: str = "important_change", *, subject: str = "subject-a", score: float = 0.8) -> CandidateDraft:
    return CandidateDraft(
        candidate_class=candidate_class, presentation_kind="inbox_item", subject=subject,
        scope="personal", domains=("project",), target_group=("risk:risk-1",),
        valid_from="2026-07-18T00:00:00Z", expires_at="2026-07-20T00:00:00Z",
        support_refs=(_ref(),), severity=score, urgency=score, goal_impact=score,
        cross_domain_impact=score, evidence_strength=score, user_relevance=score,
        outcome_signal=0.0, uncertainty="fixture contract only", reason_codes=("fixture_only",),
    )


@pytest.mark.parametrize("candidate_class", [
    "important_change", "goal_conflict", "deadline_risk", "stalled_project",
    "cross_domain_opportunity", "outcome_followup", "trust_attention",
])
def test_all_candidate_classes_are_metadata_only_presentation_proposals(candidate_class: str) -> None:
    item = rank_candidates([_draft(candidate_class)], policy=DEFAULT_RANKING_POLICY)[0]
    assert item.candidate_class == candidate_class
    assert item.presentation_kind in {"inbox_item", "digest_item"}
    assert item.fixture_label == "contract_fixture_not_real_usefulness"
    assert item.importance.final_score > 0
    assert item.support_refs[0].record_checksum == checksum({"id": "risk-1"})


def test_privacy_evidence_and_trust_vetoes_precede_score() -> None:
    maximum = replace(_draft(score=1.0), sensitive=True)
    candidates = rank_candidates([maximum], policy=DEFAULT_RANKING_POLICY)
    result = evaluate_candidates(candidates, context=EvaluationContext.fixed(), policy=DEFAULT_NOISE_POLICY)[0]
    assert result.result == "abstained"
    assert result.reason_codes == ("privacy_veto",)
    for field, reason in (("evidence_eligible", "evidence_veto"), ("trust_eligible", "trust_veto")):
        candidate = rank_candidates([replace(_draft(score=1.0), **{field: False})], policy=DEFAULT_RANKING_POLICY)[0]
        evaluation = evaluate_candidates([candidate], context=EvaluationContext.fixed(), policy=DEFAULT_NOISE_POLICY)[0]
        assert evaluation.result == "abstained"
        assert evaluation.reason_codes == (reason,)


def test_threshold_ties_and_checksum_are_stable() -> None:
    drafts = [_draft(subject="b"), _draft(subject="a")]
    first = rank_candidates(drafts, policy=DEFAULT_RANKING_POLICY)
    second = rank_candidates(reversed(drafts), policy=DEFAULT_RANKING_POLICY)
    assert first == second
    assert [item.candidate_id for item in first] == sorted(item.candidate_id for item in first)
    assert all(len(item.payload_checksum) == 64 for item in first)
    low = rank_candidates([_draft(score=0.1)], policy=DEFAULT_RANKING_POLICY)[0]
    assert evaluate_candidates([low], context=EvaluationContext.fixed(), policy=DEFAULT_NOISE_POLICY)[0].reason_codes == ("below_threshold",)


def test_novelty_dedup_cooldown_quiet_and_budgets_are_reason_coded() -> None:
    base = rank_candidates([_draft()], policy=DEFAULT_RANKING_POLICY)[0]
    duplicate = rank_candidates([_draft()], policy=DEFAULT_RANKING_POLICY, prior_candidates=(base,))[0]
    assert duplicate.novelty == 0.0
    assert duplicate.candidate_id == base.candidate_id
    cooldown = EvaluationContext.fixed(surface_records=(SurfaceRecord(base.cooldown_key, "presented", "2026-07-18T08:00:00Z"),))
    assert evaluate_candidates([base], context=cooldown, policy=DEFAULT_NOISE_POLICY)[0].reason_codes == ("cooldown_active",)
    quiet = replace(EvaluationContext.fixed(), as_of="2026-07-18T23:00:00Z")
    deferred = evaluate_candidates([base], context=quiet, policy=DEFAULT_NOISE_POLICY)[0]
    assert deferred.result == "deferred" and deferred.deferred_until == "2026-07-19T07:00:00Z"
    many = rank_candidates([_draft(subject=f"s-{i}") for i in range(5)], policy=DEFAULT_RANKING_POLICY)
    results = evaluate_candidates(many, context=EvaluationContext.fixed(), policy=DEFAULT_NOISE_POLICY)
    assert sum(item.result == "eligible" for item in results) == DEFAULT_NOISE_POLICY.domain_budget
    assert sum("domain_budget_exhausted" in item.reason_codes for item in results) == 3


def test_invalid_timezone_fails_closed_to_inbox_only_and_expiry_is_explicit() -> None:
    candidate = rank_candidates([replace(_draft(), presentation_kind="digest_item")], policy=DEFAULT_RANKING_POLICY)[0]
    invalid = replace(EvaluationContext.fixed(), timezone="Mars/Olympus")
    result = evaluate_candidates([candidate], context=invalid, policy=DEFAULT_NOISE_POLICY)[0]
    assert result.result == "abstained" and result.reason_codes == ("invalid_timezone_inbox_only",)
    expired = replace(EvaluationContext.fixed(), as_of="2026-07-21T00:00:00Z",
                      window_start="2026-07-21T00:00:00Z", window_end="2026-07-22T00:00:00Z")
    assert evaluate_candidates([candidate], context=expired, policy=DEFAULT_NOISE_POLICY)[0].result == "expired"


def test_cooldown_expires_at_exact_boundary_and_dismissal_does_not_start_it() -> None:
    candidate = rank_candidates([_draft()], policy=DEFAULT_RANKING_POLICY)[0]
    boundary = EvaluationContext.fixed(surface_records=(
        SurfaceRecord(candidate.cooldown_key, "acknowledged", "2026-07-17T12:00:00Z"),
    ))
    assert evaluate_candidates([candidate], context=boundary, policy=DEFAULT_NOISE_POLICY)[0].result == "eligible"
    dismissed = EvaluationContext.fixed(surface_records=(
        SurfaceRecord(candidate.cooldown_key, "dismissed", "2026-07-18T11:59:00Z"),
    ))
    assert evaluate_candidates([candidate], context=dismissed, policy=DEFAULT_NOISE_POLICY)[0].result == "eligible"


def test_quiet_period_boundaries_are_fair_and_do_not_schedule_delivery() -> None:
    candidate = rank_candidates([_draft()], policy=DEFAULT_RANKING_POLICY)[0]
    at_start = replace(EvaluationContext.fixed(), as_of="2026-07-18T22:00:00Z")
    before_end = replace(EvaluationContext.fixed(), as_of="2026-07-18T06:59:59Z")
    at_end = replace(EvaluationContext.fixed(), as_of="2026-07-18T07:00:00Z")
    assert evaluate_candidates([candidate], context=at_start, policy=DEFAULT_NOISE_POLICY)[0].result == "deferred"
    assert evaluate_candidates([candidate], context=before_end, policy=DEFAULT_NOISE_POLICY)[0].result == "deferred"
    assert evaluate_candidates([candidate], context=at_end, policy=DEFAULT_NOISE_POLICY)[0].result == "eligible"
    assert "schedule" not in evaluate_candidates([candidate], context=at_start, policy=DEFAULT_NOISE_POLICY)[0].payload


def test_global_and_domain_budget_ties_are_stable_and_critical_never_bypasses_user_veto() -> None:
    drafts = [replace(_draft(subject=f"p-{i}"), domains=("project",)) for i in range(2)] + [
        replace(_draft(subject=f"l-{i}"), domains=("learning",)) for i in range(2)
    ]
    candidates = rank_candidates(reversed(drafts), policy=DEFAULT_RANKING_POLICY)
    results = evaluate_candidates(candidates, context=EvaluationContext.fixed(), policy=DEFAULT_NOISE_POLICY)
    assert sum(item.result == "eligible" for item in results) == DEFAULT_NOISE_POLICY.global_budget
    assert sum("global_budget_exhausted" in item.reason_codes for item in results) == 1
    winner_ids = [item.candidate_id for item in results if item.result == "eligible"]
    expected = [item.candidate_id for item in sorted(candidates, key=lambda item: (-item.importance.final_score, -item.importance.urgency, item.candidate_id))[:3]]
    assert winner_ids == sorted(expected)
    critical = rank_candidates([_draft(score=1.0)], policy=DEFAULT_RANKING_POLICY)[0]
    suppressed = replace(EvaluationContext.fixed(), explicit_suppressions=(critical.dedup_key,))
    result = evaluate_candidates([critical], context=suppressed, policy=replace(DEFAULT_NOISE_POLICY, global_budget=0, domain_budget=0))[0]
    assert result.result == "abstained" and result.reason_codes == ("trust_veto",)


def test_digest_keeps_each_support_manifest_and_never_merges_contradictions() -> None:
    candidates = rank_candidates([_draft(subject="a"), _draft(subject="b")], policy=DEFAULT_RANKING_POLICY)
    digest = build_digest(candidates)
    assert digest["presentation_kind"] == "digest_item"
    assert digest["contradictory_evidence_merged"] is False
    assert len(digest["support_manifests"]) == 2
