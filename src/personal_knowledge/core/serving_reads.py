"""Read-only serving-snapshot primitives (OC-10).

The immutable serving-snapshot read helpers (``get_active_snapshot``,
``get_snapshot``, ``canonical_json``, ``manifest_hash``) live here in the
neutral ``core`` layer so the ``evaluation`` layer can bind evaluation targets
to a serving authority without importing into ``application``.  Write paths and
lifecycle orchestration stay in ``application/serving/snapshots.py``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def _snapshot_rows(con: sqlite3.Connection, snapshot_id: str) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    con.row_factory = sqlite3.Row
    snap = con.execute("SELECT * FROM serving_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    rows = con.execute(
        "SELECT m.serving_role, m.watermark_id, v.* FROM serving_snapshot_members m JOIN artifact_versions v ON v.artifact_version_id=m.artifact_version_id WHERE m.snapshot_id=? ORDER BY m.serving_role",
        (snapshot_id,),
    ).fetchall()
    return snap, rows


def get_active_snapshot(db_path: Path) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='serving_authority'").fetchone() is None:
            return None
        row = con.execute(
            "SELECT s.* FROM serving_authority a JOIN serving_snapshots s ON s.snapshot_id=a.active_snapshot_id WHERE a.singleton_id=1"
        ).fetchone()
        if row is None:
            return None
        _, members = _snapshot_rows(con, str(row["snapshot_id"]))
        return {**dict(row), "members": {m["serving_role"]: dict(m) for m in members}}
    finally:
        con.close()


def get_snapshot(db_path: Path, snapshot_id: str) -> dict[str, Any] | None:
    """Read any immutable snapshot without changing serving authority."""
    if not db_path.exists() or not snapshot_id:
        return None
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row, members = _snapshot_rows(con, snapshot_id)
        if row is None:
            return None
        return {**dict(row), "members": {m["serving_role"]: dict(m) for m in members}}
    finally:
        con.close()


__all__ = ["canonical_json", "get_active_snapshot", "get_snapshot", "manifest_hash"]
