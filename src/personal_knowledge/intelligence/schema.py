"""Typed, canonical records for snapshot-bound personal-state analysis.

These records describe A-layer analysis only.  They deliberately contain typed
references and checksums rather than source bodies.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
from typing import Any, Mapping


SCHEMA_VERSION = "personal_state_run_v1"
ASSERTION_KINDS = frozenset({"goal", "constraint", "observation", "state"})
PROVENANCE_CLASSES = frozenset({"fact", "observation", "inference"})
ASSERTION_LIFECYCLES = frozenset({"current", "stale", "conflict", "resolved", "expired"})
EVIDENCE_TYPES = frozenset({"canonical_message", "knowledge_unit", "turn", "google_signal"})
PRIVACY_CLASSES = frozenset({"R1", "R2", "R3", "R4"})
RISK_SEVERITIES = frozenset({"low", "medium", "high"})


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value with stable ordering and no whitespace."""
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceReference:
    ref: str
    artifact_type: str
    serving_role: str
    artifact_version_id: str
    checksum: str = ""
    privacy_class: str = "R4"


@dataclass(frozen=True)
class StateAssertion:
    assertion_kind: str
    provenance_class: str
    subject: str
    domain: str
    scope: str
    predicate: str
    value: Any
    valid_from: str
    observed_at: str
    evidence: tuple[EvidenceReference, ...]
    confidence: float = 1.0
    valid_to: str | None = None
    uncertainty: str = ""
    lifecycle: str = "current"
    assertion_id: str = ""


@dataclass(frozen=True)
class SnapshotBinding:
    snapshot_id: str
    snapshot_hash: str
    members: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class ValidatedEvidence:
    ref: str
    artifact_type: str
    serving_role: str
    artifact_version_id: str
    evidence_checksum: str
    privacy_class: str


@dataclass(frozen=True)
class ValidatedAssertion:
    assertion_id: str
    assertion_kind: str
    provenance_class: str
    subject: str
    domain: str
    scope: str
    predicate: str
    value: Any
    valid_from: str
    valid_to: str | None
    observed_at: str
    confidence: float
    uncertainty: str
    lifecycle: str
    evidence: tuple[ValidatedEvidence, ...]
    payload_checksum: str


@dataclass(frozen=True)
class PersonalStateRun:
    run_id: str
    registry_id: str
    snapshot: SnapshotBinding
    producer_version: str
    input_manifest: Mapping[str, Any]
    input_manifest_checksum: str
    output_manifest: Mapping[str, Any]
    output_manifest_checksum: str
    assertions: tuple[ValidatedAssertion, ...]
