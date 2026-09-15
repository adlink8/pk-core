"""Read-only exact evidence resolution for decision-analysis claims."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from personal_knowledge.external_context.service import ExternalContextService
from personal_knowledge.core.privacy_guard import guard_jsonable
from personal_knowledge.intelligence.decision.context_binding import (
    DecisionContextBinding,
    validate_decision_context_binding,
)
from personal_knowledge.intelligence.decision.runs import resolve_cognition_reference

from .schema import AnalysisClaim, AnalysisSchemaError, EvidenceReference, canonical_json, from_exact_mapping


class EvidenceGateError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class ResolvedEvidence:
    authority_id: str
    record_type: str
    record_id: str
    record_checksum: str
    snapshot_id: str
    snapshot_hash: str
    evidence_type: str


def _ro(path: Path | str) -> sqlite3.Connection:
    target = Path(path)
    if not target.exists():
        raise EvidenceGateError("evidence_authority_missing", str(target))
    con = sqlite3.connect(f"file:{target.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _personal_record(db_path: Path | str, ref: EvidenceReference) -> ResolvedEvidence:
    table_map = {
        "fact": ("personal_state_assertions", "assertion_id", "fact"),
        "observation": ("personal_state_assertions", "assertion_id", "observation"),
        "inference": ("personal_state_assertions", "assertion_id", "inference"),
        "assertion": ("personal_state_assertions", "assertion_id", None),
        "change": ("personal_state_changes", "change_id", "inference"),
        "risk": ("personal_state_risks", "risk_id", "inference"),
    }
    spec = table_map.get(ref.record_type)
    if spec is None:
        raise EvidenceGateError("evidence_type_incompatible", ref.record_type)
    table, id_column, required_type = spec
    con = _ro(db_path)
    try:
        columns = "run_id,payload_checksum"
        if table == "personal_state_assertions":
            columns += ",provenance_class"
        row = con.execute(
            f"SELECT {columns} FROM {table} WHERE {id_column}=?", (ref.record_id,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise EvidenceGateError("evidence_record_missing", ref.record_id)
    evidence_type = required_type or str(row["provenance_class"])
    if required_type and table == "personal_state_assertions" and str(row["provenance_class"]) != required_type:
        raise EvidenceGateError("evidence_type_incompatible", ref.record_id)
    try:
        resolved = resolve_cognition_reference(
            Path(db_path), source_run_id=str(row["run_id"]), record_id=ref.record_id,
            cognitive_type=evidence_type,
        )
    except Exception as exc:
        code = getattr(exc, "code", "evidence_record_invalid")
        raise EvidenceGateError(str(code), ref.record_id) from exc
    expected = {
        "authority_id": ref.authority_id,
        "record_id": ref.record_id,
        "record_checksum": ref.record_checksum,
        "snapshot_id": ref.snapshot_id,
        "snapshot_hash": ref.snapshot_hash,
    }
    actual = {key: getattr(resolved, key) for key in expected}
    if actual["snapshot_id"] != expected["snapshot_id"] or actual["snapshot_hash"] != expected["snapshot_hash"]:
        raise EvidenceGateError("evidence_snapshot_mismatch", ref.record_id)
    if actual["record_checksum"] != expected["record_checksum"]:
        raise EvidenceGateError("evidence_checksum_mismatch", ref.record_id)
    if actual["authority_id"] != expected["authority_id"] or actual["record_id"] != expected["record_id"]:
        raise EvidenceGateError("evidence_record_mismatch", ref.record_id)
    return ResolvedEvidence(**asdict(ref), evidence_type=evidence_type)


def _external_record(db_path: Path | str, ref: EvidenceReference) -> ResolvedEvidence:
    if ref.record_type not in {"fact", "external_fact"}:
        raise EvidenceGateError("evidence_type_incompatible", ref.record_type)
    response = ExternalContextService(db_path).invoke("facts.get", fact_id=ref.record_id)
    if not response.get("ok"):
        error = response.get("error") if isinstance(response.get("error"), Mapping) else {}
        code = str(error.get("code") or "evidence_record_invalid")
        if code in {"fact_missing", "fact_not_in_active_snapshot"}:
            code = "evidence_record_missing"
        raise EvidenceGateError(code, ref.record_id)
    item = response["data"]
    if item.get("snapshot_id") != ref.snapshot_id or item.get("snapshot_hash") != ref.snapshot_hash:
        raise EvidenceGateError("evidence_snapshot_mismatch", ref.record_id)
    if item.get("fact_checksum") != ref.record_checksum:
        raise EvidenceGateError("evidence_checksum_mismatch", ref.record_id)
    return ResolvedEvidence(**asdict(ref), evidence_type="fact")


def resolve_evidence_reference(
    reference: EvidenceReference,
    *,
    personal_db_path: Path | str,
    external_db_path: Path | str,
) -> ResolvedEvidence:
    if reference.authority_id == "a.personal_change":
        return _personal_record(personal_db_path, reference)
    if reference.authority_id == "s.external_fact":
        return _external_record(external_db_path, reference)
    raise EvidenceGateError("evidence_authority_invalid", reference.authority_id)


def present_evidence_reference(
    reference: EvidenceReference,
    *,
    personal_db_path: Path | str,
    external_db_path: Path | str,
) -> dict[str, Any]:
    """Resolve first, then expose one bounded structured value to the provider."""
    resolved = resolve_evidence_reference(
        reference, personal_db_path=personal_db_path, external_db_path=external_db_path,
    )
    if reference.authority_id == "s.external_fact":
        response = ExternalContextService(external_db_path).invoke("facts.get", fact_id=reference.record_id)
        if not response.get("ok"):
            raise EvidenceGateError("evidence_record_invalid", reference.record_id)
        item = response["data"]
        presentation: dict[str, Any] = {
            "reference": asdict(reference), "evidence_type": resolved.evidence_type,
            "subject": item["subject"], "predicate": item["predicate"],
            "value": item["value"], "valid_from": item["valid_from"],
            "valid_to": item["valid_to"], "region": item["region"],
            "source_quality": item["source_quality"],
            "fact_confidence": item["fact_confidence"],
        }
    else:
        table_map = {
            "fact": ("personal_state_assertions", "assertion_id"),
            "observation": ("personal_state_assertions", "assertion_id"),
            "inference": ("personal_state_assertions", "assertion_id"),
            "assertion": ("personal_state_assertions", "assertion_id"),
        }
        spec = table_map.get(reference.record_type)
        if spec is None:
            presentation = {
                "reference": asdict(reference), "evidence_type": resolved.evidence_type,
                "record_id": reference.record_id,
            }
        else:
            table, id_column = spec
            con = _ro(personal_db_path)
            try:
                row = con.execute(
                    f"SELECT subject,domain,scope,predicate,value_json,confidence,uncertainty,lifecycle "
                    f"FROM {table} WHERE {id_column}=?", (reference.record_id,),
                ).fetchone()
            finally:
                con.close()
            if row is None:
                raise EvidenceGateError("evidence_record_missing", reference.record_id)
            try:
                value = json.loads(str(row["value_json"]))
            except json.JSONDecodeError as exc:
                raise EvidenceGateError("evidence_record_invalid", reference.record_id) from exc
            presentation = {
                "reference": asdict(reference), "evidence_type": resolved.evidence_type,
                "subject": str(row["subject"]), "domain": str(row["domain"]),
                "scope": str(row["scope"]), "predicate": str(row["predicate"]),
                "value": value, "confidence": float(row["confidence"]),
                "uncertainty": str(row["uncertainty"]), "lifecycle": str(row["lifecycle"]),
            }
    guarded, privacy = guard_jsonable(presentation, mode="redact")
    if privacy.hit_count or canonical_json(guarded) != canonical_json(presentation):
        raise EvidenceGateError("evidence_privacy_risk", reference.record_id)
    if len(canonical_json(presentation).encode("utf-8")) > 8_192:
        raise EvidenceGateError("evidence_presentation_too_large", reference.record_id)
    return presentation


def validate_claim_evidence(
    claims: Iterable[AnalysisClaim],
    *,
    allowlist: Iterable[EvidenceReference | Mapping[str, Any]] | Mapping[str, Iterable[Mapping[str, Any]]],
    binding: DecisionContextBinding | Mapping[str, Any],
    personal_db_path: Path | str,
    external_db_path: Path | str,
    now: str | None = None,
) -> tuple[ResolvedEvidence, ...]:
    """Resolve every cited ref under the exact current binding; never write authorities."""
    try:
        validated = validate_decision_context_binding(
            binding, personal_db_path, external_db_path, now=now,
        )
    except Exception as exc:
        raise EvidenceGateError("binding_validation_failed", str(exc)) from exc
    bound = validated["binding"]
    raw_allowed: Iterable[EvidenceReference | Mapping[str, Any]]
    if isinstance(allowlist, Mapping):
        raw_allowed = tuple(item for group in allowlist.values() for item in group)
    else:
        raw_allowed = allowlist
    try:
        allowed_items = tuple(
            item if isinstance(item, EvidenceReference) else from_exact_mapping(EvidenceReference, item)
            for item in raw_allowed
        )
    except (AnalysisSchemaError, TypeError) as exc:
        raise EvidenceGateError("evidence_allowlist_invalid") from exc
    allowed = {canonical_json(asdict(item)): item for item in allowed_items}
    resolved: dict[str, ResolvedEvidence] = {}
    for claim in claims:
        if claim.claim_type == "factual" and not claim.evidence:
            raise EvidenceGateError("claim_support_missing", claim.claim_id)
        for ref in claim.evidence:
            key = canonical_json(asdict(ref))
            if key not in allowed:
                raise EvidenceGateError("claim_support_not_allowlisted", ref.record_id)
            expected_snapshot = (
                (bound["personal_snapshot_id"], bound["personal_snapshot_hash"])
                if ref.authority_id == "a.personal_change"
                else (bound["external_snapshot_id"], bound["external_snapshot_hash"])
            )
            if (ref.snapshot_id, ref.snapshot_hash) != expected_snapshot:
                raise EvidenceGateError("evidence_snapshot_mismatch", ref.record_id)
            item = resolved.get(key) or resolve_evidence_reference(
                ref, personal_db_path=personal_db_path, external_db_path=external_db_path,
            )
            if claim.claim_type == "factual" and item.evidence_type not in {"fact", "observation"}:
                raise EvidenceGateError("claim_evidence_type_incompatible", claim.claim_id)
            resolved[key] = item
    return tuple(resolved[key] for key in sorted(resolved))


__all__ = [
    "EvidenceGateError", "ResolvedEvidence", "resolve_evidence_reference",
    "present_evidence_reference", "validate_claim_evidence",
]
