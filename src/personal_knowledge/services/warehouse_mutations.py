"""Guarded ingestion and canonical operation ledger.

This module owns the transaction protocol for Phase 56.  It records operation
metadata in a small ledger and delegates any actual domain adapter to Python;
the Pi caller receives only a preview or a receipt.  Raw records are never
updated or deleted here.  Canonical corrections are compensation events.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


SCHEMA_VERSION = "pi_data_operation_v1"
RECEIPT_SCHEMA = "pi_data_operation_receipt_v1"
PREVIEW_SCHEMA = "pi_data_operation_preview_v1"
MUTATION_OPERATIONS = frozenset({
    "ingestion.discover", "ingestion.preview", "ingestion.commit", "ingestion.quarantine",
    "canonical.reconcile", "canonical.deduplicate", "canonical.link",
    "canonical.apply_correction", "canonical.verify",
    "knowledge.extract_l1", "knowledge.extract_l2", "knowledge.repair_candidates",
    "knowledge.detect_conflicts", "knowledge.backfill", "index.build", "index.reconcile", "index.evaluate",
    "snapshot.prepare", "snapshot.activate", "snapshot.rollback",
})
CANONICAL_OPERATIONS = frozenset({
    "canonical.reconcile", "canonical.deduplicate", "canonical.link", "canonical.apply_correction",
})
CONFIRMATION_OPERATIONS = CANONICAL_OPERATIONS | frozenset({"snapshot.activate", "snapshot.rollback"})
PROFILES = frozenset({"production", "operator", "test"})


class WarehouseMutationError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _safe_token(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise WarehouseMutationError(f"{name}_invalid")
    if "/" in value or "\\" in value or any(term in value.lower() for term in ("select", "delete", "truncate", "pragma", "attach", ";")):
        raise WarehouseMutationError(f"{name}_invalid")
    return value


def _timestamp(value: Any = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    if not isinstance(value, str):
        raise WarehouseMutationError("timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WarehouseMutationError("timestamp_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _expired(expires_at: str, now: str) -> bool:
    return datetime.fromisoformat(expires_at).timestamp() < datetime.fromisoformat(now).timestamp()


class InMemoryWarehouseStore:
    """Fixture adapter used by contract/integration tests and local dry-runs."""

    def __init__(self, raw_rows: list[Mapping[str, Any]] | None = None) -> None:
        self.raw_rows = [dict(row) for row in (raw_rows or [])]
        self.candidate_events: list[dict[str, Any]] = []
        self.canonical_events: list[dict[str, Any]] = []
        self.compensation_events: list[dict[str, Any]] = []
        self.markers: set[str] = set()
        self.watermark = "watermark:fixture:0"

    @property
    def raw_fingerprint(self) -> str:
        return _digest(self.raw_rows)

    @property
    def fingerprint(self) -> str:
        return _digest({
            "raw": self.raw_rows,
            "candidate": self.candidate_events,
            "canonical": self.canonical_events,
            "compensation": self.compensation_events,
            "watermark": self.watermark,
        })

    def apply(self, *, operation_id: str, capability_id: str, count: int, compensation_of: str | None = None) -> str:
        if operation_id in self.markers:
            return "already_applied"
        self.markers.add(operation_id)
        event = {
            "operation_id": operation_id,
            "capability_id": capability_id,
            "count": count,
            "compensation_of": compensation_of,
        }
        if capability_id == "canonical.apply_correction":
            self.compensation_events.append(event)
        elif capability_id.startswith("canonical."):
            self.canonical_events.append(event)
        else:
            self.candidate_events.append(event)
        return "applied"


class SqliteWarehouseStore:
    """Small durable store used by isolated Pi end-to-end runs.

    The production warehouse adapter remains an explicit dependency of the
    ledger.  This adapter is deliberately limited to append-only test events;
    it gives the cross-process Pi bridge a real SQLite observation point
    without granting it arbitrary SQL or filesystem access.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pi_test_warehouse_markers (
                    operation_id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS pi_test_warehouse_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL UNIQUE,
                    capability_id TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    compensation_of TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _event_rows(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT operation_id, capability_id, count, compensation_of "
                "FROM pi_test_warehouse_events ORDER BY event_id"
            ).fetchall()
        return [dict(row) for row in rows]

    @property
    def markers(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT operation_id FROM pi_test_warehouse_markers"
            ).fetchall()
        return {str(row[0]) for row in rows}

    @property
    def raw_fingerprint(self) -> str:
        return _digest([])

    @property
    def candidate_events(self) -> list[dict[str, Any]]:
        return [row for row in self._event_rows() if not str(row["capability_id"]).startswith("canonical.")]

    @property
    def canonical_events(self) -> list[dict[str, Any]]:
        return [
            row for row in self._event_rows()
            if str(row["capability_id"]).startswith("canonical.")
            and row["capability_id"] != "canonical.apply_correction"
        ]

    @property
    def compensation_events(self) -> list[dict[str, Any]]:
        return [row for row in self._event_rows() if row["capability_id"] == "canonical.apply_correction"]

    @property
    def watermark(self) -> str:
        return "watermark:sqlite-test:0"

    @property
    def fingerprint(self) -> str:
        return _digest({
            "raw": [],
            "candidate": self.candidate_events,
            "canonical": self.canonical_events,
            "compensation": self.compensation_events,
            "watermark": self.watermark,
        })

    def apply(self, *, operation_id: str, capability_id: str, count: int,
              compensation_of: str | None = None) -> str:
        created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM pi_test_warehouse_markers WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing:
                return "already_applied"
            connection.execute(
                "INSERT INTO pi_test_warehouse_markers(operation_id) VALUES(?)",
                (operation_id,),
            )
            connection.execute(
                "INSERT INTO pi_test_warehouse_events(operation_id, capability_id, count, compensation_of, created_at) "
                "VALUES(?,?,?,?,?)",
                (operation_id, capability_id, count, compensation_of, created_at),
            )
        return "applied"


class WarehouseOperationLedger:
    """SQLite metadata ledger with exact-preview and outcome reconciliation."""

    def __init__(self, ledger_path: str | Path, *, store: InMemoryWarehouseStore | SqliteWarehouseStore | None = None) -> None:
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = store or InMemoryWarehouseStore()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.ledger_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS pi_data_operations (
                    operation_id TEXT PRIMARY KEY,
                    capability_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    authority_id TEXT NOT NULL,
                    source_checksum TEXT NOT NULL,
                    snapshot_checksum TEXT NOT NULL,
                    watermark_checksum TEXT NOT NULL,
                    plan_checksum TEXT NOT NULL,
                    preview_checksum TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    before_fingerprint TEXT NOT NULL,
                    after_fingerprint TEXT NOT NULL DEFAULT '',
                    outcome_marker TEXT NOT NULL DEFAULT '',
                    receipt_json TEXT NOT NULL DEFAULT '',
                    compensation_of TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    UNIQUE(capability_id, idempotency_key)
                )"""
            )

    @staticmethod
    def _validate_common(*, operation: str, authority_id: str, source_checksum: str,
                         snapshot_checksum: str, watermark_checksum: str, count: int,
                         actor: str, profile: str, idempotency_key: str) -> None:
        if operation not in MUTATION_OPERATIONS:
            raise WarehouseMutationError("operation_unknown")
        _safe_token(authority_id, "authority_id")
        _safe_token(source_checksum, "source_checksum")
        _safe_token(snapshot_checksum, "snapshot_checksum")
        _safe_token(watermark_checksum, "watermark_checksum")
        _safe_token(actor, "actor")
        _safe_token(idempotency_key, "idempotency_key")
        if profile not in PROFILES:
            raise WarehouseMutationError("profile_unknown")
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 100_000:
            raise WarehouseMutationError("count_invalid")

    def preview(self, operation: str, *, authority_id: str, source_checksum: str,
                snapshot_checksum: str, watermark_checksum: str, count: int,
                idempotency_key: str, actor: str = "pi_kernel", profile: str = "production",
                before_fingerprint: str = "", plan: Mapping[str, Any] | None = None,
                compensation_of: str | None = None, now: Any = None,
                ttl_seconds: int = 900) -> dict[str, Any]:
        self._validate_common(
            operation=operation, authority_id=authority_id, source_checksum=source_checksum,
            snapshot_checksum=snapshot_checksum, watermark_checksum=watermark_checksum,
            count=count, actor=actor, profile=profile, idempotency_key=idempotency_key,
        )
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 86_400:
            raise WarehouseMutationError("ttl_invalid")
        created_at = _timestamp(now)
        existing = self._find_by_idempotency(operation, idempotency_key)
        if existing is not None:
            previous = self._preview_from_row(existing)
            if any(previous.get(field) != value for field, value in {
                "authority_id": authority_id,
                "source_checksum": source_checksum,
                "snapshot_checksum": snapshot_checksum,
                "watermark_checksum": watermark_checksum,
                "count": count,
            }.items()):
                raise WarehouseMutationError("idempotency_conflict")
            return previous
        plan_value = dict(plan or {})
        allowed_plan = {
            "mode", "count", "candidate_ids", "reason", "compensation_of", "raw_immutable", "append_only",
            "target_pointer", "current_pointer", "manifest_checksum", "eval_checksum", "protected_fingerprint",
        }
        if set(plan_value) - allowed_plan:
            raise WarehouseMutationError("undeclared_plan")
        if plan_value.get("candidate_ids") is not None:
            if not isinstance(plan_value["candidate_ids"], list) or len(plan_value["candidate_ids"]) > 100:
                raise WarehouseMutationError("candidate_scope_invalid")
            for value in plan_value["candidate_ids"]:
                _safe_token(value, "candidate_id")
        plan_value.setdefault("mode", "append_only")
        plan_value.setdefault("count", count)
        plan_value.setdefault("raw_immutable", True)
        plan_value.setdefault("append_only", True)
        if compensation_of:
            plan_value["compensation_of"] = _safe_token(compensation_of, "compensation_of")
        plan_checksum = _digest(plan_value)
        operation_id = f"op:{_digest({'operation': operation, 'idempotency_key': idempotency_key})[:24]}"
        expires_at = (datetime.fromisoformat(created_at) + timedelta(seconds=ttl_seconds)).isoformat()
        before = before_fingerprint or self.store.raw_fingerprint
        _safe_token(before, "before_fingerprint")
        payload = {
            "schema_version": PREVIEW_SCHEMA,
            "operation_id": operation_id,
            "capability_id": operation,
            "actor": actor,
            "profile": profile,
            "idempotency_key": idempotency_key,
            "authority_id": authority_id,
            "source_checksum": source_checksum,
            "snapshot_checksum": snapshot_checksum,
            "watermark_checksum": watermark_checksum,
            "plan": plan_value,
            "plan_checksum": plan_checksum,
            "count": count,
            "before_fingerprint": before,
            "confirmation_required": operation in CONFIRMATION_OPERATIONS,
            "expires_at": expires_at,
            "status": "previewed",
        }
        preview_checksum = _digest(payload)
        preview = {**payload, "preview_checksum": preview_checksum}
        with self._connect() as connection:
            try:
                connection.execute(
                    """INSERT INTO pi_data_operations
                    (operation_id, capability_id, actor, profile, idempotency_key, authority_id,
                     source_checksum, snapshot_checksum, watermark_checksum, plan_checksum,
                     preview_checksum, preview_json, status, count, before_fingerprint,
                     compensation_of, created_at, updated_at, expires_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        operation_id, operation, actor, profile, idempotency_key, authority_id,
                        source_checksum, snapshot_checksum, watermark_checksum, plan_checksum,
                        preview_checksum, json.dumps(preview, ensure_ascii=False, sort_keys=True),
                        "previewed", count, before, compensation_of or "", created_at, created_at, expires_at,
                    ),
                )
            except sqlite3.IntegrityError:
                return self._preview_from_row(self._find_by_idempotency(operation, idempotency_key))
        return preview

    def _find_by_idempotency(self, operation: str, key: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM pi_data_operations WHERE capability_id=? AND idempotency_key=?",
                (operation, key),
            ).fetchone()

    @staticmethod
    def _preview_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise WarehouseMutationError("operation_not_found")
        return json.loads(row["preview_json"])

    def _row(self, operation_id: str) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM pi_data_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise WarehouseMutationError("operation_not_found")
        return row

    @staticmethod
    def _verify_preview(preview: Mapping[str, Any]) -> None:
        if not isinstance(preview, Mapping) or preview.get("schema_version") != PREVIEW_SCHEMA:
            raise WarehouseMutationError("preview_invalid")
        expected = _digest({key: value for key, value in preview.items() if key != "preview_checksum"})
        if preview.get("preview_checksum") != expected:
            raise WarehouseMutationError("preview_checksum_mismatch")

    def commit(self, preview: Mapping[str, Any], *, confirmed: bool = False,
               idempotency_key: str, now: Any = None, snapshot_checksum: str | None = None,
               watermark_checksum: str | None = None, before_fingerprint: str | None = None,
               crash_at: str | None = None) -> dict[str, Any]:
        self._verify_preview(preview)
        operation_id = _safe_token(preview.get("operation_id"), "operation_id")
        row = self._row(operation_id)
        if row["idempotency_key"] != idempotency_key:
            raise WarehouseMutationError("idempotency_mismatch")
        stored_preview = json.loads(row["preview_json"])
        if dict(preview) != stored_preview:
            raise WarehouseMutationError("preview_checksum_mismatch")
        if row["status"] in {"committed", "compensated"}:
            return json.loads(row["receipt_json"])
        if row["status"] == "outcome_unknown":
            raise WarehouseMutationError("provider_outcome_unknown")
        timestamp = _timestamp(now)
        if _expired(row["expires_at"], timestamp):
            raise WarehouseMutationError("preview_stale")
        if row["capability_id"] in CONFIRMATION_OPERATIONS and confirmed is not True:
            raise WarehouseMutationError("explicit_confirmation_required")
        if snapshot_checksum is not None and snapshot_checksum != row["snapshot_checksum"]:
            raise WarehouseMutationError("snapshot_binding_mismatch")
        if watermark_checksum is not None and watermark_checksum != row["watermark_checksum"]:
            raise WarehouseMutationError("watermark_binding_mismatch")
        if before_fingerprint is not None and before_fingerprint != row["before_fingerprint"]:
            raise WarehouseMutationError("fingerprint_binding_mismatch")
        if self.store.raw_fingerprint != row["before_fingerprint"]:
            raise WarehouseMutationError("fingerprint_binding_mismatch")
        if crash_at == "before_transaction":
            raise WarehouseMutationError("simulated_crash")
        with self._connect() as connection:
            connection.execute(
                "UPDATE pi_data_operations SET status='executing', updated_at=? WHERE operation_id=? AND status='previewed'",
                (timestamp, operation_id),
            )
        before = row["before_fingerprint"]
        self.store.apply(
            operation_id=operation_id,
            capability_id=row["capability_id"],
            count=row["count"],
            compensation_of=row["compensation_of"] or None,
        )
        after = self.store.fingerprint
        if crash_at == "after_store_before_receipt":
            with self._connect() as connection:
                connection.execute(
                    "UPDATE pi_data_operations SET status='outcome_unknown', after_fingerprint=?, outcome_marker=?, updated_at=? WHERE operation_id=?",
                    (after, operation_id, timestamp, operation_id),
                )
            raise WarehouseMutationError("provider_outcome_unknown")
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "receipt_id": f"receipt:{operation_id[3:]}",
            "operation_id": operation_id,
            "capability_id": row["capability_id"],
            "status": "committed",
            "authority_id": row["authority_id"],
            "count": row["count"],
            "source_checksum": row["source_checksum"],
            "snapshot_checksum": row["snapshot_checksum"],
            "watermark_checksum": row["watermark_checksum"],
            "before_fingerprint": before,
            "after_fingerprint": after,
            "compensation_of": row["compensation_of"] or None,
        }
        with self._connect() as connection:
            connection.execute(
                "UPDATE pi_data_operations SET status='committed', after_fingerprint=?, receipt_json=?, updated_at=? WHERE operation_id=?",
                (after, json.dumps(receipt, ensure_ascii=False, sort_keys=True), timestamp, operation_id),
            )
        if crash_at == "after_receipt":
            raise WarehouseMutationError("provider_outcome_unknown")
        return receipt

    def reconcile_receipt(self, operation_id: str) -> dict[str, Any]:
        row = self._row(_safe_token(operation_id, "operation_id"))
        if row["status"] in {"committed", "compensated"}:
            return json.loads(row["receipt_json"])
        if row["status"] == "outcome_unknown" and row["outcome_marker"] and row["outcome_marker"] in self.store.markers:
            timestamp = _timestamp()
            receipt = {
                "schema_version": RECEIPT_SCHEMA,
                "receipt_id": f"receipt:{row['operation_id'][3:]}",
                "operation_id": row["operation_id"],
                "capability_id": row["capability_id"],
                "status": "committed",
                "authority_id": row["authority_id"],
                "count": row["count"],
                "source_checksum": row["source_checksum"],
                "snapshot_checksum": row["snapshot_checksum"],
                "watermark_checksum": row["watermark_checksum"],
                "before_fingerprint": row["before_fingerprint"],
                "after_fingerprint": row["after_fingerprint"],
                "compensation_of": row["compensation_of"] or None,
            }
            with self._connect() as connection:
                connection.execute(
                    "UPDATE pi_data_operations SET status='committed', receipt_json=?, updated_at=? WHERE operation_id=?",
                    (json.dumps(receipt, ensure_ascii=False, sort_keys=True), timestamp, row["operation_id"]),
                )
            return receipt
        raise WarehouseMutationError("provider_outcome_unknown")

    def resume(self, operation_id: str) -> dict[str, Any]:
        return self.reconcile_receipt(operation_id)

    def compensate(self, operation_id: str, *, idempotency_key: str, confirmed: bool = False,
                   now: Any = None) -> dict[str, Any]:
        original = self._row(_safe_token(operation_id, "operation_id"))
        if original["status"] not in {"outcome_unknown", "committed"}:
            raise WarehouseMutationError("compensation_not_allowed")
        preview = self.preview(
            "canonical.apply_correction",
            authority_id=original["authority_id"], source_checksum=original["source_checksum"],
            snapshot_checksum=original["snapshot_checksum"], watermark_checksum=original["watermark_checksum"],
            count=original["count"], idempotency_key=idempotency_key, actor=original["actor"],
            profile=original["profile"], before_fingerprint=self.store.raw_fingerprint,
            compensation_of=operation_id, now=now,
        )
        return self.commit(preview, confirmed=confirmed, idempotency_key=idempotency_key, now=now)

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        row = self._row(operation_id)
        return {
            "schema_version": SCHEMA_VERSION,
            "operation_id": row["operation_id"],
            "capability_id": row["capability_id"],
            "status": row["status"],
            "authority_id": row["authority_id"],
            "count": row["count"],
            "before_fingerprint": row["before_fingerprint"],
            "after_fingerprint": row["after_fingerprint"] or None,
            "compensation_of": row["compensation_of"] or None,
        }

    def get_preview(self, operation_id: str) -> dict[str, Any]:
        return self._preview_from_row(self._row(_safe_token(operation_id, "operation_id")))

    def invoke(self, operation: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        if operation == "canonical.verify":
            operation_id = _safe_token(params.get("operation_id"), "operation_id")
            return self.get_operation(operation_id)
        preview_value = params.get("preview")
        if operation in ({"ingestion.commit", "ingestion.quarantine"} | CANONICAL_OPERATIONS):
            if not preview_value:
                raise WarehouseMutationError("preview_required")
            return self.commit(
                preview_value, confirmed=params.get("confirmed") is True,
                idempotency_key=_safe_token(params.get("idempotency_key"), "idempotency_key"),
                now=params.get("now"), snapshot_checksum=params.get("snapshot_checksum"),
                watermark_checksum=params.get("watermark_checksum"),
            )
        return self.preview(
            operation,
            authority_id=params.get("authority_id", "knowledge"),
            source_checksum=params.get("source_checksum", "source:unknown"),
            snapshot_checksum=params.get("snapshot_checksum", "snapshot:unknown"),
            watermark_checksum=params.get("watermark_checksum", "watermark:unknown"),
            count=int(params.get("count", 0)),
            idempotency_key=_safe_token(params.get("idempotency_key"), "idempotency_key"),
            actor=params.get("actor", "pi_kernel"), profile=params.get("profile", "production"),
            before_fingerprint=params.get("before_fingerprint", ""), now=params.get("now"),
        )


WarehouseMutationService = WarehouseOperationLedger

__all__ = [
    "CANONICAL_OPERATIONS", "CONFIRMATION_OPERATIONS", "InMemoryWarehouseStore", "SqliteWarehouseStore", "MUTATION_OPERATIONS", "PREVIEW_SCHEMA",
    "RECEIPT_SCHEMA", "SCHEMA_VERSION", "WarehouseMutationError", "WarehouseMutationService",
    "WarehouseOperationLedger",
]
