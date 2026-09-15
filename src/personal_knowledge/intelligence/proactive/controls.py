"""User-owned append-only trust overlays for proactive intelligence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, ClassVar, Mapping

from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw

from .schema import canonical_json, checksum, validate_metadata_payload


GENESIS_CHECKSUM = checksum({"control_event": "genesis"})
DENIAL_OPERATIONS = frozenset({"suppress", "snooze", "revoke"})
FEEDBACK_OPERATIONS = frozenset({"mark_not_useful", "mark_wrong_timing"})


class ControlError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


@dataclass(frozen=True)
class ControlTarget:
    authority: str
    record_type: str
    record_id: str
    record_checksum: str


@dataclass(frozen=True)
class ControlCommand:
    OPERATIONS: ClassVar[frozenset[str]] = frozenset({
        "limit_scope", "suppress", "snooze", "revoke", "correct",
        "mark_not_useful", "mark_wrong_timing", "restore",
    })

    target: ControlTarget
    operation: str
    scope: str
    actor_class: str
    actor_identity_hash: str
    expected_sequence: int
    idempotency_key: str
    reason_code: str
    created_at: str
    expires_at: str | None
    rollback_of_event_id: str | None
    details: Mapping[str, Any]


@dataclass(frozen=True)
class ControlEvent:
    event_id: str
    target: ControlTarget
    sequence: int
    operation: str
    scope: str
    actor_identity_hash: str
    expected_sequence: int
    idempotency_key: str
    previous_event_checksum: str
    reason_code: str
    created_at: str
    expires_at: str | None
    rollback_of_event_id: str | None
    outcome: str
    details: Mapping[str, Any]
    before_projected_checksum: str
    after_projected_checksum: str
    payload_checksum: str


@dataclass(frozen=True)
class ControlReceipt:
    event: ControlEvent
    written: bool
    existing: bool


@dataclass(frozen=True)
class ControlProjection:
    eligible: bool
    reason_codes: tuple[str, ...]
    winning_event_id: str | None
    active_event_ids: tuple[str, ...]
    correction_requested: bool
    checksum: str


def _time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ControlError("invalid_time", field) from exc
    if parsed.tzinfo is None:
        raise ControlError("invalid_time", f"{field}:timezone_required")
    return parsed.astimezone(timezone.utc)


def _target_key(target: ControlTarget) -> tuple[str, str, str]:
    return target.authority, target.record_type, target.record_id


def _validate_command(command: ControlCommand) -> None:
    if command.operation not in command.OPERATIONS:
        raise ControlError("operation_invalid", command.operation)
    if command.actor_class != "user":
        raise ControlError("human_actor_required")
    if len(command.actor_identity_hash) != 64:
        raise ControlError("actor_identity_hash_invalid")
    if len(command.target.record_checksum) != 64:
        raise ControlError("target_checksum_invalid")
    if command.expected_sequence < 0 or not command.idempotency_key or not command.scope:
        raise ControlError("control_command_invalid")
    _time(command.created_at, "created_at")
    if command.expires_at is not None:
        if _time(command.expires_at, "expires_at") <= _time(command.created_at, "created_at"):
            raise ControlError("expiry_invalid")
    validate_metadata_payload(command.details, "control.details")
    if command.operation == "restore" and not command.rollback_of_event_id:
        raise ControlError("invalid_restore", "rollback_required")
    if command.operation != "restore" and command.rollback_of_event_id is not None:
        raise ControlError("invalid_restore", "rollback_only_for_restore")


def _validate_target(con: sqlite3.Connection, target: ControlTarget) -> None:
    synthetic = {
        ("a.proactive_intelligence", "global"): checksum({"global": target.record_id}),
        ("a.proactive_intelligence", "policy"): checksum({"policy": target.record_id}),
        ("a.proactive_intelligence", "domain"): checksum({"domain": target.record_id}),
    }
    expected = synthetic.get((target.authority, target.record_type))
    if expected is not None:
        if expected != target.record_checksum:
            raise ControlError("target_drift", target.record_id)
        return
    tables = {
        ("a.proactive_intelligence", "candidate"): ("proactive_candidates", "candidate_id"),
        ("a.proactive_intelligence", "coordination"): ("proactive_coordination_items", "coordination_id"),
        ("a.proactive_intelligence", "evaluation"): ("proactive_evaluations", "evaluation_id"),
        ("a.personal_change", "assertion"): ("personal_state_assertions", "assertion_id"),
        ("a.personal_change", "change"): ("personal_state_changes", "change_id"),
        ("a.personal_change", "risk"): ("personal_state_risks", "risk_id"),
        ("a.decision_feedback", "recommendation"): ("decision_recommendations", "recommendation_id"),
        ("a.decision_feedback", "event"): ("decision_events", "event_id"),
        ("a.decision_feedback", "effectiveness"): ("decision_effectiveness", "assessment_id"),
    }
    pair = tables.get((target.authority, target.record_type))
    if pair is None:
        raise ControlError("target_type_invalid")
    table, column = pair
    row = con.execute(f"SELECT payload_checksum FROM {table} WHERE {column}=?", (target.record_id,)).fetchone()
    if row is None or str(row[0]) != target.record_checksum:
        raise ControlError("target_drift", target.record_id)


def _row_to_event(row: sqlite3.Row) -> ControlEvent:
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ControlError("control_event_tampered", str(row["event_id"])) from exc
    if checksum(payload) != str(row["payload_checksum"]):
        raise ControlError("control_event_tampered", str(row["event_id"]))
    target_data = payload.get("target")
    if not isinstance(target_data, dict):
        raise ControlError("control_event_tampered", str(row["event_id"]))
    target = ControlTarget(**target_data)
    expected_columns = (
        target.authority, target.record_type, target.record_id, target.record_checksum,
        int(payload["sequence"]), str(payload["operation"]), str(payload["scope"]),
        str(payload["actor_identity_hash"]), int(payload["expected_sequence"]),
        str(payload["idempotency_key"]), str(payload["previous_event_checksum"]),
        str(payload["reason_code"]), payload.get("expires_at"), payload.get("rollback_of_event_id"),
    )
    actual_columns = (
        str(row["target_authority"]), str(row["target_type"]), str(row["target_id"]),
        str(row["target_checksum"]), int(row["sequence"]), str(row["operation"]),
        str(row["scope"]), str(row["actor_identity_hash"]), int(row["expected_sequence"]),
        str(row["idempotency_key"]), str(row["previous_event_checksum"]),
        str(row["reason_code"]), row["expires_at"], row["rollback_of_event_id"],
    )
    if expected_columns != actual_columns or str(row["actor_class"]) != "user":
        raise ControlError("control_event_tampered", str(row["event_id"]))
    return ControlEvent(
        str(row["event_id"]), target, int(row["sequence"]), str(row["operation"]),
        str(row["scope"]), str(row["actor_identity_hash"]), int(row["expected_sequence"]),
        str(row["idempotency_key"]), str(row["previous_event_checksum"]),
        str(row["reason_code"]), str(payload["created_at"]), payload.get("expires_at"),
        payload.get("rollback_of_event_id"), str(payload["outcome"]),
        payload.get("details", {}), str(payload["before_projected_checksum"]),
        str(payload["after_projected_checksum"]), str(row["payload_checksum"]),
    )


def _load_stream(con: sqlite3.Connection, target: ControlTarget) -> tuple[ControlEvent, ...]:
    rows = con.execute(
        "SELECT * FROM proactive_control_events WHERE target_authority=? AND target_type=? "
        "AND target_id=? ORDER BY sequence", _target_key(target),
    ).fetchall()
    events = tuple(_row_to_event(row) for row in rows)
    previous = GENESIS_CHECKSUM
    for index, event in enumerate(events, start=1):
        if event.sequence != index or event.expected_sequence != index - 1 or event.previous_event_checksum != previous:
            raise ControlError("control_chain_tampered", event.event_id)
        previous = event.payload_checksum
    for index, event in enumerate(events):
        prior = events[:index]
        before = _project(prior, as_of=event.created_at, scope=event.scope,
                          domains=frozenset(), policies=frozenset())
        after = _project(prior + (event,), as_of=event.created_at, scope=event.scope,
                         domains=frozenset(), policies=frozenset())
        if before.checksum != event.before_projected_checksum or after.checksum != event.after_projected_checksum:
            raise ControlError("control_projection_tampered", event.event_id)
    return events


def _scope_matches(event: ControlEvent, *, scope: str, domains: frozenset[str], policies: frozenset[str]) -> bool:
    label = event.scope
    if label == "global" or label == scope:
        return True
    if label.startswith("domain:"):
        return label.split(":", 1)[1] in domains
    if label.startswith("policy:"):
        return label.split(":", 1)[1] in policies
    return False


def _specificity(target: ControlTarget, event: ControlEvent) -> tuple[int, int]:
    target_rank = {"global": 1, "domain": 2, "policy": 3}.get(target.record_type, 4)
    scope_rank = 1 if event.scope == "global" else 2 if event.scope.startswith("domain:") else 3 if event.scope.startswith("policy:") else 4
    return target_rank, scope_rank


def _latest_control(events: tuple[ControlEvent, ...]) -> ControlEvent:
    """Select by verified stream order, then compare streams on normalized time."""
    latest_by_target: dict[tuple[str, str, str], ControlEvent] = {}
    for event in events:
        target_key = _target_key(event.target)
        current = latest_by_target.get(target_key)
        if current is None or (event.sequence, event.event_id) > (current.sequence, current.event_id):
            latest_by_target[target_key] = event
    return max(
        latest_by_target.values(),
        key=lambda item: (
            _time(item.created_at, "created_at"),
            _target_key(item.target),
            item.sequence,
            item.event_id,
        ),
    )


def _project(events: tuple[ControlEvent, ...], *, as_of: str, scope: str,
             domains: frozenset[str], policies: frozenset[str]) -> ControlProjection:
    instant = _time(as_of, "as_of")
    restored = {event.rollback_of_event_id for event in events
                if event.operation == "restore" and _time(event.created_at, "created_at") <= instant}
    active = tuple(event for event in events if event.operation != "restore" and event.event_id not in restored
                   and _time(event.created_at, "created_at") <= instant
                   and (event.expires_at is None or instant < _time(event.expires_at, "expires_at"))
                   and _scope_matches(event, scope=scope, domains=domains, policies=policies))
    correction = any(event.operation == "correct" for event in active)
    candidates = tuple(event for event in active if event.operation not in FEEDBACK_OPERATIONS | {"correct"})
    winning: ControlEvent | None = None
    reason: tuple[str, ...] = ()
    eligible = True
    if candidates:
        best = max(_specificity(event.target, event) for event in candidates)
        top = tuple(event for event in candidates if _specificity(event.target, event) == best)
        denials = tuple(event for event in top if event.operation in DENIAL_OPERATIONS)
        winning = _latest_control(denials or top)
        if len({event.operation for event in top}) > 1 and not denials:
            eligible, reason, winning = False, ("trust_veto", "ambiguous_control"), None
        elif winning.operation == "suppress":
            eligible, reason = False, ("trust_veto", "suppressed_by_user")
        elif winning.operation == "snooze":
            eligible, reason = False, ("trust_veto", "snoozed_by_user")
        elif winning.operation == "revoke":
            eligible, reason = False, ("trust_veto", "revoked_by_user")
        elif winning.operation == "limit_scope":
            allowed = winning.details.get("allowed_scopes", [])
            if not isinstance(allowed, list) or scope not in {str(item) for item in allowed}:
                eligible, reason = False, ("trust_veto", "scope_limited")
    body = {
        "eligible": eligible, "reason_codes": reason,
        "winning_event_id": winning.event_id if winning else None,
        "active_event_ids": [event.event_id for event in active],
        "correction_requested": correction,
    }
    return ControlProjection(eligible, reason, body["winning_event_id"], tuple(body["active_event_ids"]), correction, checksum(body))


def project_controls(db_path: Path, *, targets: tuple[ControlTarget, ...], as_of: str,
                     scope: str = "global", domains: tuple[str, ...] = (),
                     policies: tuple[str, ...] = ()) -> ControlProjection:
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return project_controls_connection(con, targets=targets, as_of=as_of, scope=scope,
                                           domains=domains, policies=policies)
    finally:
        con.close()


def project_controls_connection(con: sqlite3.Connection, *, targets: tuple[ControlTarget, ...],
                                as_of: str, scope: str = "global",
                                domains: tuple[str, ...] = (), policies: tuple[str, ...] = ()) -> ControlProjection:
    """Project controls using the caller's consistent SQLite snapshot/transaction."""
    events: list[ControlEvent] = []
    seen: set[tuple[str, str, str]] = set()
    for target in targets:
        key = _target_key(target)
        if key in seen:
            continue
        seen.add(key)
        _validate_target(con, target)
        events.extend(_load_stream(con, target))
    return _project(tuple(events), as_of=as_of, scope=scope,
                    domains=frozenset(domains), policies=frozenset(policies))


