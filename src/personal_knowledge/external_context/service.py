"""Read-only, checksum-verifying service for External Context snapshots and facts."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from .lifecycle import project_fact_lifecycle
from .snapshots import ExternalSnapshotError, get_active_snapshot


INTERFACE_SCHEMA_VERSION = "external_context_interface_v1"


class ExternalContextServiceError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00") if value.endswith("Z") else datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExternalContextServiceError("invalid_time", value) from exc
    if parsed.tzinfo is None:
        raise ExternalContextServiceError("invalid_time", value)
    return parsed.astimezone(timezone.utc)


def _ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise ExternalContextServiceError("database_missing", str(path))
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def validate_active_snapshot_policy(
    db_path: Path | str,
    *,
    snapshot_id: str,
    snapshot_hash: str,
    region: str,
    now: str,
    max_age_seconds: int,
    conflict_policy: str = "reject",
) -> dict[str, Any]:
    """Validate exact active identity plus freshness/region/conflict fail-closed policy."""
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or max_age_seconds < 0:
        raise ExternalContextServiceError("invalid_freshness_policy", str(max_age_seconds))
    if conflict_policy != "reject":
        raise ExternalContextServiceError("unsupported_conflict_policy", conflict_policy)
    try:
        active = get_active_snapshot(db_path)
    except ExternalSnapshotError as exc:
        raise ExternalContextServiceError(exc.code, exc.detail) from exc
    if active is None:
        raise ExternalContextServiceError("external_authority_missing")
    if active["snapshot_id"] != snapshot_id or active["snapshot_hash"] != snapshot_hash:
        raise ExternalContextServiceError("external_authority_drift", snapshot_id)
    manifest = active["manifest"]
    if manifest.get("region") != region:
        raise ExternalContextServiceError("external_region_mismatch", region)
    members = manifest.get("members")
    watermarks = manifest.get("watermarks")
    if not isinstance(members, list) or not isinstance(watermarks, list) or not members or not watermarks:
        raise ExternalContextServiceError("external_manifest_incomplete", snapshot_id)
    lifecycles = {str(item.get("lifecycle")) for item in members if isinstance(item, dict)}
    if "conflict" in lifecycles:
        raise ExternalContextServiceError("external_conflict_unresolved", snapshot_id)
    invalid = sorted(lifecycles - {"current"})
    if invalid:
        raise ExternalContextServiceError("external_lifecycle_ineligible", ",".join(invalid))
    at = _utc(now)
    watermark_times = [
        _utc(str(item.get("ingested_at") or ""))
        for item in watermarks if isinstance(item, dict)
    ]
    if len(watermark_times) != len(watermarks):
        raise ExternalContextServiceError("external_manifest_incomplete", snapshot_id)
    age = max((at - ingested).total_seconds() for ingested in watermark_times)
    if age < 0:
        raise ExternalContextServiceError("external_watermark_from_future", now)
    if age > max_age_seconds:
        raise ExternalContextServiceError("external_snapshot_stale", str(int(age)))
    for item in members:
        if not isinstance(item, dict):
            raise ExternalContextServiceError("external_manifest_incomplete", snapshot_id)
        if item.get("region") != region:
            raise ExternalContextServiceError("external_region_mismatch", str(item.get("fact_id")))
        valid_to = item.get("valid_to")
        if valid_to and _utc(str(valid_to)) < at:
            raise ExternalContextServiceError("external_fact_expired", str(item.get("fact_id")))
    return {
        "snapshot_id": snapshot_id, "snapshot_hash": snapshot_hash, "region": region,
        "freshness_age_seconds": int(age), "member_count": len(members),
        "authority_sequence": active["authority_sequence"],
    }


class ExternalContextService:
    """A metadata-only service; all SQLite connections are mode=ro/query_only."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    @staticmethod
    def _error(operation: str, code: str, detail: str = "") -> dict[str, Any]:
        return {
            "schema_version": INTERFACE_SCHEMA_VERSION, "operation": operation,
            "ok": False, "status": "error", "error": {"code": code, "detail": detail},
            "privacy": {"metadata_only": True, "raw_bodies": 0, "personal_writes": 0},
        }

    def invoke(self, operation: str, **params: Any) -> dict[str, Any]:
        handlers = {"snapshot.active": self.snapshot_active, "facts.list": self.facts_list,
                    "facts.get": self.facts_get}
        handler = handlers.get(operation)
        if handler is None:
            return self._error(operation, "unknown_operation", operation)
        try:
            return handler(**params)
        except (ExternalContextServiceError, ExternalSnapshotError) as exc:
            return self._error(operation, exc.code, exc.detail)
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._error(operation, "invalid_external_state", str(exc))

    @staticmethod
    def _success(operation: str, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": INTERFACE_SCHEMA_VERSION, "operation": operation,
            "ok": True, "status": "success", "data": data,
            "privacy": {"metadata_only": True, "raw_bodies": 0, "personal_writes": 0},
        }

    def snapshot_active(self) -> dict[str, Any]:
        active = get_active_snapshot(self.db_path)
        if active is None:
            raise ExternalContextServiceError("external_authority_missing")
        return self._success("snapshot.active", {
            key: active[key] for key in (
                "snapshot_id", "snapshot_hash", "authority_sequence", "action", "activated_at"
            )
        } | {"region": active["manifest"]["region"],
             "member_count": len(active["manifest"]["members"]),
             "watermarks": active["manifest"]["watermarks"]})

    def _fact(self, con: sqlite3.Connection, fact_id: str) -> dict[str, Any]:
        active = get_active_snapshot(self.db_path)
        if active is None:
            raise ExternalContextServiceError("external_authority_missing")
        member = next((item for item in active["manifest"]["members"] if item["fact_id"] == fact_id), None)
        if member is None:
            raise ExternalContextServiceError("fact_not_in_active_snapshot", fact_id)
        row = con.execute("SELECT * FROM external_facts WHERE fact_id=?", (fact_id,)).fetchone()
        if row is None:
            raise ExternalContextServiceError("fact_missing", fact_id)
        projection = project_fact_lifecycle(con, fact_id)
        if str(row["payload_checksum"]) != member["fact_checksum"] or projection.head_checksum != member["lifecycle_head_checksum"]:
            raise ExternalContextServiceError("fact_snapshot_drift", fact_id)
        try:
            value = json.loads(str(row["value_json"]))
        except json.JSONDecodeError as exc:
            raise ExternalContextServiceError("fact_value_invalid", fact_id) from exc
        return {
            "fact_id": fact_id, "fact_checksum": str(row["payload_checksum"]),
            "subject": str(row["subject"]), "predicate": str(row["predicate"]), "value": value,
            "valid_from": str(row["valid_from"]), "valid_to": row["valid_to"],
            "region": str(row["region"]), "source_quality": float(row["source_quality"]),
            "fact_confidence": float(row["fact_confidence"]), "lifecycle": projection.lifecycle,
            "snapshot_id": active["snapshot_id"], "snapshot_hash": active["snapshot_hash"],
        }

    def facts_list(self, *, limit: int = 50) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ExternalContextServiceError("invalid_limit", str(limit))
        active = get_active_snapshot(self.db_path)
        if active is None:
            raise ExternalContextServiceError("external_authority_missing")
        ids = [str(item["fact_id"]) for item in active["manifest"]["members"]]
        con = _ro(self.db_path)
        try:
            items = [self._fact(con, fact_id) for fact_id in ids[:limit]]
        finally:
            con.close()
        return self._success("facts.list", {"items": items, "total_available": len(ids), "limit": limit})

    def facts_get(self, *, fact_id: str) -> dict[str, Any]:
        con = _ro(self.db_path)
        try:
            item = self._fact(con, fact_id)
        finally:
            con.close()
        return self._success("facts.get", item)


__all__ = [
    "ExternalContextService", "ExternalContextServiceError", "INTERFACE_SCHEMA_VERSION",
    "validate_active_snapshot_policy",
]
