"""Confirmed, bounded inputs for deterministic decision-analysis generation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from personal_knowledge.intelligence.decision.context_binding import (
    DecisionContextBinding,
    validate_decision_context_binding,
)

from personal_knowledge.core.runtime_config import (
    analysis_max_output_tokens,
    analysis_temperature_max,
)

from .runs import load_policy
from .evidence import present_evidence_reference
from .schema import (
    ALLOWED_EVIDENCE_AUTHORITIES,
    SCHEMA_VERSION,
    AnalysisSchemaError,
    EvidenceReference,
    canonical_json,
    checksum,
    from_exact_mapping,
    reject_forbidden,
)


ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PROMPT_PATH = ROOT / "assets/prompts/decision_analysis_v1.txt"
DEFAULT_SCHEMA_PATH = ROOT / "assets/schemas/decision_analysis_response_v1.json"
DEFAULT_POLICY_PATH = ROOT / "governance/policies/decision_analysis.yaml"
PROMPT_VERSION = "decision-analysis-prompt-v1"
BEGIN_EVIDENCE = "<<<UNTRUSTED_EVIDENCE_BEGIN>>>"
END_EVIDENCE = "<<<UNTRUSTED_EVIDENCE_END>>>"


class AnalysisInputError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class ConfirmationEvent:
    event_id: str
    confirmed_at: str
    confirmed: bool
    actor: str = "user"

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.confirmed_at.strip():
            raise AnalysisInputError("confirmation_event_incomplete")
        if self.confirmed is not True or self.actor != "user":
            raise AnalysisInputError("user_confirmation_required")
        if len(self.event_id) > 256 or not self.confirmed_at.endswith("Z"):
            raise AnalysisInputError("confirmation_event_invalid")
        try:
            parsed = datetime.fromisoformat(self.confirmed_at[:-1] + "+00:00")
        except ValueError as exc:
            raise AnalysisInputError("confirmation_event_invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise AnalysisInputError("confirmation_event_invalid")


@dataclass(frozen=True)
class ConfirmedAnalysisInput:
    request_manifest: Mapping[str, Any]
    request_checksum: str
    rendered_prompt: str
    prompt_checksum: str
    schema_checksum: str
    policy_checksum: str


def _file_checksum(path: Path | str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise AnalysisInputError("lineage_asset_unreadable", str(path)) from exc


def _evidence(value: EvidenceReference | Mapping[str, Any]) -> EvidenceReference:
    if isinstance(value, EvidenceReference):
        return value
    try:
        return from_exact_mapping(EvidenceReference, value)
    except (AnalysisSchemaError, TypeError) as exc:
        raise AnalysisInputError("evidence_reference_invalid") from exc


def _normalize_evidence(
    values: Iterable[EvidenceReference | Mapping[str, Any]],
    *,
    authority_id: str,
    snapshot_id: str,
    snapshot_hash: str,
) -> tuple[EvidenceReference, ...]:
    items = tuple(_evidence(value) for value in values)
    if not items:
        raise AnalysisInputError("evidence_allowlist_required", authority_id)
    seen: set[tuple[str, str]] = set()
    for item in items:
        if item.authority_id != authority_id:
            raise AnalysisInputError("evidence_authority_mismatch", item.authority_id)
        if item.snapshot_id != snapshot_id or item.snapshot_hash != snapshot_hash:
            raise AnalysisInputError("evidence_snapshot_mismatch", item.record_id)
        key = (item.record_type, item.record_id)
        if key in seen:
            raise AnalysisInputError("duplicate_evidence_reference", item.record_id)
        seen.add(key)
    return tuple(sorted(items, key=lambda item: (item.record_type, item.record_id)))


def _normalized_weights(value: Mapping[str, float]) -> dict[str, float]:
    if not value or len(value) > 16:
        raise AnalysisInputError("weights_invalid")
    result: dict[str, float] = {}
    for key, item in value.items():
        if (not str(key).strip() or len(str(key)) > 128 or isinstance(item, bool)
                or not isinstance(item, (int, float)) or not math.isfinite(float(item)) or item <= 0):
            raise AnalysisInputError("weights_invalid", str(key))
        result[str(key)] = float(item)
    if abs(sum(result.values()) - 1.0) > 1e-9:
        raise AnalysisInputError("weights_not_normalized")
    return dict(sorted(result.items()))


def _prompt_template(path: Path | str) -> str:
    try:
        template = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AnalysisInputError("prompt_unreadable", str(path)) from exc
    required = (f"PROMPT_VERSION: {PROMPT_VERSION}", "{{CONTROL_JSON}}",
                "{{UNTRUSTED_EVIDENCE_JSON}}", BEGIN_EVIDENCE, END_EVIDENCE)
    if any(template.count(token) != 1 for token in required):
        raise AnalysisInputError("prompt_contract_invalid")
    if template.index(BEGIN_EVIDENCE) > template.index(END_EVIDENCE):
        raise AnalysisInputError("prompt_delimiter_invalid")
    return template


def build_confirmed_input(
    *,
    binding: DecisionContextBinding | Mapping[str, Any],
    personal_db_path: Path | str,
    external_db_path: Path | str,
    goal: str,
    constraints: Iterable[str],
    weights: Mapping[str, float],
    risk_budget: str,
    confirmation: ConfirmationEvent | Mapping[str, Any],
    personal_evidence: Iterable[EvidenceReference | Mapping[str, Any]],
    external_evidence: Iterable[EvidenceReference | Mapping[str, Any]],
    domain: str = "project",
    prompt_path: Path | str = DEFAULT_PROMPT_PATH,
    schema_path: Path | str = DEFAULT_SCHEMA_PATH,
    policy_path: Path | str = DEFAULT_POLICY_PATH,
    now: str | None = None,
    max_request_bytes: int = 32_768,
    temperature: float = 0.0,
    max_output_tokens: int = 4096,
) -> ConfirmedAnalysisInput:
    """Revalidate both authorities read-only and build an exact provider allowlist."""
    if not goal.strip() or len(goal) > 2_000:
        raise AnalysisInputError("goal_required")
    constraint_items = tuple(str(item).strip() for item in constraints)
    if (not constraint_items or any(not item or len(item) > 1_000 for item in constraint_items)
            or len(constraint_items) > 32):
        raise AnalysisInputError("constraints_invalid")
    if risk_budget != "low":
        raise AnalysisInputError("risk_budget_forbidden", risk_budget)
    event = confirmation if isinstance(confirmation, ConfirmationEvent) else from_exact_mapping(ConfirmationEvent, confirmation)
    policy, policy_checksum = load_policy(policy_path)
    sampling = policy.get("sampling") if isinstance(policy.get("sampling"), Mapping) else {}
    if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
            or not math.isfinite(float(temperature))
            or not float(sampling.get("temperature_min", 0.0)) <= float(temperature)
            <= float(sampling.get("temperature_max", analysis_temperature_max()))):
        raise AnalysisInputError("temperature_forbidden")
    if (isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int)
            or not 1 <= max_output_tokens <= int(sampling.get("max_output_tokens", analysis_max_output_tokens()))):
        raise AnalysisInputError("output_token_budget_forbidden")
    if domain not in policy.get("domain", {}).get("allow", ()):
        raise AnalysisInputError("domain_forbidden", domain)
    try:
        validated = validate_decision_context_binding(
            binding, personal_db_path, external_db_path, now=now,
        )
    except Exception as exc:
        raise AnalysisInputError("binding_validation_failed", str(exc)) from exc
    bound = dict(validated["binding"])
    personal = _normalize_evidence(
        personal_evidence, authority_id="a.personal_change",
        snapshot_id=bound["personal_snapshot_id"], snapshot_hash=bound["personal_snapshot_hash"],
    )
    external = _normalize_evidence(
        external_evidence, authority_id="s.external_fact",
        snapshot_id=bound["external_snapshot_id"], snapshot_hash=bound["external_snapshot_hash"],
    )
    if set(item.authority_id for item in (*personal, *external)) != ALLOWED_EVIDENCE_AUTHORITIES:
        raise AnalysisInputError("evidence_allowlist_incomplete")
    template = _prompt_template(prompt_path)
    prompt_checksum = _file_checksum(prompt_path)
    schema_checksum = _file_checksum(schema_path)
    controls = {
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "prompt_checksum": prompt_checksum,
        "schema_checksum": schema_checksum,
        "policy_version": str(policy["version"]),
        "policy_checksum": policy_checksum,
        "binding": bound,
        "binding_hash": bound["binding_hash"],
        "domain": domain,
        "goal": goal.strip(),
        "constraints": list(constraint_items),
        "weights": _normalized_weights(weights),
        "risk_budget": risk_budget,
        "confirmation": asdict(event),
        "generation": {
            "temperature": float(temperature), "max_output_tokens": max_output_tokens,
        },
    }
    evidence = {
        "personal": [asdict(item) for item in personal],
        "external": [asdict(item) for item in external],
    }
    evidence_context = {
        "personal": [present_evidence_reference(
            item, personal_db_path=personal_db_path, external_db_path=external_db_path,
        ) for item in personal],
        "external": [present_evidence_reference(
            item, personal_db_path=personal_db_path, external_db_path=external_db_path,
        ) for item in external],
    }
    manifest = {**controls, "evidence_allowlist": evidence, "evidence_context": evidence_context}
    reject_forbidden(manifest, "request")
    encoded = canonical_json(manifest).encode("utf-8")
    if len(encoded) > max_request_bytes:
        raise AnalysisInputError("request_payload_too_large", str(len(encoded)))
    request_checksum = checksum(manifest)
    rendered_controls = {**controls, "request_checksum": request_checksum}
    rendered = template.replace("{{CONTROL_JSON}}", canonical_json(rendered_controls)).replace(
        "{{UNTRUSTED_EVIDENCE_JSON}}", canonical_json({
            "allowlist": evidence, "context": evidence_context,
        }),
    )
    return ConfirmedAnalysisInput(
        request_manifest=manifest, request_checksum=request_checksum,
        rendered_prompt=rendered, prompt_checksum=prompt_checksum,
        schema_checksum=schema_checksum, policy_checksum=policy_checksum,
    )


__all__ = [
    "AnalysisInputError", "BEGIN_EVIDENCE", "ConfirmationEvent",
    "ConfirmedAnalysisInput", "END_EVIDENCE", "PROMPT_VERSION",
    "build_confirmed_input",
]
