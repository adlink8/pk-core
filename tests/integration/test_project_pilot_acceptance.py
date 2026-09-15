from __future__ import annotations

from pathlib import Path

import pytest

from personal_knowledge.intelligence.pilot.cli import main
from personal_knowledge.intelligence.pilot.controls import (
    record_correction, record_restore, record_revoke, record_snapshot_transition,
)
from personal_knowledge.intelligence.pilot.outcomes import record_outcome_observation
from personal_knowledge.intelligence.pilot.service import acceptance_report, controls, explain, get_case
from personal_knowledge.intelligence.pilot.workflow import PilotWorkflowError, read_event_stream

from tests.integration.test_project_pilot_outcomes import ACTOR, _completed
from tests.integration.test_project_pilot_authority import _external


def _controlled(tmp_path: Path):
    env, case = _completed(tmp_path)
    record_outcome_observation(
        env["pilot"], case_id=case.case_id, observed_value=1,
        actual_time_minutes=18, actual_cost=0, completion="completed", quality=1,
        satisfaction=.9, side_effects=(), regret=0, confounders=(), source="pytest receipt",
        observed_at="2026-07-18T09:31:00Z", expected_sequence=5,
        idempotency_key="observation", actor_identity_hash=ACTOR,
    )
    stream = read_event_stream(env["pilot"], case.case_id)
    decision_checksum = stream[2]["payload_checksum"]
    observation_checksum = stream[5]["payload_checksum"]
    correction = record_correction(
        env["pilot"], case_id=case.case_id, target_checksum=observation_checksum,
        corrected_fields={"actual_time_minutes": 17.5}, reason_code="receipt_precision",
        expected_sequence=6, idempotency_key="correction", actor_identity_hash=ACTOR,
    )
    revoke = record_revoke(
        env["pilot"], case_id=case.case_id, target_checksum=decision_checksum,
        reason_code="recovery_drill", expected_sequence=7, idempotency_key="revoke",
        actor_identity_hash=ACTOR,
    )
    restore = record_restore(
        env["pilot"], case_id=case.case_id, revoke_checksum=revoke.payload_checksum,
        reason_code="recovery_complete", expected_sequence=8, idempotency_key="restore",
        actor_identity_hash=ACTOR,
    )
    rollback = record_snapshot_transition(
        env["pilot"], case_id=case.case_id, action="rollback",
        personal_db_path=env["personal"], external_db_path=env["external"],
        expected_sequence=9, idempotency_key="snapshot-rollback",
        actor_identity_hash=ACTOR, now="2026-07-18T09:35:00Z",
    )
    forward = record_snapshot_transition(
        env["pilot"], case_id=case.case_id, action="forward_restore",
        personal_db_path=env["personal"], external_db_path=env["external"],
        expected_sequence=10, idempotency_key="snapshot-forward",
        actor_identity_hash=ACTOR, now="2026-07-18T09:35:00Z",
    )
    return env, case, {"correction": correction, "revoke": revoke, "restore": restore,
                       "rollback": rollback, "forward": forward}


def test_compensating_controls_and_snapshot_recovery_preserve_history(tmp_path: Path) -> None:
    env, case, receipts = _controlled(tmp_path)
    stream = read_event_stream(env["pilot"], case.case_id)
    assert len(stream) == 11
    assert [item["event_type"] for item in stream[-5:]] == [
        "correction", "revoke", "restore", "snapshot_rollback", "snapshot_forward_restore",
    ]
    view = controls(env["pilot"], case.case_id)
    assert view["snapshot_state"] == "BOUND"
    assert view["revoked_target_checksums"] == []
    assert view["restored_target_checksums"]
    assert view["corrections"][0]["corrected_fields"]["actual_time_minutes"] == 17.5
    assert get_case(env["pilot"], case.case_id)["case"]["payload_checksum"] == case.payload_checksum
    with pytest.raises(PilotWorkflowError, match="control_target_missing"):
        record_correction(
            env["pilot"], case_id=case.case_id, target_checksum="0" * 64,
            corrected_fields={"x": 1}, reason_code="bad", expected_sequence=11,
            idempotency_key="bad", actor_identity_hash=ACTOR,
        )


def test_bounded_reads_and_metadata_only_acceptance_are_zero_side_effect(tmp_path: Path, capsys) -> None:
    env, case, _ = _controlled(tmp_path)
    report = acceptance_report(
        pilot_db_path=env["pilot"], knowledge_authority_path=env["personal"],
        personal_db_path=env["personal"], external_db_path=env["external"],
        analysis_db_path=env["analysis"], as_of="2026-07-18T09:40:00Z",
    )
    assert report["ok"] and report["unchanged"]
    assert report["provider_calls"] == report["network_calls"] == 0
    assert report["system_external_actions"] == report["unauthorized_knowledge_writes"] == 0
    view = explain(env["pilot"], case.case_id, as_of="2026-07-18T09:40:00Z")
    assert view["outcome"]["status"] == "pass" and not view["authoritative_decision"]
    assert main([
        "--db", str(env["pilot"]), "acceptance", "--knowledge-authority", str(env["personal"]),
        "--personal-db", str(env["personal"]), "--external-db", str(env["external"]),
        "--analysis-db", str(env["analysis"]), "--as-of", "2026-07-18T09:40:00Z",
        "--metadata-only",
    ]) == 0
    assert '"ok":true' in capsys.readouterr().out


def test_acceptance_fails_when_active_external_pointer_drifts(tmp_path: Path) -> None:
    env, _, _ = _controlled(tmp_path)
    _external(env["external"], version="3.14.3")
    with pytest.raises(Exception, match="external_authority_drift"):
        acceptance_report(
            pilot_db_path=env["pilot"], knowledge_authority_path=env["personal"],
            personal_db_path=env["personal"], external_db_path=env["external"],
            analysis_db_path=env["analysis"], as_of="2026-07-18T09:40:00Z",
        )
