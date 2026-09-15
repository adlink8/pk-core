from __future__ import annotations

from dataclasses import replace

from personal_knowledge.intelligence.decision.effectiveness import (
    EffectivenessRule,
    OutcomeObservation,
    assess_outcome,
    summarize_assessments,
)


def _outcome(**changes):
    values = dict(
        outcome_id="doc_fixture",
        recommendation_id="rec_fixture",
        recommendation_checksum="a" * 64,
        action_id="dac_fixture",
        action_checksum="b" * 64,
        source_class="user_reported",
        measurement_definition="weekly completed focus blocks",
        metric="focus_blocks",
        baseline_value=2.0,
        target_value=4.0,
        observed_value=5.0,
        unit="count/week",
        direction="increase",
        window_start="2026-07-01T00:00:00Z",
        window_end="2026-07-08T00:00:00Z",
        adherence_status="adhered",
        evidence_refs=("observation:a.personal_change:obs1:" + "c" * 64,),
        confidence=0.8,
        uncertainty=(),
        confounders=(),
        concurrent_actions=(),
        payload_checksum="d" * 64,
    )
    values.update(changes)
    return OutcomeObservation(**values)


RULE = EffectivenessRule(
    rule_id="observed_goal_attainment",
    version="1",
    metric="focus_blocks",
    unit="count/week",
    direction="increase",
    minimum_window_seconds=86400,
)


def test_valid_observation_is_versioned_non_causal_inference() -> None:
    result = assess_outcome(_outcome(), RULE, action_state="completed")
    assert result.verdict == "effective"
    assert result.cognitive_type == "inference"
    assert result.causal_claim is False
    assert result.rule_version == "1"
    assert result.input_checksums == ("b" * 64, "d" * 64)


def test_missing_mismatched_nonadherent_and_confounded_data_abstain() -> None:
    fixtures = (
        (_outcome(observed_value=None), "missing_observed_value", "completed"),
        (_outcome(unit="minutes"), "unit_mismatch", "completed"),
        (_outcome(adherence_status="non_adherent"), "non_adherent", "completed"),
        (_outcome(confounders=("seasonality",)), "confounded", "completed"),
        (_outcome(concurrent_actions=("other_action",)), "concurrent_actions", "completed"),
        (_outcome(), "action_not_completed", "abandoned"),
    )
    for outcome, limitation, action_state in fixtures:
        result = assess_outcome(outcome, RULE, action_state=action_state)
        assert result.verdict == "inconclusive"
        assert limitation in result.limitations
        assert result.causal_claim is False


def test_direction_supports_ineffective_and_mixed_without_causal_language() -> None:
    ineffective = assess_outcome(_outcome(observed_value=1.0), RULE, action_state="completed")
    mixed = assess_outcome(_outcome(observed_value=3.0), RULE, action_state="completed")
    assert ineffective.verdict == "ineffective"
    assert mixed.verdict == "mixed"
    assert all(result.causal_claim is False for result in (ineffective, mixed))


def test_bounded_cohort_isolated_by_policy_domain_kind_and_minimum_sample() -> None:
    rows = tuple(assess_outcome(_outcome(outcome_id=f"doc_{i}"), RULE, action_state="completed") for i in range(3))
    insufficient = summarize_assessments(
        rows[:2], policy_version="v1", domain="work", recommendation_kind="next_step", minimum_sample=3
    )
    ready = summarize_assessments(
        rows, policy_version="v1", domain="work", recommendation_kind="next_step", minimum_sample=3
    )
    assert insufficient.status == "insufficient_sample" and insufficient.effectiveness_rate is None
    assert ready.status == "observational_summary" and ready.effectiveness_rate == 1.0
    assert ready.causal_claim is False
    assert ready.cohort_key == ("v1", "work", "next_step")


def test_rule_version_changes_identity_not_history() -> None:
    first = assess_outcome(_outcome(), RULE, action_state="completed")
    second = assess_outcome(_outcome(), replace(RULE, version="2"), action_state="completed")
    assert first.assessment_id != second.assessment_id
    assert first.rule_version == "1" and second.rule_version == "2"

