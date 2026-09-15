"""Strict immutable contracts for non-authoritative decision-analysis candidates."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
import hashlib
import json
import re
from typing import Any, Mapping, TypeVar


SCHEMA_VERSION = "decision_analysis_candidate_v1"
REGISTRY_ID = "a.decision_analysis"
AUTHORITY_ROLE = "decision_analysis"
ALLOWED_DOMAINS = frozenset({"project"})
ALLOWED_EVIDENCE_AUTHORITIES = frozenset({"a.personal_change", "s.external_fact"})
ALLOWED_STATUSES = frozenset({"candidate", "abstain"})
FORBIDDEN_KEYS = frozenset({
    "approved", "command", "credential", "dispatch_target", "executed",
    "hidden_reasoning", "knowledge_unit", "raw_text", "response_text",
    "secret", "tool_call",
})
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|authorization|cookie|password|secret)\s*[:=]\s*\S+"
)
T = TypeVar("T")


class AnalysisSchemaError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise AnalysisSchemaError("unsupported_value", type(value).__name__)


def canonical_json(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{checksum(value)[:24]}"


def require_checksum(name: str, value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise AnalysisSchemaError("invalid_checksum", name)


def reject_forbidden(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            label = str(key).lower()
            if label in FORBIDDEN_KEYS or any(token in label for token in ("credential", "password", "tool_call")):
                raise AnalysisSchemaError("forbidden_field", f"{path}.{key}")
            reject_forbidden(item, f"{path}.{key}")
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            reject_forbidden(item, f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_RE.search(value):
        raise AnalysisSchemaError("secret_payload", path)


def from_exact_mapping(cls: type[T], value: Mapping[str, Any]) -> T:
    expected = {item.name for item in fields(cls)}
    actual = set(value)
    if actual != expected:
        raise AnalysisSchemaError("schema_fields_mismatch", f"missing={sorted(expected-actual)} extra={sorted(actual-expected)}")
    try:
        return cls(**dict(value))
    except TypeError as exc:
        raise AnalysisSchemaError("schema_type_invalid", cls.__name__) from exc


@dataclass(frozen=True)
class EvidenceReference:
    authority_id: str
    record_type: str
    record_id: str
    record_checksum: str
    snapshot_id: str
    snapshot_hash: str

    def __post_init__(self) -> None:
        if self.authority_id not in ALLOWED_EVIDENCE_AUTHORITIES:
            raise AnalysisSchemaError("evidence_authority_invalid", self.authority_id)
        if not self.record_type or not self.record_id or not self.snapshot_id:
            raise AnalysisSchemaError("evidence_field_required")
        require_checksum("record_checksum", self.record_checksum)
        require_checksum("snapshot_hash", self.snapshot_hash)


@dataclass(frozen=True)
class AnalysisClaim:
    claim_id: str
    claim_type: str
    statement: str
    evidence: tuple[EvidenceReference, ...]
    claim_checksum: str

    def __post_init__(self) -> None:
        if self.claim_type not in {"factual", "inference"}:
            raise AnalysisSchemaError("claim_type_invalid", self.claim_type)
        if not self.statement.strip():
            raise AnalysisSchemaError("claim_statement_required")
        if self.claim_type == "factual" and not self.evidence:
            raise AnalysisSchemaError("factual_claim_evidence_required", self.claim_id)
        require_checksum("claim_checksum", self.claim_checksum)


@dataclass(frozen=True)
class ProviderReceipt:
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    policy_version: str
    temperature: float
    max_output_tokens: int
    input_tokens: int
    output_tokens: int
    cost_amount: float
    cost_currency: str
    latency_ms: int
    request_checksum: str
    response_checksum: str
    status: str

    def __post_init__(self) -> None:
        for name in ("provider", "model", "prompt_version", "schema_version", "policy_version", "cost_currency", "status"):
            if not str(getattr(self, name)).strip():
                raise AnalysisSchemaError("receipt_field_required", name)
        if not 0.0 <= self.temperature <= 1.0 or min(
            self.max_output_tokens, self.input_tokens, self.output_tokens,
            self.latency_ms,
        ) < 0 or self.cost_amount < 0:
            raise AnalysisSchemaError("receipt_metric_invalid")
        require_checksum("request_checksum", self.request_checksum)
        require_checksum("response_checksum", self.response_checksum)


@dataclass(frozen=True)
class CandidateDraft:
    domain: str
    status: str
    options: tuple[Mapping[str, Any], ...]
    no_action_baseline: Mapping[str, Any]
    assumptions: tuple[str, ...]
    uncertainty: tuple[str, ...]
    missing_information: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    abstain_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        reject_forbidden(asdict(self), "candidate")
        if self.domain not in ALLOWED_DOMAINS:
            raise AnalysisSchemaError("domain_forbidden", self.domain)
        if self.status not in ALLOWED_STATUSES:
            raise AnalysisSchemaError("candidate_status_invalid", self.status)
        if self.status == "candidate" and (not self.options or not self.no_action_baseline):
            raise AnalysisSchemaError("candidate_structure_incomplete")
        if self.status == "abstain" and not self.abstain_reasons:
            raise AnalysisSchemaError("abstain_reason_required")
        option_fields = {"option_id", "title", "benefits", "costs", "risks", "opportunity_cost", "reversibility"}
        baseline_fields = {"benefits", "costs", "risks", "opportunity_cost", "reversibility"}
        for index, option in enumerate(self.options):
            if set(option) != option_fields:
                raise AnalysisSchemaError("option_fields_mismatch", str(index))
        if self.status == "candidate" and set(self.no_action_baseline) != baseline_fields:
            raise AnalysisSchemaError("baseline_fields_mismatch")


@dataclass(frozen=True)
class AnalysisCandidate:
    candidate_id: str
    run_id: str
    binding_hash: str
    draft: CandidateDraft
    candidate_payload: Mapping[str, Any]
    candidate_checksum: str

    def __post_init__(self) -> None:
        require_checksum("binding_hash", self.binding_hash)
        require_checksum("candidate_checksum", self.candidate_checksum)


@dataclass(frozen=True)
class AnalysisRun:
    run_id: str
    registry_id: str
    binding: Mapping[str, Any]
    binding_hash: str
    policy_version: str
    policy_checksum: str
    request_manifest: Mapping[str, Any]
    request_checksum: str
    response_manifest: Mapping[str, Any]
    response_checksum: str
    candidate: AnalysisCandidate
    claims: tuple[AnalysisClaim, ...]
    receipt: ProviderReceipt
    run_checksum: str

    def __post_init__(self) -> None:
        if self.registry_id != REGISTRY_ID:
            raise AnalysisSchemaError("registry_id_invalid", self.registry_id)
        for name in ("binding_hash", "policy_checksum", "request_checksum", "response_checksum", "run_checksum"):
            require_checksum(name, str(getattr(self, name)))


__all__ = [
    "ALLOWED_DOMAINS", "ALLOWED_EVIDENCE_AUTHORITIES", "AUTHORITY_ROLE",
    "AnalysisCandidate", "AnalysisClaim", "AnalysisRun", "AnalysisSchemaError",
    "CandidateDraft", "EvidenceReference", "ProviderReceipt", "REGISTRY_ID",
    "SCHEMA_VERSION", "canonical_json", "checksum", "from_exact_mapping",
    "reject_forbidden", "stable_id",
]
