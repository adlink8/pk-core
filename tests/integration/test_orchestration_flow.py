from __future__ import annotations

from pathlib import Path

import pytest

from personal_knowledge.intelligence.orchestration import (
    OrchestrationError,
    OrchestrationService,
    apply_schema,
    execute_calibrate,
    execute_decide,
    execute_manual_action,
    execute_observe,
    execute_preregister,
    execute_publish,
)
from personal_knowledge.intelligence.pilot.workflow import read_event_stream
from tests.integration.test_project_pilot_authority import setup_authorities


ACTOR = "a" * 64
SECRET = b"phase-33-flow-confirmation-secret-32-bytes"
NOW = "2026-07-18T09:30:00Z"


def _service(tmp_path: Path):
    env = setup_authorities(tmp_path)
    db = tmp_path / "orchestration.sqlite"
    apply_schema(db)
    service = OrchestrationService(
        db_path=db,
        personal_db=env["personal"],
        external_db=env["external"],
        confirmation_secret=SECRET,
    )
    prepared = service.prepare(
        goal="Choose a compatible local runtime",
        constraints=("local validation only", "manual operation only"),
        weights={"safety": 0.7, "speed": 0.3},
        actor_identity_hash=ACTOR,
        max_external_age_seconds=7200,
        now="2026-07-18T09:10:00Z",
    )
    confirmed = service.confirm(
        prepared,
        confirmation_token=service.issue_confirmation(prepared),
        idempotency_key="confirm-flow",
        now="2026-07-18T09:10:00Z",
    )
    return env, service, confirmed.session_id


def _preview(service, session_id, operation, payload):
    sequence = service.get(session_id, now=NOW)["sequence"]
    preview = service.preview_transition(
        session_id,
        operation,
        payload,
        actor_identity_hash=ACTOR,
        expected_sequence=sequence,
        now=NOW,
    )
    return preview, service.issue_confirmation(preview)


def _reach_published(tmp_path: Path):
    env, service, session_id = _service(tmp_path)
    generated, token = _preview(service, session_id, "generate", {"request": "fixture"})
    service.commit_transition(
        generated,
        confirmation_token=token,
        idempotency_key="generate-flow",
        references={"run_id": env["run_id"], "candidate_id": env["candidate_id"]},
        now=NOW,
    )
    published, token = _preview(
        service,
        session_id,
        "publish",
        {
            "run_id": env["run_id"],
            "candidate_id": env["candidate_id"],
            "selected_option_id": "validate-first",
            "case_confirmation_event_id": "case-confirm-flow",
        },
    )
    result = execute_publish(
        service,
        published,
        confirmation_token=token,
        idempotency_key="publish-flow",
        pilot_db=env["pilot"],
        analysis_db=env["analysis"],
        now=NOW,
    )
    return env, service, session_id, result.references


