"""Permission-safe append-only recommendation decision and action streams."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw

from .schema import (
    GENESIS_SENTINEL,
    SCHEMA_VERSION,
    DecisionEvent,
    DecisionReceipt,
    DecisionState,
    canonical_json,
    checksum,
)
from .effectiveness import (
    EffectivenessAssessment,
    assess_outcome,
    outcome_from_payload,
    registered_effectiveness_rule,
)


_ACTION_STATES = frozenset({"planned", "started", "completed", "abandoned", "not_taken"})
_CONFIRMATIONS = frozenset({"accept", "reject", "defer", "revoke_before_action"})
_FORBIDDEN_ACTION_KEYS = frozenset(
    {"command", "url", "uri", "connector", "credential", "token", "dispatch_target", "executable"}
)
_FORBIDDEN_OUTCOME_KEYS = frozenset(
    {"body", "content", "raw_body", "raw_content", "note", "notes", "secret", "token",
     "credential", "credentials", "password", "cookie", "prompt", "message"}
)
_OUTCOME_SOURCES = frozenset({"user_reported", "evidence_measured"})
_OUTCOME_DIRECTIONS = frozenset({"increase", "decrease", "maintain"})
_ADHERENCE = frozenset({"adhered", "non_adherent", "unknown"})
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://")


class DecisionStateError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DecisionStateError("invalid_time", field) from exc
    if parsed.tzinfo is None:
        raise DecisionStateError("invalid_time", f"{field}:timezone_required")
    return parsed.astimezone(timezone.utc)


def _validate_actor(actor_class: str, actor_identity_hash: str) -> None:
    if actor_class != "user":
        raise DecisionStateError("human_actor_required", actor_class)
    if len(actor_identity_hash) != 64:
        raise DecisionStateError("actor_identity_hash_invalid")


def _reject_action_metadata(value: Any, path: str = "metadata") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            label = str(key).lower()
            if label in _FORBIDDEN_ACTION_KEYS:
                raise DecisionStateError("forbidden_action_field", f"{path}.{label}")
            _reject_action_metadata(item, f"{path}.{label}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_action_metadata(item, f"{path}[{index}]")
    elif isinstance(value, str) and _URL_RE.search(value):
        raise DecisionStateError("forbidden_action_field", path)


def validate_outcome_metadata(metadata: Mapping[str, Any], evidence_refs: tuple[Mapping[str, Any], ...]) -> None:
    """Reject source bodies/secrets and require bounded typed reference manifests."""
    def inspect(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                label = str(key).lower()
                if label in _FORBIDDEN_OUTCOME_KEYS:
                    raise DecisionStateError("forbidden_outcome_field", f"{path}.{label}")
                inspect(item, f"{path}.{label}")
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                inspect(item, f"{path}[{index}]")
    inspect(metadata, "metadata")
    for ref in evidence_refs:
        if not isinstance(ref, Mapping):
            raise DecisionStateError("typed_evidence_ref_required")
        required = {"cognitive_type", "authority_id", "record_id", "record_checksum",
                    "source_run_id", "snapshot_id", "snapshot_hash"}
        if set(ref) != required:
            raise DecisionStateError("typed_evidence_ref_invalid")
        if ref["cognitive_type"] not in {"fact", "observation", "inference"}:
            raise DecisionStateError("typed_evidence_ref_invalid")
        if ref["authority_id"] != "a.personal_change" or len(str(ref["record_checksum"])) != 64:
            raise DecisionStateError("typed_evidence_ref_invalid")


def _load_recommendation(con: sqlite3.Connection, recommendation_id: str) -> tuple[sqlite3.Row, dict[str, Any], sqlite3.Row]:
    row = con.execute(
        "SELECT * FROM decision_recommendations WHERE recommendation_id=?", (recommendation_id,)
    ).fetchone()
    if row is None:
        raise DecisionStateError("recommendation_missing", recommendation_id)
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError as exc:
        raise DecisionStateError("recommendation_payload_invalid", recommendation_id) from exc
    if checksum(payload) != str(row["payload_checksum"]):
        raise DecisionStateError("recommendation_checksum_mismatch", recommendation_id)
    run = con.execute("SELECT * FROM decision_runs WHERE run_id=?", (row["run_id"],)).fetchone()
    if run is None:
        raise DecisionStateError("decision_run_missing", str(row["run_id"]))
    for column in ("input_manifest", "output_manifest"):
        try:
            value = json.loads(str(run[f"{column}_json"]))
        except json.JSONDecodeError as exc:
            raise DecisionStateError("decision_run_manifest_invalid", column) from exc
        if checksum(value) != str(run[f"{column}_checksum"]):
            raise DecisionStateError("decision_run_checksum_mismatch", column)
    return row, payload, run


def _validate_source_binding(con: sqlite3.Connection, recommendation_id: str) -> None:
    """Validate the immutable Phase 25 anchor using the append transaction.

    The caller must already hold ``BEGIN IMMEDIATE`` on ``con``.  Keeping all
    reads on that connection makes source validation and the later append one
    serialized operation instead of a check on a racing read connection.
    """
    def fail(detail: str) -> None:
        raise DecisionStateError("source_binding_invalid", detail)

    def manifest(row: sqlite3.Row, name: str, owner: str) -> dict[str, Any]:
        try:
            value = json.loads(str(row[f"{name}_manifest_json"]))
        except (TypeError, json.JSONDecodeError):
            fail(f"{owner}_{name}_manifest")
        if not isinstance(value, dict) or checksum(value) != str(row[f"{name}_manifest_checksum"]):
            fail(f"{owner}_{name}_checksum")
        return value

    rec = con.execute(
        "SELECT * FROM decision_recommendations WHERE recommendation_id=?",
        (recommendation_id,),
    ).fetchone()
    if rec is None:
        fail("recommendation_missing")
    run = con.execute("SELECT * FROM decision_runs WHERE run_id=?", (rec["run_id"],)).fetchone()
    if run is None:
        fail("decision_run_missing")
    source = con.execute(
        "SELECT r.*,p.publication_sequence,s.manifest_hash AS current_snapshot_hash "
        "FROM personal_state_runs r "
        "LEFT JOIN personal_state_publications p ON p.run_id=r.run_id "
        "LEFT JOIN serving_snapshots s ON s.snapshot_id=r.snapshot_id "
        "WHERE r.run_id=?",
        (run["source_run_id"],),
    ).fetchone()
    if source is None or source["publication_sequence"] is None or source["current_snapshot_hash"] is None:
        fail("source_run_unpublished")

    source_input = manifest(source, "input", "source")
    source_output = manifest(source, "output", "source")
    source_anchor = (
        str(source["run_id"]),
        str(source["output_manifest_checksum"]),
        int(source["publication_sequence"]),
        str(source["snapshot_id"]),
        str(source["snapshot_hash"]),
    )
    if (
        str(source["status"]) != "committed"
        or str(source["current_snapshot_hash"]) != source_anchor[4]
        or source_input.get("snapshot_id") != source_anchor[3]
        or source_input.get("snapshot_hash") != source_anchor[4]
        or source_input.get("producer_version") != str(source["producer_version"])
        or source_output.get("run_id") != source_anchor[0]
        or source_output.get("snapshot_id") != source_anchor[3]
        or source_output.get("snapshot_hash") != source_anchor[4]
    ):
        fail("source_manifest_binding")

    decision_input = manifest(run, "input", "decision")
    decision_output = manifest(run, "output", "decision")
    run_anchor = (
        str(run["source_run_id"]),
        str(run["source_run_checksum"]),
        int(run["source_publication_sequence"]),
        str(run["snapshot_id"]),
        str(run["snapshot_hash"]),
    )
    if run_anchor != source_anchor:
        fail("decision_source_binding")
    for value in (decision_input, decision_output):
        if (
            value.get("source_run_id") != source_anchor[0]
            or value.get("source_run_checksum") != source_anchor[1]
            or value.get("source_publication_sequence") != source_anchor[2]
            or value.get("snapshot_id") != source_anchor[3]
            or value.get("snapshot_hash") != source_anchor[4]
        ):
            fail("decision_manifest_binding")
    recommendations = decision_output.get("recommendations")
    core = {
        key: decision_output.get(key)
        for key in (
            "schema_version", "run_id", "source_run_id", "source_run_checksum",
            "source_publication_sequence", "snapshot_id", "snapshot_hash", "policy_id",
            "policy_version", "input_manifest_checksum", "recommendations",
        )
    }
    if (
        decision_input.get("registry_id") != str(run["registry_id"])
        or decision_input.get("policy_id") != str(run["policy_id"])
        or decision_input.get("policy_version") != str(run["policy_version"])
        or decision_output.get("run_id") != str(run["run_id"])
        or decision_output.get("input_manifest_checksum") != str(run["input_manifest_checksum"])
        or checksum(core) != str(run["run_checksum"])
        or decision_output.get("run_checksum") != str(run["run_checksum"])
        or not isinstance(recommendations, list)
    ):
        fail("decision_run_binding")

    try:
        rec_payload = json.loads(str(rec["payload_json"]))
    except (TypeError, json.JSONDecodeError):
        fail("recommendation_manifest")
    if (
        not isinstance(rec_payload, dict)
        or checksum(rec_payload) != str(rec["payload_checksum"])
        or str(rec["source_run_id"]) != source_anchor[0]
        or str(rec["source_run_checksum"]) != source_anchor[1]
        or str(rec["snapshot_id"]) != source_anchor[3]
        or str(rec["snapshot_hash"]) != source_anchor[4]
        or not any(
            isinstance(item, dict)
            and item.get("recommendation_id") == recommendation_id
            and item.get("payload_checksum") == str(rec["payload_checksum"])
            for item in recommendations
        )
    ):
        fail("recommendation_source_binding")

    expected_support = rec_payload.get("support")
    support_rows = con.execute(
        "SELECT * FROM decision_support_refs WHERE recommendation_id=? ORDER BY support_id",
        (recommendation_id,),
    ).fetchall()
    if not isinstance(expected_support, list) or len(support_rows) != len(expected_support):
        fail("support_manifest")
    actual_support: list[dict[str, Any]] = []
    for support in support_rows:
        try:
            payload = json.loads(str(support["payload_json"]))
        except (TypeError, json.JSONDecodeError):
            fail("support_manifest")
        if (
            not isinstance(payload, dict)
            or checksum(payload) != str(support["payload_checksum"])
            or str(support["source_run_id"]) != source_anchor[0]
            or str(support["source_run_checksum"]) != source_anchor[1]
            or int(support["source_publication_sequence"]) != source_anchor[2]
            or str(support["snapshot_id"]) != source_anchor[3]
            or str(support["snapshot_hash"]) != source_anchor[4]
            or payload.get("source_run_id") != source_anchor[0]
            or payload.get("source_run_checksum") != source_anchor[1]
            or payload.get("source_publication_sequence") != source_anchor[2]
            or payload.get("snapshot_id") != source_anchor[3]
            or payload.get("snapshot_hash") != source_anchor[4]
        ):
            fail("support_source_binding")
        actual_support.append(payload)
    if sorted(map(canonical_json, actual_support)) != sorted(map(canonical_json, expected_support)):
        fail("support_manifest")


def _typed_payload(con: sqlite3.Connection, event_type: str, record_id: str) -> dict[str, Any]:
    table, id_column = {
        "confirmation": ("decision_confirmations", "confirmation_id"),
        "action": ("decision_actions", "action_id"),
        "outcome": ("decision_outcomes", "outcome_id"),
        "assessment": ("decision_effectiveness", "assessment_id"),
    }[event_type]
    row = con.execute(f"SELECT payload_json,payload_checksum FROM {table} WHERE {id_column}=?", (record_id,)).fetchone()
    if row is None:
        raise DecisionStateError("typed_record_missing", record_id)
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError as exc:
        raise DecisionStateError("typed_record_payload_invalid", record_id) from exc
    if checksum(payload) != str(row["payload_checksum"]):
        raise DecisionStateError("typed_record_checksum_mismatch", record_id)
    return payload


def _confirmation_transition(current: str, action_state: str | None, decision: str) -> str:
    legal = (
        (current == "proposed" and decision in {"accept", "reject", "defer"})
        or (current == "deferred" and decision in {"accept", "reject", "defer"})
        or (current == "accepted" and action_state is None and decision == "revoke_before_action")
    )
    if not legal:
        raise DecisionStateError("illegal_confirmation_transition", f"{current}:{decision}")
    return {
        "accept": "accepted", "reject": "rejected", "defer": "deferred",
        "revoke_before_action": "revoked",
    }[decision]


def _action_transition(confirmation_state: str, current: str | None, next_state: str) -> str:
    legal = False
    if confirmation_state == "accepted":
        if current is None:
            legal = next_state in {"planned", "not_taken"}
        elif current == "planned":
            legal = next_state in {"started", "abandoned", "not_taken"}
        elif current == "started":
            legal = next_state in {"completed", "abandoned"}
    if not legal:
        raise DecisionStateError("illegal_action_transition", f"{current}:{next_state}")
    return next_state


def _project(con: sqlite3.Connection, recommendation_id: str) -> DecisionState:
    recommendation, _, run = _load_recommendation(con, recommendation_id)
    rows = con.execute(
        "SELECT * FROM decision_events WHERE recommendation_id=? ORDER BY sequence", (recommendation_id,)
    ).fetchall()
    if not rows:
        raise DecisionStateError("genesis_missing", recommendation_id)
    events: list[DecisionEvent] = []
    confirmation_state = "proposed"
    action_state: str | None = None
    prior_checksum = GENESIS_SENTINEL
    for expected_sequence, row in enumerate(rows, start=1):
        sequence = int(row["sequence"])
        if sequence != expected_sequence:
            raise DecisionStateError("event_sequence_invalid", f"expected={expected_sequence},actual={sequence}")
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError as exc:
            raise DecisionStateError("event_payload_invalid", str(row["event_id"])) from exc
        payload_checksum = str(row["payload_checksum"])
        if checksum(payload) != payload_checksum:
            raise DecisionStateError("event_checksum_mismatch", str(row["event_id"]))
        event_type = str(row["event_type"])
        previous = str(row["previous_event_checksum"])
        if previous != prior_checksum:
            raise DecisionStateError("event_chain_mismatch", str(row["event_id"]))
        if sequence == 1:
            required = {
                "event_type": "recommendation_published",
                "sequence": 1,
                "recommendation_id": recommendation_id,
                "recommendation_checksum": str(recommendation["payload_checksum"]),
                "decision_run_id": str(run["run_id"]),
                "decision_run_checksum": str(run["run_checksum"]),
                "source_run_id": str(run["source_run_id"]),
                "source_run_checksum": str(run["source_run_checksum"]),
                "source_publication_sequence": int(run["source_publication_sequence"]),
                "snapshot_id": str(run["snapshot_id"]),
                "snapshot_hash": str(run["snapshot_hash"]),
                "previous_event_checksum": GENESIS_SENTINEL,
            }
            if event_type != "recommendation_published" or any(payload.get(key) != value for key, value in required.items()):
                raise DecisionStateError("genesis_binding_mismatch", recommendation_id)
            if str(row["typed_record_id"]) != recommendation_id:
                raise DecisionStateError("genesis_binding_mismatch", recommendation_id)
        else:
            if event_type not in {"confirmation", "action", "outcome", "assessment"}:
                raise DecisionStateError("unsupported_event_type", event_type)
            typed = _typed_payload(con, event_type, str(row["typed_record_id"]))
            if payload.get("typed_record_checksum") != checksum(typed):
                raise DecisionStateError("event_typed_record_mismatch", str(row["event_id"]))
            expected_event = {
                "event_type": event_type,
                "sequence": sequence,
                "recommendation_id": recommendation_id,
                "recommendation_checksum": str(recommendation["payload_checksum"]),
                "typed_record_id": str(row["typed_record_id"]),
                "previous_event_checksum": previous,
            }
            if any(payload.get(key) != value for key, value in expected_event.items()):
                raise DecisionStateError("event_binding_mismatch", str(row["event_id"]))
            if (
                typed.get("recommendation_id") != recommendation_id
                or typed.get("recommendation_checksum") != str(recommendation["payload_checksum"])
                or typed.get("expected_sequence") != sequence - 1
            ):
                raise DecisionStateError("typed_record_binding_mismatch", str(row["typed_record_id"]))
            if event_type == "confirmation":
                if typed.get("actor_class") != "user" or len(str(typed.get("actor_identity_hash", ""))) != 64:
                    raise DecisionStateError("typed_record_binding_mismatch", str(row["typed_record_id"]))
                decision = str(typed["decision"])
                if typed.get("cognitive_type") != "user_confirmation" or decision not in _CONFIRMATIONS:
                    raise DecisionStateError("typed_record_binding_mismatch", str(row["typed_record_id"]))
                if _parse_time(str(typed.get("occurred_at", "")), "occurred_at") > _parse_time(
                    str(recommendation["expires_at"]), "expires_at"
                ):
                    raise DecisionStateError("recommendation_expired", recommendation_id)
                confirmation_state = _confirmation_transition(confirmation_state, action_state, decision)
            elif event_type == "action":
                if typed.get("actor_class") != "user" or len(str(typed.get("actor_identity_hash", ""))) != 64:
                    raise DecisionStateError("typed_record_binding_mismatch", str(row["typed_record_id"]))
                next_action = str(typed["action_state"])
                if typed.get("record_type") != "action_attestation" or next_action not in _ACTION_STATES:
                    raise DecisionStateError("typed_record_binding_mismatch", str(row["typed_record_id"]))
                action_state = _action_transition(confirmation_state, action_state, next_action)
            elif event_type == "outcome":
                if (
                    typed.get("record_type") != "outcome_observation"
                    or typed.get("cognitive_type") != "observation"
                    or typed.get("causal_claim") is not False
                    or typed.get("actor_class") != "user"
                    or len(str(typed.get("actor_identity_hash", ""))) != 64
                ):
                    raise DecisionStateError("typed_record_binding_mismatch", str(row["typed_record_id"]))
            else:
                if (
                    typed.get("record_type") != "effectiveness_assessment"
                    or typed.get("cognitive_type") != "inference"
                    or typed.get("causal_claim") is not False
                ):
                    raise DecisionStateError("typed_record_binding_mismatch", str(row["typed_record_id"]))
        events.append(DecisionEvent(
            event_id=str(row["event_id"]), recommendation_id=recommendation_id,
            sequence=sequence, event_type=event_type, typed_record_id=str(row["typed_record_id"]),
            previous_event_checksum=previous, payload=payload, payload_checksum=payload_checksum,
        ))
        prior_checksum = payload_checksum
    return DecisionState(
        recommendation_id=recommendation_id,
        recommendation_checksum=str(recommendation["payload_checksum"]),
        confirmation_state=confirmation_state,
        action_state=action_state,
        events=tuple(events),
    )


def project_history(db_path: Path, recommendation_id: str) -> DecisionState:
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        return _project(con, recommendation_id)
    finally:
        con.close()


def _existing_receipt(
    con: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    recommendation_id: str,
    actor_identity_hash: str,
    idempotency_key: str,
    payload: Mapping[str, Any],
) -> DecisionReceipt | None:
    row = con.execute(
        f"SELECT {id_column},payload_json,payload_checksum FROM {table} "
        "WHERE recommendation_id=? AND actor_identity_hash=? AND idempotency_key=?",
        (recommendation_id, actor_identity_hash, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if canonical_json(json.loads(str(row["payload_json"]))) != canonical_json(payload) or str(row["payload_checksum"]) != checksum(payload):
        raise DecisionStateError("idempotency_conflict", idempotency_key)
    event = con.execute(
        "SELECT event_id,sequence,payload_checksum FROM decision_events WHERE typed_record_id=?",
        (row[id_column],),
    ).fetchone()
    if event is None:
        raise DecisionStateError("typed_event_missing", str(row[id_column]))
    return DecisionReceipt(str(row[id_column]), str(event["event_id"]), recommendation_id,
                           int(event["sequence"]), str(row["payload_checksum"]))


def _append_event(
    con: sqlite3.Connection,
    *,
    recommendation_id: str,
    recommendation_checksum: str,
    event_type: str,
    typed_record_id: str,
    typed_payload_checksum: str,
    sequence: int,
    previous_event_checksum: str,
    occurred_at: str,
) -> tuple[str, str]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type,
        "sequence": sequence,
        "recommendation_id": recommendation_id,
        "recommendation_checksum": recommendation_checksum,
        "typed_record_id": typed_record_id,
        "typed_record_checksum": typed_payload_checksum,
        "previous_event_checksum": previous_event_checksum,
        "occurred_at": occurred_at,
    }
    event_id = f"dev_{checksum(payload)[:24]}"
    payload_checksum = checksum(payload)
    con.execute(
        "INSERT INTO decision_events VALUES (?,?,?,?,?,?,?,?,?)",
        (event_id, recommendation_id, sequence, event_type, typed_record_id,
         previous_event_checksum, canonical_json(payload), payload_checksum, occurred_at),
    )
    return event_id, payload_checksum


def record_confirmation(
    db_path: Path,
    *,
    recommendation_id: str,
    recommendation_checksum: str,
    decision: str,
    actor_class: str,
    actor_identity_hash: str,
    reason_code: str,
    expected_sequence: int,
    idempotency_key: str,
    occurred_at: str,
    inject_failure_at: str | None = None,
) -> DecisionReceipt:
    if decision not in _CONFIRMATIONS:
        raise DecisionStateError("invalid_confirmation", decision)
    _validate_actor(actor_class, actor_identity_hash)
    occurred = _parse_time(occurred_at, "occurred_at")
    if not reason_code or not idempotency_key:
        raise DecisionStateError("confirmation_metadata_required")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "cognitive_type": "user_confirmation",
        "recommendation_id": recommendation_id,
        "recommendation_checksum": recommendation_checksum,
        "decision": decision,
        "actor_class": actor_class,
        "actor_identity_hash": actor_identity_hash,
        "reason_code": reason_code,
        "expected_sequence": expected_sequence,
        "idempotency_key": idempotency_key,
        "occurred_at": occurred_at,
    }
    con = connect_rw(Path(db_path), timeout=60)
    con.row_factory = sqlite3.Row
    try:
        assert_foreign_key_integrity(con)
        con.execute("BEGIN IMMEDIATE")
        _validate_source_binding(con, recommendation_id)
        state = _project(con, recommendation_id)
        rec, _, _ = _load_recommendation(con, recommendation_id)
        if str(rec["payload_checksum"]) != recommendation_checksum:
            raise DecisionStateError("recommendation_checksum_mismatch", recommendation_id)
        if occurred > _parse_time(str(rec["expires_at"]), "expires_at"):
            raise DecisionStateError("recommendation_expired", recommendation_id)
        existing = _existing_receipt(
            con, table="decision_confirmations", id_column="confirmation_id",
            recommendation_id=recommendation_id, actor_identity_hash=actor_identity_hash,
            idempotency_key=idempotency_key, payload=payload,
        )
        if existing is not None:
            con.commit()
            return existing
        if expected_sequence != state.events[-1].sequence:
            raise DecisionStateError("stale_expected_sequence", str(expected_sequence))
        _confirmation_transition(state.confirmation_state, state.action_state, decision)
        confirmation_id = f"dcf_{checksum(payload)[:24]}"
        payload_checksum = checksum(payload)
        con.execute(
            "INSERT INTO decision_confirmations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (confirmation_id, recommendation_id, recommendation_checksum, decision,
             actor_class, actor_identity_hash, reason_code, expected_sequence,
             idempotency_key, canonical_json(payload), payload_checksum, occurred_at),
        )
        if inject_failure_at == "after_typed_record":
            raise RuntimeError("injected decision state failure after typed record")
        event_id, _ = _append_event(
            con, recommendation_id=recommendation_id,
            recommendation_checksum=recommendation_checksum, event_type="confirmation",
            typed_record_id=confirmation_id, typed_payload_checksum=payload_checksum,
            sequence=expected_sequence + 1,
            previous_event_checksum=state.events[-1].payload_checksum,
            occurred_at=occurred_at,
        )
        if inject_failure_at == "after_event":
            raise RuntimeError("injected decision state failure after event")
        con.commit()
        return DecisionReceipt(confirmation_id, event_id, recommendation_id,
                               expected_sequence + 1, payload_checksum)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def record_action(
    db_path: Path,
    *,
    recommendation_id: str,
    recommendation_checksum: str,
    action_state: str,
    source_class: str,
    actor_class: str,
    actor_identity_hash: str,
    reason_code: str,
    expected_sequence: int,
    idempotency_key: str,
    occurred_at: str,
    external_ref_checksum: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    inject_failure_at: str | None = None,
) -> DecisionReceipt:
    if action_state not in _ACTION_STATES:
        raise DecisionStateError("invalid_action_state", action_state)
    if source_class not in {"user_attested", "user_external_ref"}:
        raise DecisionStateError("invalid_action_source", source_class)
    _validate_actor(actor_class, actor_identity_hash)
    _parse_time(occurred_at, "occurred_at")
    if not reason_code or not idempotency_key:
        raise DecisionStateError("action_metadata_required")
    if external_ref_checksum is not None and len(external_ref_checksum) != 64:
        raise DecisionStateError("external_ref_checksum_invalid")
    if source_class == "user_external_ref" and external_ref_checksum is None:
        raise DecisionStateError("external_ref_checksum_required")
    metadata = dict(metadata or {})
    _reject_action_metadata(metadata)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "action_attestation",
        "recommendation_id": recommendation_id,
        "recommendation_checksum": recommendation_checksum,
        "action_state": action_state,
        "source_class": source_class,
        "actor_class": actor_class,
        "actor_identity_hash": actor_identity_hash,
        "reason_code": reason_code,
        "expected_sequence": expected_sequence,
        "idempotency_key": idempotency_key,
        "external_ref_checksum": external_ref_checksum,
        "metadata": metadata,
        "occurred_at": occurred_at,
    }
    con = connect_rw(Path(db_path), timeout=60)
    con.row_factory = sqlite3.Row
    try:
        assert_foreign_key_integrity(con)
        con.execute("BEGIN IMMEDIATE")
        _validate_source_binding(con, recommendation_id)
        state = _project(con, recommendation_id)
        rec, _, _ = _load_recommendation(con, recommendation_id)
        if str(rec["payload_checksum"]) != recommendation_checksum:
            raise DecisionStateError("recommendation_checksum_mismatch", recommendation_id)
        existing = _existing_receipt(
            con, table="decision_actions", id_column="action_id",
            recommendation_id=recommendation_id, actor_identity_hash=actor_identity_hash,
            idempotency_key=idempotency_key, payload=payload,
        )
        if existing is not None:
            con.commit()
            return existing
        if expected_sequence != state.events[-1].sequence:
            raise DecisionStateError("stale_expected_sequence", str(expected_sequence))
        _action_transition(state.confirmation_state, state.action_state, action_state)
        action_id = f"dac_{checksum(payload)[:24]}"
        payload_checksum = checksum(payload)
        con.execute(
            "INSERT INTO decision_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (action_id, recommendation_id, recommendation_checksum, action_state,
             source_class, actor_class, actor_identity_hash, reason_code,
             expected_sequence, idempotency_key, external_ref_checksum,
             canonical_json(payload), payload_checksum, occurred_at),
        )
        if inject_failure_at == "after_typed_record":
            raise RuntimeError("injected decision state failure after typed record")
        event_id, _ = _append_event(
            con, recommendation_id=recommendation_id,
            recommendation_checksum=recommendation_checksum, event_type="action",
            typed_record_id=action_id, typed_payload_checksum=payload_checksum,
            sequence=expected_sequence + 1,
            previous_event_checksum=state.events[-1].payload_checksum,
            occurred_at=occurred_at,
        )
        if inject_failure_at == "after_event":
            raise RuntimeError("injected decision state failure after event")
        con.commit()
        return DecisionReceipt(action_id, event_id, recommendation_id,
                               expected_sequence + 1, payload_checksum)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def record_outcome(
    db_path: Path,
    *,
    recommendation_id: str,
    recommendation_checksum: str,
    action_id: str,
    action_checksum: str,
    source_class: str,
    actor_class: str,
    actor_identity_hash: str,
    measurement_definition: str,
    metric: str,
    baseline_value: float | None,
    target_value: float | None,
    observed_value: float | None,
    unit: str,
    direction: str,
    window_start: str,
    window_end: str,
    adherence_status: str,
    evidence_refs: tuple[Mapping[str, Any], ...],
    confidence: float,
    uncertainty: tuple[str, ...],
    confounders: tuple[str, ...],
    concurrent_actions: tuple[str, ...],
    expected_sequence: int,
    idempotency_key: str,
    occurred_at: str,
    metadata: Mapping[str, Any] | None = None,
    inject_failure_at: str | None = None,
) -> DecisionReceipt:
    if source_class not in _OUTCOME_SOURCES:
        raise DecisionStateError("invalid_outcome_source", source_class)
    _validate_actor(actor_class, actor_identity_hash)
    if direction not in _OUTCOME_DIRECTIONS or adherence_status not in _ADHERENCE:
        raise DecisionStateError("invalid_measurement_definition")
    if not all(value.strip() for value in (measurement_definition, metric, unit, idempotency_key)):
        raise DecisionStateError("outcome_metadata_required")
    if not 0.0 <= confidence <= 1.0:
        raise DecisionStateError("invalid_outcome_confidence")
    start = _parse_time(window_start, "window_start")
    end = _parse_time(window_end, "window_end")
    _parse_time(occurred_at, "occurred_at")
    if end <= start:
        raise DecisionStateError("invalid_outcome_window")
    for value in (baseline_value, target_value, observed_value):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise DecisionStateError("invalid_measurement_value")
    metadata = dict(metadata or {})
    evidence_refs = tuple(dict(ref) if isinstance(ref, Mapping) else ref for ref in evidence_refs)
    validate_outcome_metadata(metadata, evidence_refs)  # type: ignore[arg-type]
    if source_class == "evidence_measured" and not evidence_refs:
        raise DecisionStateError("evidence_required")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "outcome_observation",
        "cognitive_type": "observation",
        "causal_claim": False,
        "recommendation_id": recommendation_id,
        "recommendation_checksum": recommendation_checksum,
        "action_id": action_id,
        "action_checksum": action_checksum,
        "source_class": source_class,
        "actor_class": actor_class,
        "actor_identity_hash": actor_identity_hash,
        "measurement_definition": measurement_definition,
        "metric": metric,
        "baseline_value": baseline_value,
        "target_value": target_value,
        "observed_value": observed_value,
        "unit": unit,
        "direction": direction,
        "window_start": window_start,
        "window_end": window_end,
        "adherence_status": adherence_status,
        "evidence_refs": evidence_refs,
        "confidence": confidence,
        "uncertainty": tuple(sorted(set(uncertainty))),
        "confounders": tuple(sorted(set(confounders))),
        "concurrent_actions": tuple(sorted(set(concurrent_actions))),
        "expected_sequence": expected_sequence,
        "idempotency_key": idempotency_key,
        "metadata": metadata,
        "occurred_at": occurred_at,
    }
    con = connect_rw(Path(db_path), timeout=60)
    con.row_factory = sqlite3.Row
    try:
        assert_foreign_key_integrity(con)
        con.execute("BEGIN IMMEDIATE")
        _validate_source_binding(con, recommendation_id)
        state = _project(con, recommendation_id)
        rec, _, _ = _load_recommendation(con, recommendation_id)
        if str(rec["payload_checksum"]) != recommendation_checksum:
            raise DecisionStateError("recommendation_checksum_mismatch", recommendation_id)
        existing = _existing_receipt(
            con, table="decision_outcomes", id_column="outcome_id",
            recommendation_id=recommendation_id, actor_identity_hash=actor_identity_hash,
            idempotency_key=idempotency_key, payload=payload,
        )
        if existing is not None:
            con.commit()
            return existing
        if expected_sequence != state.events[-1].sequence:
            raise DecisionStateError("stale_expected_sequence", str(expected_sequence))
        action = con.execute(
            "SELECT payload_json,payload_checksum,action_state FROM decision_actions "
            "WHERE action_id=? AND recommendation_id=?", (action_id, recommendation_id),
        ).fetchone()
        if action is None:
            raise DecisionStateError("action_missing", action_id)
        if str(action["payload_checksum"]) != action_checksum:
            raise DecisionStateError("action_checksum_mismatch", action_id)
        if str(action["action_state"]) not in {"completed", "abandoned", "not_taken"}:
            raise DecisionStateError("action_not_terminal", action_id)
        rec_snapshot = (str(rec["snapshot_id"]), str(rec["snapshot_hash"]))
        for ref in evidence_refs:
            if (str(ref["snapshot_id"]), str(ref["snapshot_hash"])) != rec_snapshot:
                raise DecisionStateError("cross_snapshot_evidence")
            support = con.execute(
                "SELECT 1 FROM decision_support_refs WHERE recommendation_id=? AND cognitive_type=? "
                "AND authority_id=? AND record_id=? AND record_checksum=? AND source_run_id=? "
                "AND snapshot_id=? AND snapshot_hash=?",
                (recommendation_id, ref["cognitive_type"], ref["authority_id"], ref["record_id"],
                 ref["record_checksum"], ref["source_run_id"], ref["snapshot_id"], ref["snapshot_hash"]),
            ).fetchone()
            if support is None:
                raise DecisionStateError("outcome_evidence_unbound", str(ref["record_id"]))
        outcome_id = f"doc_{checksum(payload)[:24]}"
        payload_checksum = checksum(payload)
        con.execute(
            "INSERT INTO decision_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (outcome_id, recommendation_id, recommendation_checksum, action_id, action_checksum,
             source_class, actor_class, actor_identity_hash, metric, unit, window_start, window_end,
             adherence_status, confidence, canonical_json(payload["uncertainty"]), expected_sequence,
             idempotency_key, canonical_json(payload), payload_checksum, occurred_at),
        )
        if inject_failure_at == "after_typed_record":
            raise RuntimeError("injected decision state failure after typed record")
        event_id, _ = _append_event(
            con, recommendation_id=recommendation_id,
            recommendation_checksum=recommendation_checksum, event_type="outcome",
            typed_record_id=outcome_id, typed_payload_checksum=payload_checksum,
            sequence=expected_sequence + 1,
            previous_event_checksum=state.events[-1].payload_checksum,
            occurred_at=occurred_at,
        )
        if inject_failure_at == "after_event":
            raise RuntimeError("injected decision state failure after event")
        con.commit()
        return DecisionReceipt(outcome_id, event_id, recommendation_id,
                               expected_sequence + 1, payload_checksum)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def record_assessment(
    db_path: Path,
    *,
    assessment: Any,
    expected_sequence: int,
    idempotency_key: str,
    occurred_at: str,
    inject_failure_at: str | None = None,
) -> DecisionReceipt:
    if (
        not isinstance(assessment, EffectivenessAssessment)
        or
        getattr(assessment, "cognitive_type", None) != "inference"
        or getattr(assessment, "causal_claim", None) is not False
        or getattr(assessment, "verdict", None) not in {"effective", "ineffective", "mixed", "inconclusive"}
    ):
        raise DecisionStateError("invalid_effectiveness_assessment")
    if not idempotency_key:
        raise DecisionStateError("assessment_metadata_required")
    _parse_time(occurred_at, "occurred_at")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "effectiveness_assessment",
        "cognitive_type": "inference",
        "causal_claim": False,
        "recommendation_id": assessment.recommendation_id,
        "recommendation_checksum": assessment.recommendation_checksum,
        "outcome_id": assessment.outcome_id,
        "outcome_checksum": assessment.outcome_checksum,
        "verdict": assessment.verdict,
        "rule_id": assessment.rule_id,
        "rule_version": assessment.rule_version,
        "input_checksums": tuple(assessment.input_checksums),
        "limitations": tuple(assessment.limitations),
        "confidence": assessment.confidence,
        "uncertainty": tuple(assessment.uncertainty),
        "expected_sequence": expected_sequence,
        "idempotency_key": idempotency_key,
        "occurred_at": occurred_at,
    }
    con = connect_rw(Path(db_path), timeout=60)
    con.row_factory = sqlite3.Row
    try:
        assert_foreign_key_integrity(con)
        con.execute("BEGIN IMMEDIATE")
        _validate_source_binding(con, assessment.recommendation_id)
        state = _project(con, assessment.recommendation_id)
        rec, _, _ = _load_recommendation(con, assessment.recommendation_id)
        outcome = con.execute(
            "SELECT recommendation_id,action_id,action_checksum,payload_json,payload_checksum "
            "FROM decision_outcomes WHERE outcome_id=?",
            (assessment.outcome_id,),
        ).fetchone()
        if outcome is None:
            raise DecisionStateError("outcome_missing", assessment.outcome_id)
        try:
            outcome_payload = json.loads(str(outcome["payload_json"]))
        except json.JSONDecodeError as exc:
            raise DecisionStateError("outcome_payload_invalid", assessment.outcome_id) from exc
        if checksum(outcome_payload) != str(outcome["payload_checksum"]):
            raise DecisionStateError("outcome_checksum_mismatch", assessment.outcome_id)
        if (
            str(outcome["recommendation_id"]) != assessment.recommendation_id
            or str(rec["payload_checksum"]) != assessment.recommendation_checksum
            or str(outcome["payload_checksum"]) != assessment.outcome_checksum
            or tuple(assessment.input_checksums) != (str(outcome["action_checksum"]), str(outcome["payload_checksum"]))
        ):
            raise DecisionStateError("assessment_input_mismatch", assessment.outcome_id)
        action = con.execute(
            "SELECT recommendation_id,action_state,payload_json,payload_checksum "
            "FROM decision_actions WHERE action_id=?",
            (str(outcome["action_id"]),),
        ).fetchone()
        if action is None:
            raise DecisionStateError("action_missing", str(outcome["action_id"]))
        try:
            action_payload = json.loads(str(action["payload_json"]))
        except json.JSONDecodeError as exc:
            raise DecisionStateError("action_payload_invalid", str(outcome["action_id"])) from exc
        if checksum(action_payload) != str(action["payload_checksum"]):
            raise DecisionStateError("action_checksum_mismatch", str(outcome["action_id"]))
        if (
            str(action["recommendation_id"]) != assessment.recommendation_id
            or str(action["payload_checksum"]) != str(outcome["action_checksum"])
        ):
            raise DecisionStateError("assessment_input_mismatch", assessment.outcome_id)
        try:
            rule = registered_effectiveness_rule(assessment.rule_id, assessment.rule_version)
        except ValueError as exc:
            raise DecisionStateError("unknown_effectiveness_rule", f"{assessment.rule_id}:{assessment.rule_version}") from exc
        hydrated_outcome = outcome_from_payload(
            assessment.outcome_id, outcome_payload, str(outcome["payload_checksum"])
        )
        derived = assess_outcome(hydrated_outcome, rule, action_state=str(action["action_state"]))
        if assessment != derived:
            raise DecisionStateError("assessment_derivation_mismatch", assessment.outcome_id)
        existing = con.execute(
            "SELECT assessment_id,payload_json,payload_checksum FROM decision_effectiveness "
            "WHERE recommendation_id=? AND rule_id=? AND rule_version=? AND outcome_id=? AND idempotency_key=?",
            (assessment.recommendation_id, assessment.rule_id, assessment.rule_version,
             assessment.outcome_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if canonical_json(json.loads(str(existing["payload_json"]))) != canonical_json(payload) or str(existing["payload_checksum"]) != checksum(payload):
                raise DecisionStateError("idempotency_conflict", idempotency_key)
            event = con.execute(
                "SELECT event_id,sequence FROM decision_events WHERE typed_record_id=?", (existing["assessment_id"],)
            ).fetchone()
            if event is None:
                raise DecisionStateError("typed_event_missing", str(existing["assessment_id"]))
            con.commit()
            return DecisionReceipt(str(existing["assessment_id"]), str(event["event_id"]),
                                   assessment.recommendation_id, int(event["sequence"]),
                                   str(existing["payload_checksum"]))
        if expected_sequence != state.events[-1].sequence:
            raise DecisionStateError("stale_expected_sequence", str(expected_sequence))
        assessment_id = assessment.assessment_id
        payload_checksum = checksum(payload)
        con.execute(
            "INSERT INTO decision_effectiveness VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (assessment_id, assessment.recommendation_id, assessment.rule_id, assessment.rule_version,
             assessment.verdict, 0, assessment.outcome_id, assessment.outcome_checksum,
             expected_sequence, idempotency_key, canonical_json(payload), payload_checksum, occurred_at),
        )
        if inject_failure_at == "after_typed_record":
            raise RuntimeError("injected decision state failure after typed record")
        event_id, _ = _append_event(
            con, recommendation_id=assessment.recommendation_id,
            recommendation_checksum=str(rec["payload_checksum"]), event_type="assessment",
            typed_record_id=assessment_id, typed_payload_checksum=payload_checksum,
            sequence=expected_sequence + 1,
            previous_event_checksum=state.events[-1].payload_checksum,
            occurred_at=occurred_at,
        )
        if inject_failure_at == "after_event":
            raise RuntimeError("injected decision state failure after event")
        con.commit()
        return DecisionReceipt(assessment_id, event_id, assessment.recommendation_id,
                               expected_sequence + 1, payload_checksum)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


__all__ = [
    "DecisionStateError", "project_history", "record_action", "record_confirmation",
    "record_assessment", "record_outcome", "validate_outcome_metadata",
]
