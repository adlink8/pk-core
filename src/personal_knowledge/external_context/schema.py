"""Frozen contracts for the independent External Context authority.

The records in this module intentionally carry structured values, provenance and
checksums only.  Raw public pages and personal data are outside this authority.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
from typing import Any, Mapping


SCHEMA_VERSION = "external_context_authority_v1"
LIFECYCLES = frozenset({"current", "stale", "superseded", "conflict", "invalid"})
EVENT_TYPES = frozenset({"created", "staled", "superseded", "conflicted", "invalidated"})


class ExternalContextSchemaError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{checksum(value)[:24]}"


def _require_checksum(name: str, value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ExternalContextSchemaError("invalid_checksum", name)


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    authority_role: str
    owner: str
    source_type: str
    topic: str
    license: str
    provenance: str
    region: str
    publication_time_policy: str
    valid_time_policy: str
    observed_time_policy: str
    ingestion_time_policy: str
    quality_policy_version: str
    endpoint: str
    retention_policy: str
    definition_checksum: str

    def __post_init__(self) -> None:
        _require_checksum("definition_checksum", self.definition_checksum)


@dataclass(frozen=True)
class ImportRun:
    run_id: str
    source_id: str
    source_definition_checksum: str
    input_manifest_checksum: str
    status: str
    started_at: str
    published_at: str | None = None

    def __post_init__(self) -> None:
        _require_checksum("source_definition_checksum", self.source_definition_checksum)
        _require_checksum("input_manifest_checksum", self.input_manifest_checksum)
        if self.status not in {"validated", "published", "rejected"}:
            raise ExternalContextSchemaError("invalid_import_status", self.status)


@dataclass(frozen=True)
class ExternalObservation:
    observation_id: str
    run_id: str
    source_id: str
    observation_kind: str
    value: Any
    publication_time: str
    valid_from: str
    valid_to: str | None
    observed_at: str
    ingested_at: str
    region: str
    payload_checksum: str

    def __post_init__(self) -> None:
        _require_checksum("payload_checksum", self.payload_checksum)


@dataclass(frozen=True)
class ExternalFact:
    fact_id: str
    run_id: str
    subject: str
    predicate: str
    value: Any
    valid_from: str
    valid_to: str | None
    region: str
    source_quality: float
    fact_confidence: float
    lifecycle: str
    payload_checksum: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.source_quality <= 1.0:
            raise ExternalContextSchemaError("invalid_source_quality")
        if not 0.0 <= self.fact_confidence <= 1.0:
            raise ExternalContextSchemaError("invalid_fact_confidence")
        if self.lifecycle not in LIFECYCLES:
            raise ExternalContextSchemaError("invalid_lifecycle", self.lifecycle)
        _require_checksum("payload_checksum", self.payload_checksum)


@dataclass(frozen=True)
class FactSupport:
    support_id: str
    fact_id: str
    observation_id: str
    support_checksum: str

    def __post_init__(self) -> None:
        _require_checksum("support_checksum", self.support_checksum)


@dataclass(frozen=True)
class LifecycleEvent:
    event_id: str
    fact_id: str
    sequence: int
    event_type: str
    previous_event_checksum: str
    payload_checksum: str
    occurred_at: str

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ExternalContextSchemaError("invalid_sequence")
        if self.event_type not in EVENT_TYPES:
            raise ExternalContextSchemaError("invalid_event_type", self.event_type)
        if self.sequence > 1:
            _require_checksum("previous_event_checksum", self.previous_event_checksum)
        elif self.previous_event_checksum != "GENESIS":
            raise ExternalContextSchemaError("invalid_genesis")
        _require_checksum("payload_checksum", self.payload_checksum)


__all__ = [
    "EVENT_TYPES", "LIFECYCLES", "SCHEMA_VERSION", "ExternalContextSchemaError",
    "ExternalFact", "ExternalObservation", "FactSupport", "ImportRun",
    "LifecycleEvent", "SourceDefinition", "canonical_json", "checksum", "stable_id",
]