def test_confirmed_flow_reaches_noncausal_calibrated_state(tmp_path: Path) -> None:
    env, service, session_id, published = _reach_published(tmp_path)
    case_id = published["case_id"]

    preview, token = _preview(service, session_id, "decide", {
        "case_id": case_id,
        "decision": "accept",
        "confirmed_case_checksum": published["case_checksum"],
        "reason_code": "bounded-local-pilot",
        "pilot_expected_sequence": 1,
    })
    execute_decide(service, preview, confirmation_token=token, idempotency_key="decide-flow", pilot_db=env["pilot"], now=NOW)

    preview, token = _preview(service, session_id, "preregister", {
        "case_id": case_id,
        "metric": "compatibility_pass_rate",
        "unit": "ratio",
        "baseline": 0.0,
        "target": 1.0,
        "direction": "higher",
        "window_start": NOW,
        "window_end": "2026-07-18T10:00:00Z",
        "collection_source": "local test output",
        "estimated_time_minutes": 20,
        "estimated_cost": 0,
        "pilot_expected_sequence": 2,
    })
    execute_preregister(service, preview, confirmation_token=token, idempotency_key="preregister-flow", pilot_db=env["pilot"], now=NOW)

    for operation, action_state, pilot_sequence in (
        ("action_start", "started", 3),
        ("action_complete", "completed", 4),
    ):
        preview, token = _preview(service, session_id, operation, {
            "case_id": case_id,
            "action_state": action_state,
            "description": "Run the bounded local compatibility test",
            "operator": "user",
            "pilot_expected_sequence": pilot_sequence,
        })
        execute_manual_action(service, preview, confirmation_token=token, idempotency_key=f"{operation}-flow", pilot_db=env["pilot"], now=NOW)

    preview, token = _preview(service, session_id, "observe", {
        "case_id": case_id,
        "observed_value": 1.0,
        "actual_time_minutes": 18,
        "actual_cost": 0,
        "completion": "completed",
        "quality": 0.9,
        "satisfaction": 0.9,
        "side_effects": [],
        "regret": 0.0,
        "confounders": [],
        "source": "local test output",
        "observed_at": NOW,
        "pilot_expected_sequence": 5,
    })
    observed = execute_observe(service, preview, confirmation_token=token, idempotency_key="observe-flow", pilot_db=env["pilot"], now=NOW)
    assert observed.references["causal_claim"] is False

    preview, token = _preview(service, session_id, "calibrate", {"protocol_id": "calp_fixture"})
    calibrated = execute_calibrate(
        service,
        preview,
        confirmation_token=token,
        idempotency_key="calibrate-flow",
        calibration_db=tmp_path / "unused.sqlite",
        now=NOW,
        calibration_runner=lambda _: {
            "protocol_id": "calp_fixture",
            "causal_claim": False,
            "promotion_available": False,
        },
    )

    assert calibrated.state == "calibrated"
    assert calibrated.references == {
        "protocol_id": "calp_fixture",
        "causal_claim": False,
        "promotion_available": False,
    }
    stream = read_event_stream(env["pilot"], case_id)
    assert [event["event_type"] for event in stream] == [
        "case_frozen", "user_decision", "outcome_preregistered",
        "manual_action", "manual_action", "outcome_observed",
    ]
    assert all(event["payload"].get("system_external_actions", 0) == 0 for event in stream)


def test_bridge_rejects_external_action_without_advancing(tmp_path: Path) -> None:
    env, service, session_id, published = _reach_published(tmp_path)
    case_id = published["case_id"]
    preview, token = _preview(service, session_id, "decide", {
        "case_id": case_id, "decision": "accept",
        "confirmed_case_checksum": published["case_checksum"],
        "reason_code": "bounded", "pilot_expected_sequence": 1,
    })
    execute_decide(service, preview, confirmation_token=token, idempotency_key="decide-negative", pilot_db=env["pilot"], now=NOW)
    preview, token = _preview(service, session_id, "preregister", {
        "case_id": case_id, "metric": "pass", "unit": "ratio", "baseline": 0,
        "target": 1, "direction": "higher", "window_start": NOW,
        "window_end": NOW, "collection_source": "local", "estimated_time_minutes": 1,
        "estimated_cost": 0, "pilot_expected_sequence": 2,
    })
    execute_preregister(service, preview, confirmation_token=token, idempotency_key="pre-negative", pilot_db=env["pilot"], now=NOW)
    preview, token = _preview(service, session_id, "action_start", {
        "case_id": case_id, "action_state": "started",
        "description": "Deploy to https://example.invalid", "operator": "user",
        "pilot_expected_sequence": 3,
    })
    with pytest.raises(Exception, match="external_action_forbidden"):
        execute_manual_action(service, preview, confirmation_token=token, idempotency_key="blocked-action", pilot_db=env["pilot"], now=NOW)
    assert service.get(session_id, now=NOW)["state"] == "preregistered"
    assert len(read_event_stream(env["pilot"], case_id)) == 3


def test_consumed_confirmation_cannot_drive_another_effect(tmp_path: Path) -> None:
    env, service, session_id, published = _reach_published(tmp_path)
    preview, token = _preview(service, session_id, "decide", {
        "case_id": published["case_id"], "decision": "accept",
        "confirmed_case_checksum": published["case_checksum"],
        "reason_code": "bounded", "pilot_expected_sequence": 1,
    })
    first = execute_decide(service, preview, confirmation_token=token, idempotency_key="decide-once", pilot_db=env["pilot"], now=NOW)
    replay = execute_decide(service, preview, confirmation_token=token, idempotency_key="decide-once", pilot_db=env["pilot"], now=NOW)
    assert replay.replayed and replay.event_id == first.event_id
    with pytest.raises(OrchestrationError, match="confirmation_consumed"):
        execute_decide(service, preview, confirmation_token=token, idempotency_key="decide-twice", pilot_db=env["pilot"], now=NOW)
    assert len(read_event_stream(env["pilot"], published["case_id"])) == 2