def append_control(db_path: Path, command: ControlCommand, *, write: bool,
                   inject_failure: bool = False) -> ControlReceipt:
    _validate_command(command)
    con = connect_rw(Path(db_path), timeout=60)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        assert_foreign_key_integrity(con)
        _validate_target(con, command.target)
        events = _load_stream(con, command.target)
        existing = next((event for event in events if event.actor_identity_hash == command.actor_identity_hash
                         and event.idempotency_key == command.idempotency_key), None)
        command_identity = {
            "target": command.target.__dict__, "operation": command.operation, "scope": command.scope,
            "actor_identity_hash": command.actor_identity_hash, "expected_sequence": command.expected_sequence,
            "idempotency_key": command.idempotency_key, "reason_code": command.reason_code,
            "created_at": command.created_at, "expires_at": command.expires_at,
            "rollback_of_event_id": command.rollback_of_event_id, "details": command.details,
        }
        if existing is not None:
            existing_identity = {key: existing.details if key == "details" else existing.target.__dict__ if key == "target" else getattr(existing, key) for key in command_identity}
            if canonical_json(existing_identity) != canonical_json(command_identity):
                raise ControlError("idempotency_conflict")
            con.rollback()
            return ControlReceipt(existing, False, True)
        if command.expected_sequence != len(events):
            raise ControlError("stale_sequence")
        before = _project(events, as_of=command.created_at, scope=command.scope,
                          domains=frozenset(), policies=frozenset())
        if command.operation == "restore":
            original = next((event for event in events if event.event_id == command.rollback_of_event_id), None)
            if original is None or original.operation == "restore" or any(
                event.operation == "restore" and event.rollback_of_event_id == original.event_id for event in events
            ):
                raise ControlError("invalid_restore")
        sequence = len(events) + 1
        previous = events[-1].payload_checksum if events else GENESIS_CHECKSUM
        outcome = "canonical_correction_requested" if command.operation == "correct" else "overlay_recorded"
        seed = {**command_identity, "sequence": sequence, "previous_event_checksum": previous}
        event_id = f"pce_{checksum(seed)[:24]}"
        provisional = ControlEvent(event_id, command.target, sequence, command.operation, command.scope,
            command.actor_identity_hash, command.expected_sequence, command.idempotency_key, previous,
            command.reason_code, command.created_at, command.expires_at, command.rollback_of_event_id,
            outcome, command.details, before.checksum, "", "")
        after = _project(events + (provisional,), as_of=command.created_at, scope=command.scope,
                         domains=frozenset(), policies=frozenset())
        payload = {**seed, "event_id": event_id, "outcome": outcome,
                   "before_projected_checksum": before.checksum,
                   "after_projected_checksum": after.checksum}
        payload_checksum = checksum(payload)
        event = ControlEvent(event_id, command.target, sequence, command.operation, command.scope,
            command.actor_identity_hash, command.expected_sequence, command.idempotency_key, previous,
            command.reason_code, command.created_at, command.expires_at, command.rollback_of_event_id,
            outcome, command.details, before.checksum, after.checksum, payload_checksum)
        if not write:
            con.rollback()
            return ControlReceipt(event, False, False)
        con.execute(
            "INSERT INTO proactive_control_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event.event_id, event.target.authority, event.target.record_type, event.target.record_id,
             event.target.record_checksum, event.sequence, event.operation, event.scope, "user",
             event.actor_identity_hash, event.expected_sequence, event.idempotency_key,
             event.previous_event_checksum, event.reason_code, event.expires_at,
             event.rollback_of_event_id, canonical_json(payload), event.payload_checksum, event.created_at),
        )
        if inject_failure:
            raise RuntimeError("injected control append failure")
        assert_foreign_key_integrity(con)
        con.commit()
        return ControlReceipt(event, True, False)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def active_control_frontier(db_path: Path) -> str:
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT target_authority,target_type,target_id,sequence,payload_checksum "
            "FROM proactive_control_events ORDER BY target_authority,target_type,target_id,sequence"
        ).fetchall()
        return checksum({"control_events": [tuple(row) for row in rows]})
    finally:
        con.close()
