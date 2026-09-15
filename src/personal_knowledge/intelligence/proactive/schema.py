"""Frozen metadata-only records for proactive intelligence."""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
from typing import Any, Mapping

CANONICAL_DOMAINS = ("learning", "career", "project", "health", "finance", "relationship", "time", "energy")
DOMAIN_ALIASES = {"study": "learning", "education": "learning", "work": "career", "job": "career", "projects": "project", "relationships": "relationship", "schedule": "time", "capacity": "energy"}
RELATION_TYPES = frozenset({"goal_support", "goal_conflict", "dependency", "resource_competition", "risk_propagation", "opportunity"})
RESOURCE_TYPES = frozenset({"time", "energy", "budget"})
FORBIDDEN_KEYS = frozenset({"content", "body", "raw_text", "note", "credential", "secret", "token", "password", "webhook", "command", "executable", "connector", "recipient", "send_target", "payment_detail"})


def canonical_domain(value: str, *, allow_unclassified: bool = False) -> str:
    normalized = value.strip().lower().replace("-", "_")
    normalized = DOMAIN_ALIASES.get(normalized, normalized)
    if normalized in CANONICAL_DOMAINS:
        return normalized
    if allow_unclassified and normalized == "unclassified":
        return normalized
    raise ValueError(f"unknown_domain:{value}")


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported canonical value:{type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_metadata_payload(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_KEYS or any(token in normalized for token in ("credential", "password", "webhook", "send_target")):
                raise ValueError(f"forbidden_payload:{path}.{key}")
            validate_metadata_payload(item, f"{path}.{key}")
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            validate_metadata_payload(item, f"{path}[{index}]")


@dataclass(frozen=True)
class SupportReference:
    authority_id: str
    record_type: str
    record_id: str
    record_checksum: str
    source_run_id: str
    source_run_checksum: str
    snapshot_id: str
    snapshot_hash: str


@dataclass(frozen=True)
class ResourceClaim:
    resource_type: str
    amount: float
    unit: str
    horizon_start: str
    horizon_end: str
    capacity: float
    source: SupportReference
    declared_by_user: bool = False
    resource_id: str = ""


@dataclass(frozen=True)
class GoalSignal:
    goal_id: str
    domain: str
    subject: str
    scope: str
    target: str
    valid_from: str
    valid_to: str | None
    observed_at: str
    confidence: float
    uncertainty: str
    support: SupportReference
    resources: tuple[ResourceClaim, ...] = ()
    sensitive: bool = False
    unresolved_conflict: bool = False


@dataclass(frozen=True)
class CoordinationDraft:
    relation_type: str
    subject: str
    scope: str
    domains: tuple[str, ...]
    valid_from: str
    valid_to: str | None
    observed_at: str
    rule_id: str
    rule_version: str
    confidence: float
    uncertainty: str
    source_refs: tuple[SupportReference, ...]
    resource_manifest: tuple[ResourceClaim, ...]


@dataclass(frozen=True)
class CoordinationItem:
    coordination_id: str
    run_id: str
    draft: CoordinationDraft
    payload: Mapping[str, Any]
    payload_checksum: str


@dataclass(frozen=True)
class CandidateDraft:
    candidate_class: str
    presentation_kind: str
    subject: str
    scope: str
    domains: tuple[str, ...]
    target_group: tuple[str, ...]
    valid_from: str
    expires_at: str
    support_refs: tuple[SupportReference, ...]
    severity: float
    urgency: float
    goal_impact: float
    cross_domain_impact: float
    evidence_strength: float
    user_relevance: float
    outcome_signal: float
    uncertainty: str
    reason_codes: tuple[str, ...]
    evidence_eligible: bool = True
    trust_eligible: bool = True
    sensitive: bool = False
    goal_relation_version: str = "v1"
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ImportanceVector:
    severity: float
    urgency: float
    goal_impact: float
    cross_domain_impact: float
    novelty: float
    evidence_strength: float
    user_relevance: float
    outcome_signal: float
    final_score: float


@dataclass(frozen=True)
class ProactiveCandidate:
    candidate_id: str
    run_id: str
    candidate_class: str
    presentation_kind: str
    subject: str
    scope: str
    domains: tuple[str, ...]
    target_group: tuple[str, ...]
    dedup_key: str
    cooldown_key: str
    material_change_signature: str
    valid_from: str
    expires_at: str
    policy_id: str
    policy_version: str
    importance: ImportanceVector
    novelty: float
    uncertainty: str
    reason_codes: tuple[str, ...]
    evidence_eligible: bool
    trust_eligible: bool
    sensitive: bool
    support_refs: tuple[SupportReference, ...]
    fixture_label: str
    payload: Mapping[str, Any]
    payload_checksum: str


@dataclass(frozen=True)
class ProactiveEvaluation:
    evaluation_id: str
    candidate_id: str
    policy_id: str
    policy_version: str
    window_start: str
    window_end: str
    result: str
    reason_codes: tuple[str, ...]
    deferred_until: str | None
    state_checksum: str
    payload: Mapping[str, Any]
    payload_checksum: str


@dataclass(frozen=True)
class ProactiveRun:
    run_id: str
    registry_id: str
    source_run_id: str
    source_run_checksum: str
    source_publication_sequence: int
    decision_run_id: str | None
    decision_run_checksum: str | None
    decision_event_frontier_checksum: str
    snapshot_id: str
    snapshot_hash: str
    control_frontier_checksum: str
    coordination_policy: str
    ranking_policy: str
    noise_policy: str
    input_manifest: Mapping[str, Any]
    input_manifest_checksum: str
    output_manifest: Mapping[str, Any]
    output_manifest_checksum: str
    run_checksum: str
    coordination_items: tuple[CoordinationItem, ...]
    candidates: tuple[ProactiveCandidate, ...] = ()
    evaluations: tuple[ProactiveEvaluation, ...] = ()
