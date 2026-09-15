"""Explicit first delta for an already active authority; never activation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from personal_knowledge.application.conversation.generation_delta import record_generation_delta

_SOURCE_TABLES = (
    "ce_event_generations", "ce_adapter_runs", "ce_sessions", "ce_events",
    "ce_event_relations", "ce_field_dispositions",
)


def preview_delta_baseline(db: Path) -> dict:
    """Read a complete source digest, but return only metadata and approval scope."""
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        con.execute("BEGIN")
        return _preview(con)
    finally:
        con.close()


def initialize_delta_baseline(db: Path, *, approval: str) -> dict:
    """Append all current event refs once; reject stale approval and existing work."""
    if not isinstance(approval, str) or not approval.startswith("INITIALIZE_EVENT_BASELINE:"):
        raise ValueError("exact baseline preview approval required")
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
    try:
        con.execute("BEGIN IMMEDIATE")
        preview = _preview(con)
        if approval != preview["confirmation"]:
            raise ValueError("baseline preview changed; obtain a fresh approval")
        if preview["status"] == "already_initialized":
            return preview
        delta_id = record_generation_delta(con, preview["generation_id"], None)
        con.execute("""CREATE TABLE ce_delta_baselines (
            generation_id TEXT PRIMARY KEY, snapshot_digest TEXT NOT NULL,
            delta_id INTEGER NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("INSERT INTO ce_delta_baselines(generation_id,snapshot_digest,delta_id) VALUES (?,?,?)",
                    (preview["generation_id"], preview["snapshot_digest"], delta_id))
        con.commit()
        return {**preview, "status": "initialized", "delta_id": delta_id}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _preview(con: sqlite3.Connection) -> dict:
    active = con.execute("SELECT generation_id FROM ce_generation_authority WHERE active=1").fetchall()
    if len(active) != 1:
        raise ValueError("baseline requires exactly one active generation")
    generation_id = active[0][0]
    identity = con.execute("SELECT source_manifest_id,dataset_digest FROM ce_event_generations WHERE generation_id=?",
                           (generation_id,)).fetchone()
    if not identity or not all(identity):
        raise ValueError("baseline source identity missing")
    bindings = con.execute("SELECT kind,generation_id,value FROM ce_activation_bindings ORDER BY kind").fetchall()
    bound = {kind: value for kind, gen, value in bindings if gen == generation_id and value}
    if (len(bindings) != 3 or len(bound) != 3 or bound.get("projection_version") != f"v2#{generation_id}"
            or not bound.get("projection_fingerprint")
            or bound.get("projection_watermark") != bound["projection_fingerprint"]):
        raise ValueError("baseline activation bindings inconsistent")
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    digest, event_count = _source_digest(con, generation_id, bindings)
    result = {"status": "preview", "generation_id": generation_id,
              "source_manifest_id": identity[0], "dataset_digest": identity[1],
              "snapshot_digest": digest, "event_count": event_count,
              "scope": "all_current_events_once", "paid_calls": 0,
              "confirmation": "INITIALIZE_EVENT_BASELINE:" + digest}
    if "ce_delta_baselines" in tables:
        row = con.execute("SELECT snapshot_digest,delta_id FROM ce_delta_baselines WHERE generation_id=?", (generation_id,)).fetchone()
        if not row or row[0] != digest:
            raise ValueError("existing baseline differs from current source")
        _validate_existing(con, row[1], result)
        return {**result, "status": "already_initialized", "delta_id": row[1]}
    if "ce_delta_prepare_cursor" in tables or "ce_delta_prepare_batches" in tables:
        raise ValueError("existing consumer state requires reconciliation, not first baseline")
    for table in ("ce_generation_deltas", "ce_generation_delta_events"):
        if table in tables and con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            raise ValueError("existing delta history requires reconciliation, not first baseline")
    return result


def _validate_existing(con: sqlite3.Connection, delta_id: int, preview: dict) -> None:
    delta = con.execute(
        "SELECT generation_id,event_count,source_manifest_id,dataset_digest,prior_generation_id,event_digest "
        "FROM ce_generation_deltas WHERE delta_id=?", (delta_id,),
    ).fetchone()
    expected = (preview["generation_id"], preview["event_count"], preview["source_manifest_id"], preview["dataset_digest"], None)
    if not delta or delta[:5] != expected:
        raise ValueError("existing baseline delta inconsistent")
    checksum, count = hashlib.sha256(), 0
    for event_id, change in con.execute(
        "SELECT event_id,change FROM ce_generation_delta_events WHERE delta_id=? ORDER BY event_id", (delta_id,)
    ):
        if change != "changed":
            raise ValueError("baseline contains non-baseline change")
        checksum.update(f"{len(event_id)}:{event_id}:{change}\n".encode())
        count += 1
    missing = con.execute(
        "SELECT event_id FROM ce_events WHERE generation_id=? EXCEPT "
        "SELECT event_id FROM ce_generation_delta_events WHERE delta_id=? LIMIT 1", (preview["generation_id"], delta_id)
    ).fetchone()
    if missing or count != preview["event_count"] or checksum.hexdigest() != delta[5]:
        raise ValueError("baseline event ledger inconsistent")


def _source_digest(con: sqlite3.Connection, generation_id: str, bindings: list) -> tuple[str, int]:
    checksum = hashlib.sha256()
    checksum.update(json.dumps(bindings, ensure_ascii=False, separators=(",", ":")).encode())
    event_count = 0
    for table in (*_SOURCE_TABLES, "ce_source_artifacts"):
        if con.execute(f"PRAGMA foreign_key_check({table})").fetchone():
            raise ValueError("source foreign-key integrity failed")
        columns = list(con.execute(f"PRAGMA table_info({table})"))
        keys = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        order = ",".join('"' + key + '"' for key in keys)
        if table == "ce_source_artifacts":
            condition = "artifact_id IN (SELECT artifact_id FROM ce_events WHERE generation_id=? UNION SELECT artifact_id FROM ce_sessions WHERE generation_id=?)"
            params = (generation_id, generation_id)
        else:
            condition, params = "generation_id=?", (generation_id,)
        checksum.update(json.dumps([table, [row[1] for row in columns]]).encode())
        for row in con.execute(f"SELECT * FROM {table} WHERE {condition} ORDER BY {order}", params):
            checksum.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
            event_count += table == "ce_events"
    return checksum.hexdigest(), event_count


def baseline_command(args) -> int:
    from personal_knowledge.core.project_paths import AGENT_CONVERSATIONS_DB
    db = args.conversation_db or AGENT_CONVERSATIONS_DB
    try:
        result = initialize_delta_baseline(db, approval=args.approval) if args.write else preview_delta_baseline(db)
    except ValueError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc), "written": False}))
        return 2
    except sqlite3.Error:
        # Source rows and paths are never included in failure diagnostics.
        print(json.dumps({"status": "blocked", "reason": "baseline_validation_or_approval_failed", "written": False}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
