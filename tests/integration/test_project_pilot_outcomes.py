from __future__ import annotations

from pathlib import Path

from personal_knowledge.intelligence.pilot.cases import admit_project_case
from personal_knowledge.intelligence.pilot.outcomes import assess_outcome, record_outcome_observation
from personal_knowledge.intelligence.pilot.workflow import (
    preregister_outcome, record_manual_action, record_user_decision,
)

from tests.integration.test_project_pilot_authority import setup_authorities


ACTOR = "b" * 64


def _completed(tmp_path: Path):
    env = setup_authorities(tmp_path)
    result = admit_project_case(
        pilot_db_path=env["pilot"], analysis_db_path=env["analysis"],
        personal_db_path=env["personal"], external_db_path=env["external"],
        run_id=env["run_id"], candidate_id=env["candidate_id"],
        selected_option_id="validate-first", case_confirmation_event_id="outcome-case",
        write=True, now="2026-07-18T09:05:00Z",
    )
    case = result.case
    assert case is not None
    preregister_outcome(
        env["pilot"], case_id=case.case_id, metric="focused_test_pass_rate", unit="fraction",
        baseline=0, target=1, direction="higher", window_start="2026-07-18T09:10:00Z",
        window_end="2026-07-18T09:30:00Z", collection_source="local pytest output",
        estimated_time_minutes=20, estimated_cost=0, expected_sequence=1,
        idempotency_key="protocol", actor_identity_hash=ACTOR,
    )
    record_user_decision(
        env["pilot"], case_id=case.case_id, decision="accept",
        confirmed_case_checksum=case.payload_checksum, reason_code="authorized",
        expected_sequence=2, idempotency_key="decision", actor_identity_hash=ACTOR,
    )
    record_manual_action(
        env["pilot"], case_id=case.case_id, action_state="started",
        description="Run local compatibility checks", operator="codex_operator",
        expected_sequence=3, idempotency_key="started", actor_identity_hash=ACTOR,
    )
    record_manual_action(
        env["pilot"], case_id=case.case_id, action_state="completed",
        description="Local compatibility checks completed", operator="codex_operator",
        expected_sequence=4, idempotency_key="completed", actor_identity_hash=ACTOR,
    )
    return env, case


def test_preregistered_complete_window_produces_non_causal_pass(tmp_path: Path) -> None:
    env, case = _completed(tmp_path)
    record_outcome_observation(
        env["pilot"], case_id=case.case_id, observed_value=1,
        actual_time_minutes=18, actual_cost=0, completion="completed", quality=1,
        satisfaction=.9, side_effects=(), regret=0, confounders=(),
        source="pytest receipt", observed_at="2026-07-18T09:31:00Z",
        expected_sequence=5, idempotency_key="observation", actor_identity_hash=ACTOR,
    )
    assessment = assess_outcome(env["pilot"], case.case_id, as_of="2026-07-18T09:31:00Z")
    assert assessment.status == "pass" and assessment.observed_value == 1
    assert assessment.window_complete and not assessment.confounded and not assessment.causal_claim


def test_missing_incomplete_or_confounded_evidence_stays_inconclusive(tmp_path: Path) -> None:
    env, case = _completed(tmp_path / "missing")
    missing = assess_outcome(env["pilot"], case.case_id, as_of="2026-07-18T09:20:00Z")
    assert missing.status == "inconclusive" and missing.reason_codes == ("observation_missing",)

    env, case = _completed(tmp_path / "confounded")
    record_outcome_observation(
        env["pilot"], case_id=case.case_id, observed_value=1,
        actual_time_minutes=20, actual_cost=0, completion="completed", quality=.8,
        satisfaction=.8, side_effects=("cache warmed",), regret=.1,
        confounders=("unrelated dependency cache change",), source="pytest receipt",
        observed_at="2026-07-18T09:20:00Z", expected_sequence=5,
        idempotency_key="observation", actor_identity_hash=ACTOR,
    )
    assessment = assess_outcome(env["pilot"], case.case_id, as_of="2026-07-18T09:20:00Z")
    assert assessment.status == "inconclusive"
    assert assessment.reason_codes == ("window_incomplete", "confounded")
    assert not assessment.causal_claim
