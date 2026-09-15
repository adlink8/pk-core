from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from personal_knowledge.intelligence.analysis.schema import checksum
from personal_knowledge.intelligence.decision.context_binding import (
    DecisionContextBinding, DecisionContextPolicy,
)
from personal_knowledge.intelligence.orchestration import (
    OrchestrationError, OrchestrationService, Preview, apply_schema, inspect_schema,
)


SECRET = b"phase-33-confirmation-secret-32-bytes-minimum"
ACTOR = hashlib.sha256(b"actor-user-verified").hexdigest()
NOW = "2026-07-19T01:00:00Z"


def _binding(*_args, **_kwargs) -> DecisionContextBinding:
    draft = DecisionContextBinding(
        personal_snapshot_id="ss_personal", personal_snapshot_hash="a" * 64,
        external_snapshot_id="exs_external", external_snapshot_hash="b" * 64,
        policy=DecisionContextPolicy("global", 86_400), bound_at=NOW,
        binding_hash="",
    )
    return replace(draft, binding_hash=checksum(draft.core()))


def _validate(value, *_args, **_kwargs):
    typed = value if isinstance(value, DecisionContextBinding) else DecisionContextBinding.from_dict(value)
    if checksum(typed.core()) != typed.binding_hash:
        raise ValueError("binding_hash_mismatch")
    return {"binding": typed.to_dict()}


@pytest.fixture()
def service(tmp_path: Path) -> OrchestrationService:
    db = tmp_path / "orchestration.sqlite"
    apply_schema(db)
    return OrchestrationService(
        db_path=db, personal_db=tmp_path / "personal.sqlite",
        external_db=tmp_path / "external.sqlite", confirmation_secret=SECRET,
        binding_factory=_binding, binding_validator=_validate,
    )


def _preview(service: OrchestrationService):
    return service.prepare(
        goal="Choose a local validation approach",
        constraints=("No external action", "Maximum 30 minutes"),
        weights={"reversibility": 0.8, "time": 0.6},
        actor_identity_hash=ACTOR, now=NOW,
    )


def _confirmed(service: OrchestrationService):
    preview = _preview(service)
    token = service.issue_confirmation(preview)
    result = service.confirm(
        preview, confirmation_token=token, idempotency_key="confirm-1", now=NOW,
    )
    return preview, result


def test_schema_is_complete_and_append_only(tmp_path: Path):
    db = tmp_path / "orchestration.sqlite"
    applied = apply_schema(db)
    assert applied == inspect_schema(db)
    assert applied["schema_state"] == "applied"
    assert applied["immutable_triggers"] == 8


def test_prepare_is_pure_bounded_and_risk_gated(service: OrchestrationService):
    before = hashlib.sha256(service.db_path.read_bytes()).hexdigest()
    preview = _preview(service)
    after = hashlib.sha256(service.db_path.read_bytes()).hexdigest()
    assert before == after
    assert preview.operation == "confirm"
    assert preview.expected_sequence == 0
    assert preview.payload["binding_hash"] == _binding().binding_hash
    with pytest.raises(OrchestrationError, match="domain_not_allowed"):
        service.prepare(
            goal="anything", constraints=("bounded",), weights={"x": 1},
            actor_identity_hash=ACTOR, domain="finance", now=NOW,
        )
    with pytest.raises(OrchestrationError, match="high_risk_or_external_action_forbidden"):
        service.prepare(
            goal="Deploy the result", constraints=("bounded",), weights={"x": 1},
            actor_identity_hash=ACTOR, now=NOW,
        )


def test_preview_checksum_survives_javascript_number_roundtrip(service: OrchestrationService):
    preview = service.prepare(
        goal="Choose a local validation approach", constraints=("bounded",),
        weights={"safety": 1.0, "reversibility": 0.8},
        actor_identity_hash=ACTOR, now=NOW,
    )
    wire = json.loads(json.dumps(preview.to_dict()).replace('"safety": 1.0', '"safety": 1'))
    restored = Preview.from_dict(wire)
    assert restored.preview_checksum == preview.preview_checksum
    assert restored.payload["weights"]["safety"] == 1


def test_confirmation_binds_preview_actor_operation_sequence_and_expiry(service: OrchestrationService):
    preview = _preview(service)
    token = service.issue_confirmation(preview, expires_at="2026-07-19T01:05:00Z")
    drifted = replace(preview, actor_identity_hash="f" * 64)
    with pytest.raises(OrchestrationError, match="preview_checksum_mismatch"):
        service.confirm(drifted, confirmation_token=token, idempotency_key="x", now=NOW)
    with pytest.raises(OrchestrationError, match="confirmation_expired"):
        service.confirm(
            preview, confirmation_token=token, idempotency_key="expired",
            now="2026-07-19T01:05:01Z",
        )
    result = service.confirm(
        preview, confirmation_token=token, idempotency_key="confirm", now=NOW,
    )
    assert result.state == "confirmed"
    assert result.sequence == 1


def test_exact_replay_returns_original_and_conflict_fails(service: OrchestrationService):
    preview, first = _confirmed(service)
    token = service.issue_confirmation(preview)
    replay = service.confirm(
        preview, confirmation_token=token, idempotency_key="confirm-1", now=NOW,
    )
    assert replay.event_id == first.event_id
    assert replay.replayed is True
    with pytest.raises(OrchestrationError, match="session_already_confirmed"):
        service.confirm(
            preview, confirmation_token=token, idempotency_key="different", now=NOW,
        )


def test_transition_table_sequence_and_idempotency(service: OrchestrationService):
    _, confirmed = _confirmed(service)
    preview = service.preview_transition(
        confirmed.session_id, "generate", {"evidence_ids": ["e1"]},
        actor_identity_hash=ACTOR, expected_sequence=1, now=NOW,
    )
    token = service.issue_confirmation(preview)
    generated = service.commit_transition(
        preview, confirmation_token=token, idempotency_key="generate-1",
        references={"run_id": "dar_1"}, now=NOW,
    )
    assert generated.state == "generated"
    replay = service.commit_transition(
        preview, confirmation_token=token, idempotency_key="generate-1",
        references={"run_id": "ignored-on-replay"}, now=NOW,
    )
    assert replay.event_id == generated.event_id and replay.replayed
    with pytest.raises(OrchestrationError, match="stale_expected_sequence"):
        service.preview_transition(
            confirmed.session_id, "publish", {}, actor_identity_hash=ACTOR,
            expected_sequence=1, now=NOW,
        )
    with pytest.raises(OrchestrationError, match="illegal_transition"):
        service.preview_transition(
            confirmed.session_id, "decide", {}, actor_identity_hash=ACTOR,
            expected_sequence=2, now=NOW,
        )


def test_resume_fails_closed_on_corrupt_event_chain(service: OrchestrationService):
    _, result = _confirmed(service)
    con = sqlite3.connect(service.db_path)
    try:
        con.execute("DROP TRIGGER trg_orchestration_events_update_immutable")
        con.execute(
            "UPDATE orchestration_events SET previous_event_checksum='tampered' WHERE session_id=?",
            (result.session_id,),
        )
        con.execute(
            "CREATE TRIGGER trg_orchestration_events_update_immutable "
            "BEFORE UPDATE ON orchestration_events BEGIN SELECT RAISE(ABORT,'append_only'); END"
        )
        con.commit()
    finally:
        con.close()
    with pytest.raises(OrchestrationError, match="event_chain_invalid"):
        service.resume(result.session_id, now=NOW, revalidate_binding=False)
