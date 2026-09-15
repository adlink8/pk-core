"""Immutable lifecycle for the independent External Context snapshot authority."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw

from .lifecycle import project_fact_lifecycle
from .migrate import inspect_schema
from .schema import canonical_json, checksum, stable_id


class ExternalSnapshotError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ro(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise ExternalSnapshotError("database_missing", str(path))
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _event(
    con: sqlite3.Connection,
    event_type: str,
    snapshot_id: str | None,
    snapshot_hash: str | None,
    payload: Mapping[str, Any],
    *,
    occurred_at: str,
    previous_snapshot_id: str | None = None,
) -> str:
    head = con.execute(
        "SELECT sequence,event_checksum FROM external_snapshot_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(head[0]) + 1 if head else 1
    previous_checksum = str(head[1]) if head else "GENESIS"
    core = {
        "sequence": sequence, "event_type": event_type, "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash, "previous_snapshot_id": previous_snapshot_id,
        "previous_event_checksum": previous_checksum, "payload": dict(payload),
        "occurred_at": occurred_at,
    }
    digest = checksum(core)
    event_id = stable_id("ese", {"sequence": sequence, "checksum": digest})
    con.execute(
        "INSERT INTO external_snapshot_events VALUES (?,?,?,?,?,?,?,?,?,?)",
        (event_id, sequence, event_type, snapshot_id, snapshot_hash,
         previous_snapshot_id, previous_checksum, canonical_json(payload), digest, occurred_at),
    )
    return event_id


def _active_row(con: sqlite3.Connection) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM external_snapshot_authority ORDER BY authority_sequence DESC LIMIT 1"
    ).fetchone()


def _materialize(con: sqlite3.Connection, fact_ids: Iterable[str] | None) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    selected = sorted(set(str(value) for value in (fact_ids or ())))
    sql = (
        "SELECT f.*,r.source_id,r.input_manifest_checksum,r.input_manifest_json "
        "FROM external_facts f JOIN external_import_runs r ON r.run_id=f.run_id"
    )
    args: tuple[Any, ...] = ()
    if selected:
        sql += f" WHERE f.fact_id IN ({','.join('?' for _ in selected)})"
        args = tuple(selected)
    sql += " ORDER BY f.fact_id"
    rows = con.execute(sql, args).fetchall()
    if not rows:
        raise ExternalSnapshotError("snapshot_members_missing")
    if selected and {str(row["fact_id"]) for row in rows} != set(selected):
        raise ExternalSnapshotError("fact_missing")
    members: list[dict[str, Any]] = []
    watermarks_by_run: dict[str, dict[str, Any]] = {}
    regions: set[str] = set()
    for row in rows:
        fact_id = str(row["fact_id"])
        projection = project_fact_lifecycle(con, fact_id)
        manifest = json.loads(str(row["input_manifest_json"]))
        watermark = {
            "source_id": str(row["source_id"]), "run_id": str(row["run_id"]),
            "input_manifest_checksum": str(row["input_manifest_checksum"]),
            "observed_at": str(manifest["observed_at"]),
            "ingested_at": str(manifest["ingested_at"]),
        }
        watermark["watermark_checksum"] = checksum(watermark)
        watermarks_by_run[str(row["run_id"])] = watermark
        member = {
            "fact_id": fact_id, "fact_checksum": str(row["payload_checksum"]),
            "lifecycle": projection.lifecycle, "region": str(row["region"]),
            "valid_from": str(row["valid_from"]),
            "valid_to": str(row["valid_to"]) if row["valid_to"] else None,
            "run_id": str(row["run_id"]), "source_id": str(row["source_id"]),
            "lifecycle_head_checksum": projection.head_checksum,
        }
        regions.add(member["region"])
        members.append(member)
    if len(regions) != 1:
        raise ExternalSnapshotError("mixed_snapshot_regions", ",".join(sorted(regions)))
    watermarks = [watermarks_by_run[key] for key in sorted(watermarks_by_run)]
    manifest = {
        "schema_version": "external_snapshot_v1", "region": next(iter(regions)),
        "members": members, "watermarks": watermarks,
    }
    return manifest, members, watermarks


def prepare_snapshot(
    db_path: Path | str,
    fact_ids: Iterable[str] | None = None,
    *,
    write: bool = False,
    prepared_at: str | None = None,
) -> dict[str, Any]:
    """Prepare an exact manifest; dry-run is the default and changes no authority."""
    path = Path(db_path)
    if inspect_schema(path)["schema_state"] != "applied":
        raise ExternalSnapshotError("authority_not_ready")
    con = _ro(path)
    try:
        manifest, members, watermarks = _materialize(con, fact_ids)
    finally:
        con.close()
    digest = checksum(manifest)
    snapshot_id = stable_id("exs", digest)
    result = {
        "snapshot_id": snapshot_id, "snapshot_hash": digest, "manifest": manifest,
        "member_count": len(members), "watermark_count": len(watermarks),
        "written": False, "dry_run": not write,
    }
    if not write:
        return result
    timestamp = prepared_at or _now()
    con = connect_rw(path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT manifest_json,manifest_hash FROM external_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if existing:
            if str(existing["manifest_hash"]) != digest or str(existing["manifest_json"]) != canonical_json(manifest):
                raise ExternalSnapshotError("snapshot_id_collision", snapshot_id)
            con.rollback()
            return {**result, "existing": True}
        con.execute(
            "INSERT INTO external_snapshots VALUES (?,?,?,?)",
            (snapshot_id, canonical_json(manifest), digest, timestamp),
        )
        watermark_ids: dict[str, str] = {}
        for watermark in watermarks:
            watermark_id = stable_id("exw", {"snapshot_id": snapshot_id, **watermark})
            watermark_ids[watermark["run_id"]] = watermark_id
            con.execute(
                "INSERT INTO external_snapshot_watermarks VALUES (?,?,?,?,?,?,?,?)",
                (watermark_id, snapshot_id, watermark["source_id"], watermark["run_id"],
                 watermark["input_manifest_checksum"], watermark["observed_at"],
                 watermark["ingested_at"], watermark["watermark_checksum"]),
            )
        for member in members:
            con.execute(
                "INSERT INTO external_snapshot_members VALUES (?,?,?,?,?,?)",
                (snapshot_id, member["fact_id"], member["fact_checksum"], member["lifecycle"],
                 member["region"], watermark_ids[member["run_id"]]),
            )
        _event(con, "prepared", snapshot_id, digest, {"member_count": len(members)}, occurred_at=timestamp)
        assert_foreign_key_integrity(con)
        con.commit()
        return {**result, "written": True, "dry_run": False}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _verify_snapshot(con: sqlite3.Connection, snapshot_id: str) -> tuple[dict[str, Any], list[str]]:
    snapshot = con.execute("SELECT * FROM external_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if snapshot is None:
        return {}, ["snapshot_missing"]
    errors: list[str] = []
    try:
        manifest = json.loads(str(snapshot["manifest_json"]))
    except (TypeError, json.JSONDecodeError):
        return {}, ["snapshot_manifest_invalid"]
    digest = checksum(manifest)
    if digest != str(snapshot["manifest_hash"]):
        errors.append("snapshot_hash_mismatch")
    rows = con.execute(
        "SELECT m.*,f.payload_checksum,f.region AS fact_region,f.valid_from,f.valid_to,r.input_manifest_checksum,"
        "w.source_id,w.run_id,w.observed_at,w.ingested_at,w.watermark_checksum "
        "FROM external_snapshot_members m JOIN external_facts f ON f.fact_id=m.fact_id "
        "JOIN external_snapshot_watermarks w ON w.watermark_id=m.watermark_id "
        "JOIN external_import_runs r ON r.run_id=w.run_id WHERE m.snapshot_id=? ORDER BY m.fact_id",
        (snapshot_id,),
    ).fetchall()
    declared = manifest.get("members") if isinstance(manifest, dict) else None
    if not isinstance(declared, list) or len(declared) != len(rows):
        errors.append("snapshot_member_count_mismatch")
        declared = []
    by_id = {str(item.get("fact_id")): item for item in declared if isinstance(item, dict)}
    for row in rows:
        fact_id = str(row["fact_id"])
        item = by_id.get(fact_id)
        if not item:
            errors.append(f"snapshot_member_missing:{fact_id}")
            continue
        try:
            projection = project_fact_lifecycle(con, fact_id)
        except Exception:
            errors.append(f"fact_lifecycle_invalid:{fact_id}")
            continue
        expected = {
            "fact_checksum": str(row["payload_checksum"]), "lifecycle": projection.lifecycle,
            "region": str(row["fact_region"]), "run_id": str(row["run_id"]),
            "source_id": str(row["source_id"]), "lifecycle_head_checksum": projection.head_checksum,
            "valid_from": str(row["valid_from"]),
            "valid_to": str(row["valid_to"]) if row["valid_to"] else None,
        }
        if any(item.get(key) != value for key, value in expected.items()):
            errors.append(f"snapshot_member_drift:{fact_id}")
        if str(row["fact_checksum"]) != str(row["payload_checksum"]):
            errors.append(f"snapshot_fact_checksum_mismatch:{fact_id}")
        watermark_core = {
            "source_id": str(row["source_id"]), "run_id": str(row["run_id"]),
            "input_manifest_checksum": str(row["input_manifest_checksum"]),
            "observed_at": str(row["observed_at"]), "ingested_at": str(row["ingested_at"]),
        }
        if checksum(watermark_core) != str(row["watermark_checksum"]):
            errors.append(f"snapshot_watermark_drift:{row['watermark_id']}")
    return {"snapshot_id": snapshot_id, "snapshot_hash": digest, "manifest": manifest, "members": declared}, errors


def validate_snapshot(db_path: Path | str, snapshot_id: str, *, occurred_at: str | None = None) -> dict[str, Any]:
    path = Path(db_path)
    con = connect_rw(path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        snapshot, errors = _verify_snapshot(con, snapshot_id)
        timestamp = occurred_at or _now()
        if errors:
            _event(con, "refused", snapshot_id if snapshot else None,
                   snapshot.get("snapshot_hash") if snapshot else None,
                   {"operation": "validate", "errors": errors}, occurred_at=timestamp)
        elif con.execute(
            "SELECT 1 FROM external_snapshot_events WHERE snapshot_id=? AND event_type='validated'",
            (snapshot_id,),
        ).fetchone() is None:
            _event(con, "validated", snapshot_id, snapshot["snapshot_hash"], {}, occurred_at=timestamp)
        assert_foreign_key_integrity(con)
        con.commit()
        return {**snapshot, "ok": not errors, "errors": errors}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def get_snapshot(db_path: Path | str, snapshot_id: str) -> dict[str, Any] | None:
    con = _ro(Path(db_path))
    try:
        snapshot, errors = _verify_snapshot(con, snapshot_id)
        if not snapshot:
            return None
        if errors:
            raise ExternalSnapshotError("snapshot_drift", ",".join(errors))
        snapshot["validated"] = con.execute(
            "SELECT 1 FROM external_snapshot_events WHERE snapshot_id=? AND event_type='validated'",
            (snapshot_id,),
        ).fetchone() is not None
        return snapshot
    finally:
        con.close()


def get_active_snapshot(db_path: Path | str) -> dict[str, Any] | None:
    con = _ro(Path(db_path))
    try:
        authority = _active_row(con)
        if authority is None:
            return None
        snapshot, errors = _verify_snapshot(con, str(authority["snapshot_id"]))
        if errors or snapshot.get("snapshot_hash") != str(authority["snapshot_hash"]):
            raise ExternalSnapshotError("active_authority_drift", ",".join(errors))
        return {**snapshot, "authority_sequence": int(authority["authority_sequence"]),
                "action": str(authority["action"]), "activated_at": str(authority["activated_at"])}
    finally:
        con.close()


def _switch(
    db_path: Path | str,
    snapshot_id: str,
    *,
    action: str,
    write: bool,
    occurred_at: str | None,
    fault_at: str | None,
) -> dict[str, Any]:
    path = Path(db_path)
    # Preflight uses a read-only connection and cannot mutate on refusal/dry-run.
    candidate = get_snapshot(path, snapshot_id)
    if candidate is None:
        return {"ok": False, "error": "snapshot_missing", "written": False}
    if not candidate["validated"]:
        return {"ok": False, "error": "snapshot_not_validated", "written": False}
    active = get_active_snapshot(path)
    result = {
        "ok": True, "snapshot_id": snapshot_id, "snapshot_hash": candidate["snapshot_hash"],
        "previous_snapshot_id": (active or {}).get("snapshot_id"), "action": action,
        "written": False, "dry_run": not write,
    }
    if not write:
        return result
    con = connect_rw(path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        checked, errors = _verify_snapshot(con, snapshot_id)
        current = _active_row(con)
        if errors or checked.get("snapshot_hash") != candidate["snapshot_hash"]:
            raise ExternalSnapshotError("snapshot_drift", ",".join(errors))
        previous = str(current["snapshot_id"]) if current else None
        if previous != result["previous_snapshot_id"]:
            raise ExternalSnapshotError("authority_changed_during_switch")
        if fault_at == "before_event":
            raise RuntimeError("injected external snapshot failure before event")
        timestamp = occurred_at or _now()
        event_id = _event(con, action if action != "activate" else "activated", snapshot_id,
                          candidate["snapshot_hash"], {}, occurred_at=timestamp,
                          previous_snapshot_id=previous)
        if fault_at == "after_event":
            raise RuntimeError("injected external snapshot failure after event")
        sequence = int(current["authority_sequence"]) + 1 if current else 1
        con.execute(
            "INSERT INTO external_snapshot_authority VALUES (?,?,?,?,?,?,?)",
            (sequence, snapshot_id, candidate["snapshot_hash"], action, previous, event_id, timestamp),
        )
        if fault_at == "after_authority":
            raise RuntimeError("injected external snapshot failure after authority")
        assert_foreign_key_integrity(con)
        con.commit()
        return {**result, "written": True, "dry_run": False,
                "authority_sequence": sequence, "event_id": event_id}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def activate_snapshot(db_path: Path | str, snapshot_id: str, *, write: bool = False,
                      occurred_at: str | None = None, fault_at: str | None = None) -> dict[str, Any]:
    return _switch(db_path, snapshot_id, action="activate", write=write,
                   occurred_at=occurred_at, fault_at=fault_at)


def rollback_snapshot(db_path: Path | str, snapshot_id: str, *, write: bool = False,
                      occurred_at: str | None = None, fault_at: str | None = None) -> dict[str, Any]:
    return _switch(db_path, snapshot_id, action="rollback", write=write,
                   occurred_at=occurred_at, fault_at=fault_at)


def forward_restore_snapshot(db_path: Path | str, snapshot_id: str, *, write: bool = False,
                             occurred_at: str | None = None, fault_at: str | None = None) -> dict[str, Any]:
    return _switch(db_path, snapshot_id, action="forward_restore", write=write,
                   occurred_at=occurred_at, fault_at=fault_at)


__all__ = [
    "ExternalSnapshotError", "activate_snapshot", "forward_restore_snapshot",
    "get_active_snapshot", "get_snapshot", "prepare_snapshot", "rollback_snapshot",
    "validate_snapshot",
]
