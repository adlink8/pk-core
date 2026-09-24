"""Isolated retrieval generation, reconcile and evaluation tools."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
from typing import Any

from personal_knowledge.services.warehouse_mutations import WarehouseMutationError, WarehouseOperationLedger


OPERATIONS = frozenset({"index.build", "index.reconcile", "index.evaluate"})
SCHEMA_VERSION = "pi_retrieval_maintenance_v1"
_TOKEN = re.compile(r"^[A-Za-z0-9:_-]{1,160}$")


class RetrievalMaintenanceError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _token(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise RetrievalMaintenanceError(f"{name}_invalid")
    return value


class RetrievalGenerationStore:
    def __init__(self, active_generation: str = "generation:active:fixture") -> None:
        self.active_generation = active_generation
        self.generations: dict[str, dict[str, Any]] = {}

    def build(self, generation_id: str, record: dict[str, Any]) -> dict[str, Any]:
        self.generations.setdefault(generation_id, dict(record))
        return dict(self.generations[generation_id])


class RetrievalMaintenanceTools:
    def __init__(self, ledger: WarehouseOperationLedger, *, store: RetrievalGenerationStore | None = None) -> None:
        self.ledger = ledger
        self.store = store or RetrievalGenerationStore()
        self._evaluations: dict[str, dict[str, Any]] = {}

    def _build_preview(self, params: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"semantic_snapshot_checksum", "source_ids", "embedding_receipt", "index_schema_version", "idempotency_key", "actor", "profile", "now"}
        if set(params) - allowed:
            raise RetrievalMaintenanceError("undeclared_input")
        semantic = _token(params.get("semantic_snapshot_checksum"), "semantic_snapshot_checksum")
        embedding = _token(params.get("embedding_receipt"), "embedding_receipt")
        schema = _token(params.get("index_schema_version"), "index_schema_version")
        source_ids = params.get("source_ids") or []
        if not isinstance(source_ids, list) or not source_ids or len(source_ids) > 1000:
            raise RetrievalMaintenanceError("source_scope_invalid")
        source_ids = [_token(item, "source_id") for item in source_ids]
        key = _token(params.get("idempotency_key"), "idempotency_key")
        generation_id = f"generation:{_digest({'semantic': semantic, 'embedding': embedding, 'schema': schema, 'sources': source_ids})[:24]}"
        preview = self.ledger.preview(
            "index.build", authority_id="retrieval", source_checksum=semantic,
            snapshot_checksum=semantic, watermark_checksum=f"watermark:{semantic[:24]}",
            count=len(source_ids), idempotency_key=key, actor=params.get("actor", "pi_kernel"),
            profile=params.get("profile", "production"), plan={"mode": "new_generation", "candidate_ids": [generation_id]}, now=params.get("now"),
        )
        return {"preview": preview, "generation_id": generation_id, "semantic_snapshot_checksum": semantic, "embedding_receipt": embedding, "index_schema_version": schema, "source_ids": source_ids}

    def invoke(self, operation: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if operation not in OPERATIONS:
            raise RetrievalMaintenanceError("operation_unknown")
        params = dict(params or {})
        if operation == "index.build":
            if params.get("preview"):
                preview = params["preview"]
                try:
                    receipt = self.ledger.commit(preview, idempotency_key=_token(params.get("idempotency_key"), "idempotency_key"), now=params.get("now"))
                except WarehouseMutationError as exc:
                    raise RetrievalMaintenanceError(exc.code) from exc
                generation_id = (preview.get("plan") or {}).get("candidate_ids", [""])[0]
                self.store.build(generation_id, {"generation_id": generation_id, "status": "built", "receipt": receipt})
                return {"schema_version": SCHEMA_VERSION, "status": "built", "generation_id": generation_id, "active_generation": self.store.active_generation, "receipt": receipt}
            try:
                return {"schema_version": SCHEMA_VERSION, "status": "previewed", **self._build_preview(params)}
            except WarehouseMutationError as exc:
                raise RetrievalMaintenanceError(exc.code) from exc
        if operation == "index.reconcile":
            # The Pi gateway carries the task-scoped idempotency key on every
            # tool call; reconciliation itself is read-only and does not use
            # the key for generation state.
            allowed = {"generation_id", "expected_ids", "indexed_ids", "idempotency_key"}
            if set(params) - allowed:
                raise RetrievalMaintenanceError("undeclared_input")
            generation = _token(params.get("generation_id"), "generation_id")
            expected = set(params.get("expected_ids") or [])
            indexed = list(params.get("indexed_ids") or [])
            if not all(_TOKEN.fullmatch(str(value)) for value in expected | set(indexed)):
                raise RetrievalMaintenanceError("index_id_invalid")
            duplicates = len(indexed) - len(set(indexed))
            missing = len(expected - set(indexed))
            orphan = len(set(indexed) - expected)
            result = {"schema_version": SCHEMA_VERSION, "status": "reconciled", "generation_id": generation, "counts": {"missing": missing, "orphan": orphan, "duplicate": duplicates}, "ok": missing == orphan == duplicates == 0}
            if generation in self.store.generations:
                self.store.generations[generation]["reconcile"] = result["counts"]
            return result
        allowed = {"generation_id", "policy_id", "policy_checksum", "reconcile", "idempotency_key"}
        if set(params) - allowed:
            raise RetrievalMaintenanceError("undeclared_input")
        generation = _token(params.get("generation_id"), "generation_id")
        policy_id = _token(params.get("policy_id"), "policy_id")
        policy_checksum = _token(params.get("policy_checksum"), "policy_checksum")
        idempotency_key = _token(params.get("idempotency_key"), "idempotency_key")
        reconcile = params.get("reconcile") or {}
        if any(int(reconcile.get(key, 0)) != 0 for key in ("missing", "orphan", "duplicate")):
            raise RetrievalMaintenanceError("reconcile_not_clean")
        previous = self._evaluations.get(idempotency_key)
        if previous is not None:
            if previous["generation_id"] != generation or previous["policy_checksum"] != policy_checksum:
                raise RetrievalMaintenanceError("idempotency_conflict")
            return dict(previous)
        evidence_checksum = _digest({"generation_id": generation, "policy_id": policy_id, "policy_checksum": policy_checksum, "reconcile": reconcile})
        result = {"schema_version": SCHEMA_VERSION, "status": "evaluated", "generation_id": generation, "policy_id": policy_id, "policy_checksum": policy_checksum, "passed": True, "evidence_checksum": evidence_checksum, "active_generation": self.store.active_generation, "limitations": ["evaluation_only", "promotion_not_performed"]}
        self._evaluations[idempotency_key] = result
        return dict(result)


__all__ = ["OPERATIONS", "RetrievalGenerationStore", "RetrievalMaintenanceError", "RetrievalMaintenanceTools", "SCHEMA_VERSION"]
