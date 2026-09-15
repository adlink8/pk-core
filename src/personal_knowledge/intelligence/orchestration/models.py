"""Immutable public models for guarded decision orchestration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from personal_knowledge.intelligence.analysis.schema import canonical_json, checksum, stable_id


SCHEMA_VERSION = "decision_orchestration_v1"
REGISTRY_ID = "a.decision_orchestration"


def _transport_stable_json(value: Any) -> Any:
    """Normalize JSON numbers whose spelling changes across Python and JS."""
    if isinstance(value, Mapping):
        return {str(key): _transport_stable_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_transport_stable_json(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


class OrchestrationError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Preview:
    session_id: str
    operation: str
    actor_identity_hash: str
    expected_sequence: int
    payload: Mapping[str, Any]
    issued_at: str
    preview_checksum: str

    def core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "operation": self.operation,
            "actor_identity_hash": self.actor_identity_hash,
            "expected_sequence": self.expected_sequence,
            "payload": dict(self.payload),
            "issued_at": self.issued_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.core(), "preview_checksum": self.preview_checksum}

    @classmethod
    def build(
        cls, *, session_id: str, operation: str, actor_identity_hash: str,
        expected_sequence: int, payload: Mapping[str, Any], issued_at: str,
    ) -> "Preview":
        draft = cls(
            session_id, operation, actor_identity_hash, expected_sequence,
            _transport_stable_json(payload), issued_at, "",
        )
        return cls(**{**draft.__dict__, "preview_checksum": checksum(draft.core())})

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Preview":
        try:
            item = cls(
                session_id=str(value["session_id"]), operation=str(value["operation"]),
                actor_identity_hash=str(value["actor_identity_hash"]),
                expected_sequence=int(value["expected_sequence"]),
                payload=_transport_stable_json(value["payload"]), issued_at=str(value["issued_at"]),
                preview_checksum=str(value["preview_checksum"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OrchestrationError("preview_invalid") from exc
        if checksum(item.core()) != item.preview_checksum:
            raise OrchestrationError("preview_checksum_mismatch")
        return item


@dataclass(frozen=True)
class OperationResult:
    session_id: str
    operation: str
    state: str
    sequence: int
    event_id: str
    event_checksum: str
    replayed: bool
    references: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id, "operation": self.operation,
            "state": self.state, "sequence": self.sequence,
            "event_id": self.event_id, "event_checksum": self.event_checksum,
            "replayed": self.replayed, "references": dict(self.references),
        }


def event_id(event_checksum: str) -> str:
    return stable_id("ore", event_checksum)


__all__ = [
    "OrchestrationError", "OperationResult", "Preview", "REGISTRY_ID",
    "SCHEMA_VERSION", "canonical_json", "checksum", "event_id", "stable_id",
]
