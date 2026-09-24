"""KU-08 journal + source-watermark ledger (extracted from refresh_knowledge_units.py).

Owns the durable incremental-promote ledger: journal schema, watermark
read/advance/preconditions, dead-ref acknowledgement, and the
prepare/commit/rollback journal lifecycle. Pure extraction — no logic changed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from personal_knowledge.core.sqlite import connect_rw


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_incremental_journals (
    journal_id TEXT PRIMARY KEY,
    delta_inventory_id TEXT NOT NULL,
    fresh_run_id TEXT NOT NULL,
    source_before_checksum TEXT NOT NULL,
    source_after_checksum TEXT NOT NULL,
    candidate_collection TEXT,
    status TEXT NOT NULL,
    prepared_at TEXT NOT NULL,
    committed_at TEXT,
    rolled_back_at TEXT,
    detail_json TEXT
);
CREATE TABLE IF NOT EXISTS knowledge_dead_refs (
    evidence_ref TEXT NOT NULL,
    run_id TEXT NOT NULL,
    error_class TEXT,
    acknowledged_at TEXT NOT NULL,
    PRIMARY KEY (evidence_ref, run_id)
);
"""


def ensure_journal_schema(db_path: Path) -> None:
    con = connect_rw(db_path)
    con.executescript(JOURNAL_SCHEMA)
    con.execute(
        "CREATE TABLE IF NOT EXISTS knowledge_source_watermark ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    con.commit()
    con.close()


def get_committed_watermark(db_path: Path, *, key: str = "committed") -> str:
    """Read a watermark value without creating or mutating schema.

    key='committed'（user 轨，默认）或 'committed_assistant'（assistant 轨，
    Phase 41 D-04：存量 assistant 豁免，只抽增量）。
    """
    if not db_path.exists():
        return ""
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT value FROM knowledge_source_watermark WHERE key=?",
            (key,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    return row[0] if row else ""


def advance_watermark(db_path: Path, checksum: str, *, key: str = "committed") -> dict:
    """Advance source watermark. Only call after successful journal commit."""
    if not checksum:
        raise ValueError("checksum required for watermark advance")
    ensure_journal_schema(db_path)
    before = get_committed_watermark(db_path, key=key)
    con = connect_rw(db_path)
    con.execute(
        "INSERT INTO knowledge_source_watermark(key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, checksum, _utc_now()),
    )
    con.commit()
    con.close()
    return {"key": key, "before": before, "after": checksum, "changed": before != checksum}


def check_watermark_advance_preconditions(db_path: Path) -> dict:
    """Read-only preflight for watermark advance (fail-closed at CLI level).

    Advancing the watermark drops unprocessed refs from the next delta, so it
    must only happen when no extraction work is unfinished and every
    terminal_failed item has been explicitly acknowledged as a dead ref.

    Returns {"ok": bool, "unfinished": [...], "failed": [...]} where unfinished
    aggregates pending/in_flight/retryable items per (run_id, status) and
    failed aggregates terminal_failed items per run_id that are not yet
    recorded in knowledge_dead_refs.
    """
    result: dict = {"ok": False, "unfinished": [], "failed": []}
    if not db_path.exists():
        result["ok"] = True
        return result
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "knowledge_run_items" not in tables:
            result["ok"] = True
            return result
        result["unfinished"] = [
            {"run_id": run_id, "status": status, "count": count}
            for run_id, status, count in con.execute(
                "SELECT run_id, status, COUNT(*) FROM knowledge_run_items "
                "WHERE status IN ('pending','in_flight','retryable') "
                "GROUP BY run_id, status ORDER BY run_id, status"
            )
        ]
        exclusion = ""
        if "knowledge_dead_refs" in tables:
            exclusion = (
                "AND NOT EXISTS (SELECT 1 FROM knowledge_dead_refs d "
                "WHERE d.evidence_ref=i.evidence_ref AND d.run_id=i.run_id) "
            )
        result["failed"] = [
            {"run_id": run_id, "count": count}
            for run_id, count in con.execute(
                "SELECT i.run_id, COUNT(*) FROM knowledge_run_items i "
                "WHERE i.status='terminal_failed' " + exclusion +
                "GROUP BY i.run_id ORDER BY i.run_id"
            )
        ]
    finally:
        con.close()
    result["ok"] = not result["unfinished"] and not result["failed"]
    return result


def acknowledge_dead_refs(db_path: Path) -> int:
    """Record unacknowledged terminal_failed items into knowledge_dead_refs.

    Returns the number of dead-ref rows newly recorded.
    """
    ensure_journal_schema(db_path)
    con = connect_rw(db_path)
    tables = {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "knowledge_run_items" not in tables:
        con.close()
        return 0
    cur = con.execute(
        "INSERT OR IGNORE INTO knowledge_dead_refs "
        "(evidence_ref, run_id, error_class, acknowledged_at) "
        "SELECT evidence_ref, run_id, COALESCE(last_error_class, ''), ? "
        "FROM knowledge_run_items WHERE status='terminal_failed'",
        (_utc_now(),),
    )
    recorded = cur.rowcount
    con.commit()
    con.close()
    return recorded


def prepare_incremental_journal(
    db_path: Path,
    *,
    delta_inventory_id: str,
    fresh_run_id: str,
    source_before_checksum: str,
    source_after_checksum: str,
    candidate_collection: str = "",
) -> dict:
    """Durable prepare record for incremental promote. Does not touch active/watermark."""
    if not delta_inventory_id or not fresh_run_id:
        raise ValueError("delta_inventory_id and fresh_run_id required")
    if source_before_checksum == source_after_checksum:
        raise ValueError("cannot prepare journal for no-op delta")
    ensure_journal_schema(db_path)
    material = f"{delta_inventory_id}|{fresh_run_id}|{source_after_checksum}"
    journal_id = "ij_" + hashlib.sha256(material.encode()).hexdigest()[:16]
    con = connect_rw(db_path)
    existing = con.execute(
        "SELECT journal_id, status FROM knowledge_incremental_journals WHERE journal_id=?",
        (journal_id,),
    ).fetchone()
    if existing:
        con.close()
        return {
            "journal_id": journal_id,
            "status": existing[1],
            "idempotent": True,
            "delta_inventory_id": delta_inventory_id,
            "fresh_run_id": fresh_run_id,
        }
    con.execute(
        "INSERT INTO knowledge_incremental_journals "
        "(journal_id, delta_inventory_id, fresh_run_id, source_before_checksum, "
        "source_after_checksum, candidate_collection, status, prepared_at, detail_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            journal_id,
            delta_inventory_id,
            fresh_run_id,
            source_before_checksum,
            source_after_checksum,
            candidate_collection or "",
            "prepared",
            _utc_now(),
            json.dumps({"schema": "ku08_journal_v1"}, ensure_ascii=False),
        ),
    )
    con.commit()
    con.close()
    return {
        "journal_id": journal_id,
        "status": "prepared",
        "idempotent": False,
        "delta_inventory_id": delta_inventory_id,
        "fresh_run_id": fresh_run_id,
        "source_after_checksum": source_after_checksum,
        "watermark_changed": False,
        "active_changed": False,
    }


def commit_incremental_journal(
    db_path: Path,
    journal_id: str,
    *,
    active_pointer_path: Path | None = None,
    promote_collection: str | None = None,
) -> dict:
    """Atomic-ish commit: optional pointer write + watermark advance.

    Fail closed if journal missing/not prepared. Rollback leaves watermark alone.
    """
    ensure_journal_schema(db_path)
    con = connect_rw(db_path)
    row = con.execute(
        "SELECT status, source_after_checksum, candidate_collection, source_before_checksum "
        "FROM knowledge_incremental_journals WHERE journal_id=?",
        (journal_id,),
    ).fetchone()
    if not row:
        con.close()
        raise ValueError(f"journal not found: {journal_id}")
    status, src_after, candidate, src_before = row
    if status == "committed":
        con.close()
        return {
            "journal_id": journal_id,
            "status": "committed",
            "idempotent": True,
            "watermark_after": src_after,
        }
    if status not in ("prepared", "rolled_back"):
        con.close()
        raise ValueError(f"journal status {status} cannot commit")

    pointer_before = ""
    pointer_after = promote_collection or candidate or ""
    if active_pointer_path is not None and pointer_after:
        active_pointer_path.parent.mkdir(parents=True, exist_ok=True)
        if active_pointer_path.exists():
            pointer_before = active_pointer_path.read_text(encoding="utf-8").strip()
        tmp = active_pointer_path.with_suffix(active_pointer_path.suffix + ".tmp")
        tmp.write_text(pointer_after + "\n", encoding="utf-8")
        tmp.replace(active_pointer_path)

    con.execute(
        "INSERT INTO knowledge_source_watermark(key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        ("committed", src_after, _utc_now()),
    )
    # stash previous for rollback
    con.execute(
        "INSERT INTO knowledge_source_watermark(key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        ("previous", src_before, _utc_now()),
    )
    con.execute(
        "UPDATE knowledge_incremental_journals SET status='committed', committed_at=? "
        "WHERE journal_id=?",
        (_utc_now(), journal_id),
    )
    con.commit()
    con.close()
    return {
        "journal_id": journal_id,
        "status": "committed",
        "idempotent": False,
        "watermark_before": src_before,
        "watermark_after": src_after,
        "watermark_changed": True,
        "pointer_before": pointer_before,
        "pointer_after": pointer_after if active_pointer_path is not None else None,
        "active_changed": bool(active_pointer_path is not None and pointer_after),
    }


def rollback_incremental_journal(
    db_path: Path,
    journal_id: str,
    *,
    active_pointer_path: Path | None = None,
) -> dict:
    """Restore previous watermark (and optional pointer) from a committed journal."""
    ensure_journal_schema(db_path)
    con = connect_rw(db_path)
    row = con.execute(
        "SELECT status, source_before_checksum, source_after_checksum "
        "FROM knowledge_incremental_journals WHERE journal_id=?",
        (journal_id,),
    ).fetchone()
    if not row:
        con.close()
        raise ValueError(f"journal not found: {journal_id}")
    status, src_before, src_after = row
    if status != "committed":
        con.close()
        raise ValueError(f"journal status {status} cannot rollback")

    prev = con.execute(
        "SELECT value FROM knowledge_source_watermark WHERE key='previous'"
    ).fetchone()
    restore = prev[0] if prev else src_before
    con.execute(
        "INSERT INTO knowledge_source_watermark(key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        ("committed", restore, _utc_now()),
    )
    con.execute(
        "UPDATE knowledge_incremental_journals SET status='rolled_back', rolled_back_at=? "
        "WHERE journal_id=?",
        (_utc_now(), journal_id),
    )
    con.commit()
    con.close()

    pointer_restored = None
    if active_pointer_path is not None and active_pointer_path.exists():
        # best-effort: leave pointer; rollback watermark is the KU-08 safety property
        pointer_restored = active_pointer_path.read_text(encoding="utf-8").strip()

    return {
        "journal_id": journal_id,
        "status": "rolled_back",
        "watermark_after": restore,
        "watermark_from": src_after,
        "pointer": pointer_restored,
    }
