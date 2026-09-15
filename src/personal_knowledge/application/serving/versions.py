"""Immutable artifact publication versions and source watermarks.

The tracked registry describes artifact types.  This module records only
metadata about successful publications in the private unified SQLite store.
It never activates a serving snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from personal_knowledge.application.serving.snapshots import canonical_json, sync_registry_entries
from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def file_checksum(path: Path) -> str:
    """Return a content checksum without exposing file contents."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def require_version_schema(con: sqlite3.Connection) -> None:
    required = {"artifact_registry_entries", "artifact_versions", "source_watermarks"}
    missing = sorted(name for name in required if not _table_exists(con, name))
    if missing:
        raise RuntimeError(
            "artifact version schema missing: " + ", ".join(missing)
            + "; run the knowledge schema migration first"
        )


def _record_publication_in_transaction(
    con: sqlite3.Connection,
    registry_by_role: Mapping[str, Mapping[str, Any]],
    *,
    registry_id: str,
    version: str,
    checksum: str,
    location_kind: str,
    location_ref: str,
    source_key: str,
    watermark_value: str,
    producer_run_id: str | None = None,
    evidence_version_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    values = (version.strip(), checksum.strip(), location_ref.strip(), watermark_value.strip())
    if any(not value for value in values):
        raise ValueError("version, checksum, location_ref and watermark_value are required")
    definition = next(
        (row for row in registry_by_role.values() if row["id"] == registry_id), None
    )
    if definition is None:
        raise ValueError(f"unknown registry id: {registry_id}")
    version_id = _stable_id("av", f"{registry_id}|{version}|{checksum}")
    watermark_id = _stable_id(
        "wm", f"{registry_id}|{source_key}|{watermark_value}"
    )
    version_exists = con.execute(
        "SELECT 1 FROM artifact_versions WHERE artifact_version_id=?", (version_id,)
    ).fetchone() is not None
    watermark_exists = con.execute(
        "SELECT 1 FROM source_watermarks WHERE watermark_id=?", (watermark_id,)
    ).fetchone() is not None
    recorded_at = now or _now()
    con.execute(
        "INSERT OR IGNORE INTO artifact_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            version_id,
            registry_id,
            version,
            checksum,
            location_kind,
            location_ref,
            "published",
            definition["privacy"],
            producer_run_id,
            evidence_version_id,
            canonical_json(dict(metadata or {})),
            recorded_at,
        ),
    )
    con.execute(
        "INSERT OR IGNORE INTO source_watermarks VALUES (?,?,?,?,?,?)",
        (
            watermark_id,
            registry_id,
            source_key,
            watermark_value,
            version_id,
            recorded_at,
        ),
    )
    # A stable watermark id can pre-exist with an older artifact binding.
    con.execute(
        "UPDATE source_watermarks SET artifact_version_id=?, recorded_at=? "
        "WHERE watermark_id=? AND artifact_version_id<>?",
        (version_id, recorded_at, watermark_id, version_id),
    )
    return {
        "registry_id": registry_id,
        "artifact_version_id": version_id,
        "watermark_id": watermark_id,
        "version": version,
        "checksum": checksum,
        "created": not version_exists,
        "watermark_created": not watermark_exists,
    }


def record_publications(
    db_path: Path,
    publications: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Record a related publication set in one SQLite transaction.

    ``evidence_registry_id`` may refer to an earlier item in the same batch;
    the immutable version id is resolved internally so callers do not need to
    know the stable-id formula.  Any invalid item rolls back the whole batch.
    """
    con = connect_rw(db_path, timeout=60)
    try:
        assert_foreign_key_integrity(con)
        require_version_schema(con)
        con.execute("BEGIN IMMEDIATE")
        registry_by_role = sync_registry_entries(con)
        recorded_at = _now()
        results: list[dict[str, Any]] = []
        by_registry: dict[str, dict[str, Any]] = {}
        for publication in publications:
            values = dict(publication)
            evidence_registry_id = values.pop("evidence_registry_id", None)
            if evidence_registry_id is not None:
                evidence = by_registry.get(str(evidence_registry_id))
                if evidence is None:
                    raise ValueError(
                        "evidence_registry_id must reference an earlier "
                        f"publication in the batch: {evidence_registry_id}"
                    )
                values["evidence_version_id"] = evidence["artifact_version_id"]
            result = _record_publication_in_transaction(
                con, registry_by_role, now=recorded_at, **values
            )
            results.append(result)
            by_registry[result["registry_id"]] = result
        con.commit()
        return results
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def record_publication(
    db_path: Path,
    **publication: Any,
) -> dict[str, Any]:
    """Record one publication atomically and idempotently."""
    return record_publications(db_path, [publication])[0]


def record_conversation_publications(
    db_path: Path,
    canonical_db: Path,
) -> list[dict[str, Any]]:
    """Atomically bind the canonical conversation store and message view."""
    checksum = file_checksum(canonical_db)
    return record_publications(db_path, [
        {
            "registry_id": "d.canonical_conversation",
            "version": checksum,
            "checksum": checksum,
            "location_kind": "sqlite_store",
            "location_ref": str(canonical_db),
            "source_key": "agentsview",
            "watermark_value": checksum,
        },
        {
            "registry_id": "d.canonical_message",
            "version": checksum,
            "checksum": checksum,
            "location_kind": "sqlite_view",
            "location_ref": f"{canonical_db}#canonical_messages",
            "source_key": "canonical_conversation",
            "watermark_value": checksum,
            "evidence_registry_id": "d.canonical_conversation",
        },
    ])


def publication_status(db_path: Path) -> dict[str, Any]:
    """Read current publication metadata; never creates or changes state."""
    if not db_path.exists():
        return {"ok": False, "schema_ready": False, "error": "unified_db_missing", "artifacts": {}}
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        try:
            require_version_schema(con)
        except RuntimeError as exc:
            return {"ok": False, "schema_ready": False, "error": str(exc), "artifacts": {}}
        rows = con.execute(
            "SELECT r.registry_id, r.authority_role, v.artifact_version_id, v.version, "
            "v.checksum, v.location_kind, v.location_ref, v.producer_run_id, "
            "v.metadata_json, v.created_at, w.watermark_id, w.source_key, "
            "w.value AS watermark_value, w.recorded_at "
            "FROM artifact_registry_entries r "
            "LEFT JOIN artifact_versions v ON v.artifact_version_id=("
            " SELECT v2.artifact_version_id FROM artifact_versions v2 "
            " WHERE v2.registry_id=r.registry_id ORDER BY v2.created_at DESC, v2.artifact_version_id DESC LIMIT 1) "
            "LEFT JOIN source_watermarks w ON w.watermark_id=("
            " SELECT w2.watermark_id FROM source_watermarks w2 "
            " WHERE w2.registry_id=r.registry_id ORDER BY w2.recorded_at DESC, w2.watermark_id DESC LIMIT 1) "
            "ORDER BY r.registry_id"
        ).fetchall()
        artifacts: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}") if item.get("artifact_version_id") else {}
            artifacts[str(row["registry_id"])] = item
        return {"ok": True, "schema_ready": True, "artifacts": artifacts}
    finally:
        con.close()
