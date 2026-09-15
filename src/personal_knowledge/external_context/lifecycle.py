"""Append-only lifecycle events and deterministic lifecycle projection."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from .schema import canonical_json, checksum, stable_id


EVENT_TO_LIFECYCLE = {
    "created": "current",
    "staled": "stale",
    "superseded": "superseded",
    "conflicted": "conflict",
    "invalidated": "invalid",
}
TERMINAL_LIFECYCLES = frozenset({"superseded", "invalid"})


class ExternalLifecycleError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class LifecycleProjection:
    fact_id: str
    lifecycle: str
    event_count: int
    head_checksum: str
    occurred_at: str


def _rows(connection: Any, fact_id: str) -> list[tuple[Any, ...]]:
    return list(connection.execute(
        "SELECT sequence,event_type,previous_event_checksum,payload_json,"
        "payload_checksum,occurred_at FROM external_lifecycle_events "
        "WHERE fact_id=? ORDER BY sequence", (fact_id,),
    ))


def project_fact_lifecycle(connection: Any, fact_id: str) -> LifecycleProjection:
    rows = _rows(connection, fact_id)
    if not rows:
        raise ExternalLifecycleError("lifecycle_missing", fact_id)
    previous = "GENESIS"
    lifecycle = "current"
    last_time = ""
    for expected, row in enumerate(rows, start=1):
        sequence, event_type, previous_checksum, payload_json, payload_checksum, occurred_at = row
        if int(sequence) != expected:
            raise ExternalLifecycleError("lifecycle_sequence_gap", fact_id)
        if str(previous_checksum) != previous:
            raise ExternalLifecycleError("lifecycle_chain_broken", fact_id)
        try:
            payload = json.loads(str(payload_json))
        except json.JSONDecodeError as exc:
            raise ExternalLifecycleError("lifecycle_payload_invalid", fact_id) from exc
        expected_checksum = checksum({
            "fact_id": fact_id, "sequence": expected, "event_type": str(event_type),
            "previous_event_checksum": previous, "payload": payload,
            "occurred_at": str(occurred_at),
        })
        if str(payload_checksum) != expected_checksum:
            raise ExternalLifecycleError("lifecycle_checksum_mismatch", fact_id)
        if expected == 1 and event_type != "created":
            raise ExternalLifecycleError("lifecycle_genesis_not_created", fact_id)
        if expected > 1 and lifecycle in TERMINAL_LIFECYCLES:
            raise ExternalLifecycleError("lifecycle_terminal_transition", fact_id)
        lifecycle = EVENT_TO_LIFECYCLE[str(event_type)]
        previous = str(payload_checksum)
        last_time = str(occurred_at)
    return LifecycleProjection(fact_id, lifecycle, len(rows), previous, last_time)


def append_lifecycle_event(
    connection: Any,
    *,
    fact_id: str,
    event_type: str,
    occurred_at: str,
    payload: Mapping[str, Any] | None = None,
) -> LifecycleProjection:
    if event_type not in EVENT_TO_LIFECYCLE:
        raise ExternalLifecycleError("lifecycle_event_invalid", event_type)
    rows = _rows(connection, fact_id)
    sequence = len(rows) + 1
    if sequence == 1:
        if event_type != "created":
            raise ExternalLifecycleError("lifecycle_genesis_not_created", fact_id)
        previous = "GENESIS"
    else:
        current = project_fact_lifecycle(connection, fact_id)
        if current.lifecycle in TERMINAL_LIFECYCLES:
            raise ExternalLifecycleError("lifecycle_terminal_transition", fact_id)
        if event_type == "created":
            raise ExternalLifecycleError("lifecycle_duplicate_created", fact_id)
        previous = current.head_checksum
    event_payload = dict(payload or {})
    digest = checksum({
        "fact_id": fact_id, "sequence": sequence, "event_type": event_type,
        "previous_event_checksum": previous, "payload": event_payload,
        "occurred_at": occurred_at,
    })
    event_id = stable_id("ele", {"fact_id": fact_id, "sequence": sequence, "checksum": digest})
    connection.execute(
        "INSERT INTO external_lifecycle_events "
        "(event_id,fact_id,sequence,event_type,previous_event_checksum,payload_json,payload_checksum,occurred_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (event_id, fact_id, sequence, event_type, previous, canonical_json(event_payload), digest, occurred_at),
    )
    return project_fact_lifecycle(connection, fact_id)


__all__ = [
    "ExternalLifecycleError", "LifecycleProjection", "append_lifecycle_event",
    "project_fact_lifecycle",
]
