"""Compensating pilot controls and read-only snapshot recovery validation."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .workflow import PilotEventReceipt, PilotWorkflowError, _append, read_event_stream


def _target(stream: tuple[dict[str, Any], ...], payload_checksum: str) -> dict[str, Any]:
    item = next((event for event in stream if event["payload_checksum"] == payload_checksum), None)
    if item is None:
        raise PilotWorkflowError("control_target_missing")
    return item


def record_correction(
    db_path: Path | str, *, case_id: str, target_checksum: str,
    corrected_fields: Mapping[str, Any], reason_code: str, expected_sequence: int,
    idempotency_key: str, actor_identity_hash: str, occurred_at: str | None = None,
) -> PilotEventReceipt:
    stream = read_event_stream(db_path, case_id)
    _target(stream, target_checksum)
    if not corrected_fields or len(corrected_fields) > 16 or not reason_code.strip():
        raise PilotWorkflowError("correction_invalid")
    forbidden = {"command", "credential", "executed", "external_action"}
    if forbidden & {str(key).lower() for key in corrected_fields}:
        raise PilotWorkflowError("correction_field_forbidden")
    return _append(
        db_path, case_id=case_id, event_type="correction",
        body={"target_checksum": target_checksum, "corrected_fields": dict(corrected_fields),
              "reason_code": reason_code, "compensating": True},
        actor="user", actor_identity_hash=actor_identity_hash, expected_sequence=expected_sequence,
        idempotency_key=idempotency_key, occurred_at=occurred_at,
    )


def record_revoke(
    db_path: Path | str, *, case_id: str, target_checksum: str,
    reason_code: str, expected_sequence: int, idempotency_key: str,
    actor_identity_hash: str, occurred_at: str | None = None,
) -> PilotEventReceipt:
    stream = read_event_stream(db_path, case_id)
    target = _target(stream, target_checksum)
    if target["event_type"] not in {"user_decision", "manual_action", "outcome_observed", "correction"}:
        raise PilotWorkflowError("revoke_target_invalid")
    return _append(
        db_path, case_id=case_id, event_type="revoke",
        body={"target_checksum": target_checksum, "reason_code": reason_code, "compensating": True},
        actor="user", actor_identity_hash=actor_identity_hash, expected_sequence=expected_sequence,
        idempotency_key=idempotency_key, occurred_at=occurred_at,
    )


def record_restore(
    db_path: Path | str, *, case_id: str, revoke_checksum: str,
    reason_code: str, expected_sequence: int, idempotency_key: str,
    actor_identity_hash: str, occurred_at: str | None = None,
) -> PilotEventReceipt:
    stream = read_event_stream(db_path, case_id)
    revoked = _target(stream, revoke_checksum)
    if revoked["event_type"] != "revoke":
        raise PilotWorkflowError("restore_target_invalid")
    return _append(
        db_path, case_id=case_id, event_type="restore",
        body={"revoke_checksum": revoke_checksum,
              "restored_target_checksum": revoked["payload"]["body"]["target_checksum"],
              "reason_code": reason_code, "compensating": True},
        actor="user", actor_identity_hash=actor_identity_hash, expected_sequence=expected_sequence,
        idempotency_key=idempotency_key, occurred_at=occurred_at,
    )


def record_snapshot_transition(
    db_path: Path | str, *, case_id: str, action: str,
    personal_db_path: Path | str, external_db_path: Path | str,
    expected_sequence: int, idempotency_key: str, actor_identity_hash: str,
    occurred_at: str | None = None, now: str | None = None,
) -> PilotEventReceipt:
    if action not in {"rollback", "forward_restore"}:
        raise PilotWorkflowError("snapshot_action_invalid")
    stream = read_event_stream(db_path, case_id)
    rollbacks = [item for item in stream if item["event_type"] == "snapshot_rollback"]
    restores = [item for item in stream if item["event_type"] == "snapshot_forward_restore"]
    import json
    import sqlite3
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT payload_json FROM pilot_cases WHERE case_id=?", (case_id,)).fetchone()
    finally:
        con.close()
    if row is None:
        raise PilotWorkflowError("pilot_case_missing")
    case_payload = json.loads(str(row["payload_json"]))
    request_binding = case_payload["source"]
    source_snapshots = case_payload["snapshots"]
    # The exact source binding is re-read from the admitted analysis authority by the
    # admission gate. Recovery additionally proves both active pointers still match.
    from personal_knowledge.intelligence.decision.context_binding import create_decision_context_binding
    current = create_decision_context_binding(
        personal_db_path, external_db_path, region="global",
        max_external_age_seconds=31_536_000, now=now,
    )
    if (current.personal_snapshot_id != source_snapshots["personal"]["snapshot_id"]
            or current.personal_snapshot_hash != source_snapshots["personal"]["snapshot_hash"]
            or current.external_snapshot_id != source_snapshots["external"]["snapshot_id"]
            or current.external_snapshot_hash != source_snapshots["external"]["snapshot_hash"]
            or current.binding_hash != request_binding["binding_hash"]):
        # binding_hash includes bound_at, so compare source snapshot identity above and
        # retain the original binding hash as the immutable restoration target.
        if (current.personal_snapshot_id != source_snapshots["personal"]["snapshot_id"]
                or current.personal_snapshot_hash != source_snapshots["personal"]["snapshot_hash"]
                or current.external_snapshot_id != source_snapshots["external"]["snapshot_id"]
                or current.external_snapshot_hash != source_snapshots["external"]["snapshot_hash"]):
            raise PilotWorkflowError("snapshot_active_pointer_drift")
    if action == "rollback":
        if rollbacks and not restores:
            raise PilotWorkflowError("snapshot_already_rolled_back")
        event_type = "snapshot_rollback"
        body = {"from_binding_hash": request_binding["binding_hash"], "to_state": "UNBOUND",
                "source_authority_mutated": False, "compensating": True}
    else:
        if len(rollbacks) != len(restores) + 1:
            raise PilotWorkflowError("snapshot_rollback_required")
        event_type = "snapshot_forward_restore"
        body = {"from_state": "UNBOUND", "to_binding_hash": request_binding["binding_hash"],
                "personal_snapshot": source_snapshots["personal"],
                "external_snapshot": source_snapshots["external"],
                "source_authority_mutated": False, "compensating": True}
    return _append(
        db_path, case_id=case_id, event_type=event_type, body=body,
        actor="user", actor_identity_hash=actor_identity_hash, expected_sequence=expected_sequence,
        idempotency_key=idempotency_key, occurred_at=occurred_at,
    )


__all__ = [
    "record_correction", "record_restore", "record_revoke", "record_snapshot_transition",
]
