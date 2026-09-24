"""Staged semantic maintenance tools with evidence/model/schema binding."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
from typing import Any

from personal_knowledge.services.warehouse_mutations import (
    WarehouseMutationError,
    WarehouseOperationLedger,
)


OPERATIONS = frozenset({
    "knowledge.extract_l1", "knowledge.extract_l2", "knowledge.repair_candidates",
    "knowledge.detect_conflicts", "knowledge.backfill",
})
SCHEMA_VERSION = "pi_semantic_candidate_v1"
_TOKEN = re.compile(r"^[A-Za-z0-9:_-]{1,160}$")
_FORBIDDEN_FIELDS = frozenset({"serving", "active_pointer", "promotion", "lifecycle", "canonical", "delete", "path", "sql"})
_RECORD_FIELDS = frozenset({"candidate_id", "claim_checksum", "unit_type", "evidence_refs", "extractor", "model_receipt", "schema_version"})


class SemanticMaintenanceError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _token(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise SemanticMaintenanceError(f"{name}_invalid")
    return value


class SemanticCandidateStore:
    def __init__(self) -> None:
        self.candidates: dict[str, dict[str, Any]] = {}
        self.active_inventory_fingerprint = "active:fixture:unchanged"
        self._operation_candidates: dict[str, list[str]] = {}

    def stage(self, operation_id: str, records: list[dict[str, Any]]) -> list[str]:
        if operation_id in self._operation_candidates:
            return list(self._operation_candidates[operation_id])
        ids: list[str] = []
        for record in records:
            candidate_id = record["candidate_id"]
            self.candidates.setdefault(candidate_id, dict(record))
            ids.append(candidate_id)
        self._operation_candidates[operation_id] = ids
        return ids


class SemanticMaintenanceTools:
    """Prepare and stage semantic candidates; never publish or promote them."""

    def __init__(self, ledger: WarehouseOperationLedger, *, store: SemanticCandidateStore | None = None) -> None:
        self.ledger = ledger
        self.store = store or SemanticCandidateStore()
        self._pending: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def _validate(params: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "source_scope", "snapshot_checksum", "watermark_checksum", "batch_limit", "extractor",
            "model_receipt", "schema_version", "evidence_refs", "records", "idempotency_key",
            "actor", "profile", "now", "preview", "confirmed",
        }
        if set(params) - allowed:
            raise SemanticMaintenanceError("undeclared_input")
        for key in params:
            if key.lower() in _FORBIDDEN_FIELDS:
                raise SemanticMaintenanceError("promotion_field_forbidden")
        scope = _token(params.get("source_scope"), "source_scope")
        snapshot = _token(params.get("snapshot_checksum"), "snapshot_checksum")
        watermark = _token(params.get("watermark_checksum"), "watermark_checksum")
        extractor = _token(params.get("extractor"), "extractor")
        model_receipt = _token(params.get("model_receipt"), "model_receipt")
        schema_version = _token(params.get("schema_version"), "schema_version")
        limit = params.get("batch_limit", 20)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise SemanticMaintenanceError("batch_limit_invalid")
        refs = params.get("evidence_refs") or []
        if not isinstance(refs, list) or not refs or len(refs) > 100:
            raise SemanticMaintenanceError("evidence_refs_required")
        safe_refs: list[dict[str, str]] = []
        for ref in refs:
            if not isinstance(ref, Mapping) or set(ref) != {"ref", "checksum"}:
                raise SemanticMaintenanceError("evidence_ref_invalid")
            safe_refs.append({"ref": _token(ref["ref"], "evidence_ref"), "checksum": _token(ref["checksum"], "evidence_checksum")})
        records = params.get("records") or []
        if not isinstance(records, list) or len(records) > limit:
            raise SemanticMaintenanceError("candidate_scope_invalid")
        safe_records: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, Mapping) or set(record) - _RECORD_FIELDS:
                raise SemanticMaintenanceError("candidate_schema_invalid")
            if set(record) != _RECORD_FIELDS:
                raise SemanticMaintenanceError("candidate_receipt_missing")
            candidate = {
                "candidate_id": _token(record["candidate_id"], "candidate_id"),
                "claim_checksum": _token(record["claim_checksum"], "claim_checksum"),
                "unit_type": _token(record["unit_type"], "unit_type"),
                "evidence_refs": safe_refs,
                "extractor": _token(record["extractor"], "extractor"),
                "model_receipt": _token(record["model_receipt"], "model_receipt"),
                "schema_version": _token(record["schema_version"], "schema_version"),
            }
            if candidate["extractor"] != extractor or candidate["model_receipt"] != model_receipt or candidate["schema_version"] != schema_version:
                raise SemanticMaintenanceError("candidate_binding_mismatch")
            safe_records.append(candidate)
        return {
            "source_scope": scope, "snapshot_checksum": snapshot, "watermark_checksum": watermark,
            "batch_limit": limit, "extractor": extractor, "model_receipt": model_receipt,
            "schema_version": schema_version, "evidence_refs": safe_refs, "records": safe_records,
        }

    def invoke(self, operation: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if operation not in OPERATIONS:
            raise SemanticMaintenanceError("operation_unknown")
        params = dict(params or {})
        if params.get("preview"):
            preview = params["preview"]
            try:
                receipt = self.ledger.commit(
                    preview, idempotency_key=_token(params.get("idempotency_key"), "idempotency_key"),
                    now=params.get("now"),
                )
            except WarehouseMutationError as exc:
                raise SemanticMaintenanceError(exc.code) from exc
            candidate_ids = self.store.stage(preview["operation_id"], self._pending.get(preview["operation_id"], []))
            return {"schema_version": SCHEMA_VERSION, "status": "staged", "candidate_ids": candidate_ids, "receipt": receipt}
        bound = self._validate(params)
        key = _token(params.get("idempotency_key"), "idempotency_key")
        candidate_ids = [record["candidate_id"] for record in bound["records"]]
        plan = {"mode": "candidate_stage", "candidate_ids": candidate_ids, "reason": operation}
        try:
            preview = self.ledger.preview(
                operation, authority_id=bound["source_scope"], source_checksum=bound["source_scope"],
                snapshot_checksum=bound["snapshot_checksum"], watermark_checksum=bound["watermark_checksum"],
                count=len(bound["records"]), idempotency_key=key, actor=params.get("actor", "pi_kernel"),
                profile=params.get("profile", "production"), plan=plan, now=params.get("now"),
            )
        except WarehouseMutationError as exc:
            raise SemanticMaintenanceError(exc.code) from exc
        self._pending[preview["operation_id"]] = bound["records"]
        if not bound["records"]:
            return {"schema_version": SCHEMA_VERSION, "status": "previewed", "preview": preview, "candidate_ids": []}
        return {
            "schema_version": SCHEMA_VERSION, "status": "previewed", "preview": preview,
            "candidate_ids": candidate_ids, "limitations": ["candidate_only", "no_promotion", "no_active_pointer_write"],
            "evidence_checksum": _digest(bound["evidence_refs"]),
        }


__all__ = ["OPERATIONS", "SCHEMA_VERSION", "SemanticCandidateStore", "SemanticMaintenanceError", "SemanticMaintenanceTools"]
