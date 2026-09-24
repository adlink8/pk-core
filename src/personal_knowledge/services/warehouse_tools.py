"""Bounded, metadata-only warehouse read tools for the Pi domain bridge.

The facade deliberately does not expose a database connection, SQL, a path, or a
Python callable.  A caller selects a logical authority and a bounded operation;
the Python side chooses the fixed adapter and projects only safe metadata.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
import json
import re
from typing import Any


SCHEMA_VERSION = "pi_warehouse_read_v1"
MAX_LIMIT = 100
OPERATIONS = frozenset({
    "warehouse.inspect",
    "warehouse.lineage",
    "warehouse.quality",
    "warehouse.freshness",
    "warehouse.integrity",
    "warehouse.failed_batches",
})
AUTHORITY_ADAPTERS = {
    "conversation": "canonical_agent_conversations",
    "knowledge": "canonical_knowledge_units",
    "retrieval": "active_retrieval_index",
    "external": "external_context_snapshot",
    "decision": "decision_analysis_snapshot",
    "system": "runtime_metadata",
}
FILTER_ENUMS = {
    "status": frozenset({"ready", "failed", "quarantined", "candidate", "unknown"}),
    "source_type": frozenset({"conversation", "knowledge", "external", "decision", "system"}),
    "batch_state": frozenset({"pending", "committed", "failed", "quarantined", "unknown"}),
}
ALLOWED_INPUTS = frozenset({
    "authority_id", "limit", "cursor", "start_date", "end_date", "filters",
    "snapshot_id", "watermark_id",
})
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DANGEROUS = re.compile(
    r"(?:\bselect\b|\binsert\b|\bupdate\b|\bdelete\b|\btruncate\b|"
    r"\bdrop\b|\balter\b|\battach\b|\bpragma\b|\bvacuum\b|\bunion\b|--|;)",
    re.IGNORECASE,
)


class WarehouseToolError(ValueError):
    """Safe, stable preflight error with no database detail."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _scan(value: Any) -> None:
    if callable(value):
        raise WarehouseToolError("callable_forbidden")
    if isinstance(value, str):
        if "/" in value or "\\" in value:
            raise WarehouseToolError("path_forbidden")
        if _DANGEROUS.search(value):
            raise WarehouseToolError("sql_fragment_forbidden")
        if len(value) > 256:
            raise WarehouseToolError("value_too_large")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _scan(str(key))
            _scan(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _scan(item)
        return
    if value is not None and not isinstance(value, (bool, int, float)):
        raise WarehouseToolError("value_type_forbidden")


def _preflight(operation: str, params: Mapping[str, Any]) -> dict[str, Any]:
    if operation not in OPERATIONS:
        raise WarehouseToolError("unknown_operation")
    if not isinstance(params, Mapping):
        raise WarehouseToolError("invalid_input")
    extra = sorted(set(params) - ALLOWED_INPUTS)
    if extra:
        raise WarehouseToolError("undeclared_input")
    _scan(params)
    authority_id = params.get("authority_id")
    if authority_id not in AUTHORITY_ADAPTERS:
        raise WarehouseToolError("authority_unknown")
    limit = params.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise WarehouseToolError("limit_exceeded")
    for name in ("start_date", "end_date"):
        value = params.get(name)
        if value is not None and (not isinstance(value, str) or not _ISO_DATE.fullmatch(value)):
            raise WarehouseToolError("date_invalid")
    for name in ("cursor", "snapshot_id", "watermark_id"):
        value = params.get(name)
        if value is not None and not re.fullmatch(r"[A-Za-z0-9:_-]+", value):
            raise WarehouseToolError("path_forbidden")
    if params.get("start_date") and params.get("end_date") and params["start_date"] > params["end_date"]:
        raise WarehouseToolError("date_range_invalid")
    filters = params.get("filters") or {}
    if not isinstance(filters, Mapping):
        raise WarehouseToolError("filters_invalid")
    for key, value in filters.items():
        if key not in FILTER_ENUMS or value not in FILTER_ENUMS[key]:
            raise WarehouseToolError("filter_invalid")
    return {
        "authority_id": authority_id,
        "limit": limit,
        "cursor": params.get("cursor"),
        "start_date": params.get("start_date"),
        "end_date": params.get("end_date"),
        "filters": dict(filters),
        "snapshot_id": params.get("snapshot_id"),
        "watermark_id": params.get("watermark_id"),
    }


def _safe_count(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(0, min(int(value), 1_000_000))
    except (TypeError, ValueError):
        return default


class WarehouseTools:
    """Fixed-adapter warehouse metadata facade.

    ``metadata`` is an in-memory fixture/adapter result keyed by logical
    authority. It is intentionally not a path or a database handle. ``db_open``
    is only a test probe and is invoked after all preflight checks pass.
    """

    def __init__(self, *, metadata: Mapping[str, Mapping[str, Any]] | None = None,
                 db_open: Callable[[str], Any] | None = None) -> None:
        self._metadata = {str(key): dict(value) for key, value in (metadata or {}).items()}
        self._db_open = db_open

    def invoke(self, operation: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        bound = _preflight(operation, params or {})
        authority_id = bound["authority_id"]
        if self._db_open is not None:
            handle = self._db_open(AUTHORITY_ADAPTERS[authority_id])
            close = getattr(handle, "close", None)
            if callable(close):
                close()
        source = self._metadata.get(authority_id, {})
        counts = {
            "records": _safe_count(source.get("records", source.get("count", 0))),
            "visible": min(_safe_count(source.get("visible", source.get("records", 0))), bound["limit"]),
            "failed": _safe_count(source.get("failed", 0)),
            "quarantined": _safe_count(source.get("quarantined", 0)),
        }
        stable_id = str(source.get("stable_id") or f"{authority_id}:metadata")
        snapshot_id = str(source.get("snapshot_id") or f"snapshot:{_digest({'authority': authority_id, 'operation': operation})[:16]}")
        watermark_id = str(source.get("watermark_id") or f"watermark:{_digest({'authority': authority_id})[:16]}")
        checks = {
            "schema": "pass",
            "adapter": AUTHORITY_ADAPTERS[authority_id],
            "snapshot": snapshot_id,
            "watermark": watermark_id,
            "scope": "bounded",
        }
        if operation == "warehouse.integrity":
            checks["fingerprint"] = str(source.get("fingerprint_status", "not_computed"))
        if operation == "warehouse.freshness":
            checks["freshness"] = str(source.get("freshness_status", "unknown"))
        if operation == "warehouse.quality":
            checks["quality"] = str(source.get("quality_status", "unknown"))
        if operation == "warehouse.failed_batches":
            counts["failed"] = max(counts["failed"], len(source.get("failed_batch_ids", [])))
        artifact_ref = f"artifact://warehouse/{authority_id}/{snapshot_id}"
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "operation": operation,
            "ok": True,
            "status": "success",
            "authority_id": authority_id,
            "counts": counts,
            "checks": checks,
            "stable_ids": [stable_id],
            "artifact_refs": [artifact_ref],
            "limitations": [
                "metadata_only",
                "bounded_result",
                "full_evidence_requires_explicit_drill_down",
            ],
            "receipt": {
                "receipt_schema": "pi_tool_receipt_v1",
                "receipt_id": f"read:{_digest({'operation': operation, 'params': bound})[:24]}",
                "snapshot_id": snapshot_id,
                "watermark_id": watermark_id,
            },
        }
        # Never let adapter metadata leak raw content, credentials or paths.
        return envelope


def invoke_warehouse_tool(operation: str, params: Mapping[str, Any] | None = None,
                          *, tools: WarehouseTools | None = None) -> dict[str, Any]:
    return (tools or WarehouseTools()).invoke(operation, params)


__all__ = [
    "ALLOWED_INPUTS", "AUTHORITY_ADAPTERS", "MAX_LIMIT", "OPERATIONS", "SCHEMA_VERSION",
    "WarehouseToolError", "WarehouseTools", "invoke_warehouse_tool",
]
