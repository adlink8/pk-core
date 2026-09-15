"""User-owned append-only decision and manual-action workflow for pilot cases."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from personal_knowledge.core.sqlite import connect_rw
from personal_knowledge.intelligence.analysis.schema import canonical_json, checksum, stable_id

from .schema import SCHEMA_VERSION, inspect_schema


class PilotWorkflowError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class PilotEventReceipt:
    event_id: str
    case_id: str
    sequence: int
    event_type: str
    payload_checksum: str
    written: bool
    existing: bool


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(value: str) -> datetime:
    if not value.endswith("Z"):
        raise PilotWorkflowError("timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PilotWorkflowError("timestamp_invalid") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PilotWorkflowError("timestamp_invalid")
    return parsed


def _case(con: sqlite3.Connection, case_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM pilot_cases WHERE case_id=?", (case_id,)).fetchone()
    if row is None:
        raise PilotWorkflowError("pilot_case_missing", case_id)
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError as exc:
        raise PilotWorkflowError("pilot_case_payload_invalid") from exc
    if checksum(payload) != str(row["payload_checksum"]):
        raise PilotWorkflowError("pilot_case_checksum_mismatch")
    return row


def read_event_stream(db_path: Path | str, case_id: str) -> tuple[dict[str, Any], ...]:
    path = Path(db_path)
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        _case(con, case_id)
        rows = con.execute("SELECT * FROM pilot_events WHERE case_id=? ORDER BY sequence", (case_id,)).fetchall()
        previous = "GENESIS"
        result: list[dict[str, Any]] = []
        for expected, row in enumerate(rows, start=1):
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError as exc:
                raise PilotWorkflowError("pilot_event_payload_invalid") from exc
            if int(row["sequence"]) != expected:
                raise PilotWorkflowError("pilot_event_sequence_invalid")
            if str(row["previous_event_checksum"]) != previous:
                raise PilotWorkflowError("pilot_event_chain_invalid")
            digest = checksum(payload)
            if digest != str(row["payload_checksum"]):
                raise PilotWorkflowError("pilot_event_checksum_mismatch")
            if payload.get("case_id") != case_id or payload.get("event_type") != row["event_type"]:
                raise PilotWorkflowError("pilot_event_lineage_invalid")
            result.append({**dict(row), "payload": payload})
            previous = digest
        if not result or result[0]["event_type"] != "case_frozen":
            raise PilotWorkflowError("pilot_event_genesis_missing")
        return tuple(result)
    finally:
        con.close()


def _append(
    db_path: Path | str, *, case_id: str, event_type: str,
    body: Mapping[str, Any], actor: str, actor_identity_hash: str,
    expected_sequence: int, idempotency_key: str, occurred_at: str | None,
) -> PilotEventReceipt:
    if inspect_schema(db_path).get("schema_state") != "applied":
        raise PilotWorkflowError("pilot_schema_invalid")
    if not idempotency_key.strip() or len(idempotency_key) > 256:
        raise PilotWorkflowError("idempotency_key_invalid")
    if len(actor_identity_hash) != 64 or any(char not in "0123456789abcdef" for char in actor_identity_hash):
        raise PilotWorkflowError("actor_identity_hash_invalid")
    event_core = {
        "schema_version": SCHEMA_VERSION, "case_id": case_id, "event_type": event_type,
        "actor": actor, "actor_identity_hash": actor_identity_hash,
        "expected_sequence": expected_sequence, "idempotency_key": idempotency_key,
        "body": dict(body), "system_external_actions": 0,
    }
    event_id = stable_id("ppe", {"case_id": case_id, "idempotency_key": idempotency_key})
    digest = checksum(event_core)
    con = connect_rw(Path(db_path), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        _case(con, case_id)
        existing = con.execute("SELECT * FROM pilot_events WHERE event_id=?", (event_id,)).fetchone()
        if existing is not None:
            if str(existing["payload_checksum"]) != digest or str(existing["event_type"]) != event_type:
                raise PilotWorkflowError("idempotency_conflict")
            con.rollback()
            return PilotEventReceipt(event_id, case_id, int(existing["sequence"]), event_type, digest, False, True)
        latest = con.execute(
            "SELECT sequence,payload_checksum FROM pilot_events WHERE case_id=? ORDER BY sequence DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if latest is None or int(latest["sequence"]) != expected_sequence:
            raise PilotWorkflowError("stale_expected_sequence")
        sequence = expected_sequence + 1
        con.execute(
            "INSERT INTO pilot_events VALUES (?,?,?,?,?,?,?,?)",
            (event_id, case_id, sequence, event_type, str(latest["payload_checksum"]),
             canonical_json(event_core), digest, occurred_at or _now()),
        )
        con.commit()
        return PilotEventReceipt(event_id, case_id, sequence, event_type, digest, True, False)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def preregister_outcome(
    db_path: Path | str, *, case_id: str, metric: str, unit: str,
    baseline: float, target: float, direction: str, window_start: str, window_end: str,
    collection_source: str, estimated_time_minutes: float, estimated_cost: float,
    expected_sequence: int, idempotency_key: str, actor_identity_hash: str,
    occurred_at: str | None = None,
) -> PilotEventReceipt:
    if direction not in {"higher", "lower", "equal"} or not metric.strip() or not unit.strip():
        raise PilotWorkflowError("outcome_protocol_invalid")
    if _utc(window_end) < _utc(window_start):
        raise PilotWorkflowError("outcome_window_invalid")
    stream = read_event_stream(db_path, case_id)
    if any(item["event_type"] == "outcome_preregistered" for item in stream):
        replay = next(
            (item for item in stream if item["event_type"] == "outcome_preregistered"
             and item["payload"].get("idempotency_key") == idempotency_key), None,
        )
        if replay is None:
            raise PilotWorkflowError("outcome_already_preregistered")
    body = {
        "metric": metric, "unit": unit, "baseline": float(baseline), "target": float(target),
        "direction": direction, "window_start": window_start, "window_end": window_end,
        "collection_source": collection_source, "estimated_time_minutes": float(estimated_time_minutes),
        "estimated_cost": float(estimated_cost),
    }
    return _append(
        db_path, case_id=case_id, event_type="outcome_preregistered", body=body,
        actor="user", actor_identity_hash=actor_identity_hash, expected_sequence=expected_sequence,
        idempotency_key=idempotency_key, occurred_at=occurred_at,
    )


def record_user_decision(
    db_path: Path | str, *, case_id: str, decision: str,
    confirmed_case_checksum: str, reason_code: str, expected_sequence: int,
    idempotency_key: str, actor_identity_hash: str, occurred_at: str | None = None,
) -> PilotEventReceipt:
    if decision not in {"accept", "reject", "defer"}:
        raise PilotWorkflowError("decision_invalid")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        case = _case(con, case_id)
    finally:
        con.close()
    if str(case["payload_checksum"]) != confirmed_case_checksum:
        raise PilotWorkflowError("case_confirmation_checksum_mismatch")
    stream = read_event_stream(db_path, case_id)
    decisions = [item for item in stream if item["event_type"] == "user_decision"]
    if decisions and decisions[0]["payload"].get("idempotency_key") != idempotency_key:
        raise PilotWorkflowError("decision_already_recorded")
    return _append(
        db_path, case_id=case_id, event_type="user_decision",
        body={"decision": decision, "confirmed_case_checksum": confirmed_case_checksum,
              "reason_code": reason_code}, actor="user", actor_identity_hash=actor_identity_hash,
        expected_sequence=expected_sequence, idempotency_key=idempotency_key, occurred_at=occurred_at,
    )


def record_manual_action(
    db_path: Path | str, *, case_id: str, action_state: str,
    description: str, operator: str, expected_sequence: int, idempotency_key: str,
    actor_identity_hash: str, occurred_at: str | None = None,
) -> PilotEventReceipt:
    if action_state not in {"started", "completed", "abandoned"}:
        raise PilotWorkflowError("action_state_invalid")
    if not description.strip() or len(description) > 1_000 or operator not in {"user", "codex_operator"}:
        raise PilotWorkflowError("manual_action_invalid")
    lowered = description.lower()
    if any(token in lowered for token in ("http://", "https://", "deploy", "send message", "purchase")):
        raise PilotWorkflowError("external_action_forbidden")
    stream = read_event_stream(db_path, case_id)
    decisions = [item for item in stream if item["event_type"] == "user_decision"]
    if len(decisions) != 1 or decisions[0]["payload"]["body"]["decision"] != "accept":
        raise PilotWorkflowError("accepted_decision_required")
    prior_actions = [item["payload"]["body"]["action_state"] for item in stream if item["event_type"] == "manual_action"]
    same_replay = any(
        item["event_type"] == "manual_action"
        and item["payload"].get("idempotency_key") == idempotency_key
        for item in stream
    )
    if action_state == "started" and prior_actions and not same_replay:
        raise PilotWorkflowError("manual_action_already_started")
    if action_state in {"completed", "abandoned"} and prior_actions != ["started"] and not same_replay:
        raise PilotWorkflowError("manual_action_transition_invalid")
    return _append(
        db_path, case_id=case_id, event_type="manual_action",
        body={"action_state": action_state, "description": description, "operator": operator,
              "reported_by": "user", "automated_external_action": False},
        actor="user", actor_identity_hash=actor_identity_hash, expected_sequence=expected_sequence,
        idempotency_key=idempotency_key, occurred_at=occurred_at,
    )


__all__ = [
    "PilotEventReceipt", "PilotWorkflowError", "preregister_outcome",
    "read_event_stream", "record_manual_action", "record_user_decision",
]
