from __future__ import annotations

from pathlib import Path

import pytest

from personal_knowledge.intelligence.pilot.cases import admit_project_case
from personal_knowledge.intelligence.pilot.workflow import (
    PilotWorkflowError, preregister_outcome, read_event_stream,
    record_manual_action, record_user_decision,
)

from tests.integration.test_project_pilot_authority import setup_authorities


ACTOR = "a" * 64


def _case(tmp_path: Path, *, confirmation: str = "case-confirm-workflow"):
    env = setup_authorities(tmp_path)
    admitted = admit_project_case(
        pilot_db_path=env["pilot"], analysis_db_path=env["analysis"],
        personal_db_path=env["personal"], external_db_path=env["external"],
        run_id=env["run_id"], candidate_id=env["candidate_id"],
        selected_option_id="validate-first", case_confirmation_event_id=confirmation,
        write=True, now="2026-07-18T09:10:00Z",
    )
    assert admitted.case is not None
    return env, admitted.case


def test_accept_and_manual_action_are_distinct_idempotent_user_owned_events(tmp_path: Path) -> None:
    env, case = _case(tmp_path)
    preregister_outcome(
        env["pilot"], case_id=case.case_id, metric="focused_test_pass_rate", unit="fraction",
        baseline=0, target=1, direction="higher", window_start="2026-07-18T09:20:00Z",
        window_end="2026-07-18T09:40:00Z", collection_source="local pytest output",
        estimated_time_minutes=20, estimated_cost=0, expected_sequence=1,
        idempotency_key="protocol-1", actor_identity_hash=ACTOR,
    )
    decision = record_user_decision(
        env["pilot"], case_id=case.case_id, decision="accept",
        confirmed_case_checksum=case.payload_checksum, reason_code="user_authorized_validation",
        expected_sequence=2, idempotency_key="decision-1", actor_identity_hash=ACTOR,
    )
    assert record_user_decision(
        env["pilot"], case_id=case.case_id, decision="accept",
        confirmed_case_checksum=case.payload_checksum, reason_code="user_authorized_validation",
        expected_sequence=2, idempotency_key="decision-1", actor_identity_hash=ACTOR,
    ).existing
    started = record_manual_action(
        env["pilot"], case_id=case.case_id, action_state="started",
        description="Run local runtime and focused compatibility checks", operator="codex_operator",
        expected_sequence=3, idempotency_key="action-start", actor_identity_hash=ACTOR,
    )
    completed = record_manual_action(
        env["pilot"], case_id=case.case_id, action_state="completed",
        description="Local compatibility checks completed", operator="codex_operator",
        expected_sequence=4, idempotency_key="action-complete", actor_identity_hash=ACTOR,
    )
    assert (decision.sequence, started.sequence, completed.sequence) == (3, 4, 5)
    stream = read_event_stream(env["pilot"], case.case_id)
    assert [item["event_type"] for item in stream] == [
        "case_frozen", "outcome_preregistered", "user_decision", "manual_action", "manual_action",
    ]
    assert all(item["payload"]["system_external_actions"] == 0 for item in stream[1:])


def test_defer_control_path_is_reconstructable_and_external_actions_are_forbidden(tmp_path: Path) -> None:
    env, case = _case(tmp_path, confirmation="case-confirm-control")
    receipt = record_user_decision(
        env["pilot"], case_id=case.case_id, decision="defer",
        confirmed_case_checksum=case.payload_checksum, reason_code="control_insufficient_window",
        expected_sequence=1, idempotency_key="control-defer", actor_identity_hash=ACTOR,
    )
    assert receipt.sequence == 2
    with pytest.raises(PilotWorkflowError, match="accepted_decision_required"):
        record_manual_action(
            env["pilot"], case_id=case.case_id, action_state="started",
            description="Run local tests", operator="codex_operator", expected_sequence=2,
            idempotency_key="forbidden-after-defer", actor_identity_hash=ACTOR,
        )
    assert read_event_stream(env["pilot"], case.case_id)[-1]["payload"]["body"]["decision"] == "defer"


def test_case_checksum_and_command_like_action_fail_closed(tmp_path: Path) -> None:
    env, case = _case(tmp_path)
    with pytest.raises(PilotWorkflowError, match="case_confirmation_checksum_mismatch"):
        record_user_decision(
            env["pilot"], case_id=case.case_id, decision="accept",
            confirmed_case_checksum="0" * 64, reason_code="bad", expected_sequence=1,
            idempotency_key="bad-confirm", actor_identity_hash=ACTOR,
        )
    record_user_decision(
        env["pilot"], case_id=case.case_id, decision="accept",
        confirmed_case_checksum=case.payload_checksum, reason_code="ok", expected_sequence=1,
        idempotency_key="ok-confirm", actor_identity_hash=ACTOR,
    )
    with pytest.raises(PilotWorkflowError, match="external_action_forbidden"):
        record_manual_action(
            env["pilot"], case_id=case.case_id, action_state="started",
            description="deploy to https://example.invalid", operator="codex_operator",
            expected_sequence=2, idempotency_key="external", actor_identity_hash=ACTOR,
        )


def test_second_protocol_and_invalid_window_timestamp_fail_closed(tmp_path: Path) -> None:
    env, case = _case(tmp_path)
    preregister_outcome(
        env["pilot"], case_id=case.case_id, metric="pass_rate", unit="fraction",
        baseline=0, target=1, direction="higher", window_start="2026-07-18T09:20:00Z",
        window_end="2026-07-18T09:40:00Z", collection_source="pytest",
        estimated_time_minutes=20, estimated_cost=0, expected_sequence=1,
        idempotency_key="first", actor_identity_hash=ACTOR,
    )
    with pytest.raises(PilotWorkflowError, match="outcome_already_preregistered"):
        preregister_outcome(
            env["pilot"], case_id=case.case_id, metric="other", unit="count",
            baseline=0, target=1, direction="higher", window_start="2026-07-18T09:20:00Z",
            window_end="2026-07-18T09:40:00Z", collection_source="other",
            estimated_time_minutes=1, estimated_cost=0, expected_sequence=2,
            idempotency_key="second", actor_identity_hash=ACTOR,
        )
    with pytest.raises(PilotWorkflowError, match="timestamp_invalid"):
        preregister_outcome(
            env["pilot"], case_id=case.case_id, metric="x", unit="x",
            baseline=0, target=1, direction="higher", window_start="not-a-time",
            window_end="2026-07-18T09:40:00Z", collection_source="x",
            estimated_time_minutes=1, estimated_cost=0, expected_sequence=2,
            idempotency_key="bad-time", actor_identity_hash=ACTOR,
        )
