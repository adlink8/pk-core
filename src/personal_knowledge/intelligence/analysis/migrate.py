"""Dry-run-first schema for the isolated Decision Analysis Candidate authority."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from personal_knowledge.core.sqlite import connect_rw


TABLES = (
    "analysis_runs", "analysis_candidates", "analysis_claims",
    "analysis_evidence_refs", "analysis_provider_receipts", "analysis_events",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    run_id TEXT PRIMARY KEY, registry_id TEXT NOT NULL CHECK(registry_id='a.decision_analysis'),
    binding_json TEXT NOT NULL, binding_hash TEXT NOT NULL CHECK(length(binding_hash)=64),
    policy_version TEXT NOT NULL, policy_checksum TEXT NOT NULL CHECK(length(policy_checksum)=64),
    request_manifest_json TEXT NOT NULL, request_checksum TEXT NOT NULL CHECK(length(request_checksum)=64),
    response_manifest_json TEXT NOT NULL, response_checksum TEXT NOT NULL CHECK(length(response_checksum)=64),
    run_checksum TEXT NOT NULL UNIQUE CHECK(length(run_checksum)=64), status TEXT NOT NULL CHECK(status='committed'),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_candidates (
    candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES analysis_runs(run_id),
    binding_hash TEXT NOT NULL CHECK(length(binding_hash)=64), domain TEXT NOT NULL CHECK(domain='project'),
    candidate_status TEXT NOT NULL CHECK(candidate_status IN ('candidate','abstain')),
    payload_json TEXT NOT NULL, payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_claims (
    claim_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES analysis_candidates(candidate_id),
    claim_ordinal INTEGER NOT NULL CHECK(claim_ordinal>=0),
    claim_type TEXT NOT NULL CHECK(claim_type IN ('factual','inference')), statement TEXT NOT NULL,
    claim_checksum TEXT NOT NULL UNIQUE CHECK(length(claim_checksum)=64), created_at TEXT NOT NULL,
    UNIQUE(candidate_id,claim_ordinal)
);
CREATE TABLE IF NOT EXISTS analysis_evidence_refs (
    evidence_ref_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES analysis_claims(claim_id),
    evidence_ordinal INTEGER NOT NULL CHECK(evidence_ordinal>=0),
    authority_id TEXT NOT NULL CHECK(authority_id IN ('a.personal_change','s.external_fact')),
    record_type TEXT NOT NULL, record_id TEXT NOT NULL, record_checksum TEXT NOT NULL CHECK(length(record_checksum)=64),
    snapshot_id TEXT NOT NULL, snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash)=64),
    payload_json TEXT NOT NULL, payload_checksum TEXT NOT NULL CHECK(length(payload_checksum)=64), created_at TEXT NOT NULL,
    UNIQUE(claim_id,evidence_ordinal), UNIQUE(claim_id,authority_id,record_id)
);
CREATE TABLE IF NOT EXISTS analysis_provider_receipts (
    receipt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES analysis_runs(run_id),
    payload_json TEXT NOT NULL, payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_events (
    event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES analysis_runs(run_id), sequence INTEGER NOT NULL CHECK(sequence>=1),
    event_type TEXT NOT NULL CHECK(event_type IN ('candidate_published','abstained')),
    previous_event_checksum TEXT NOT NULL, payload_json TEXT NOT NULL,
    payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), occurred_at TEXT NOT NULL,
    UNIQUE(run_id,sequence), CHECK(sequence>1 OR previous_event_checksum='GENESIS')
);
"""


def _triggers() -> str:
    return "\n".join(
        f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{action.lower()} BEFORE {action} ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"
        for table in TABLES for action in ("UPDATE", "DELETE")
    )


FULL_SCHEMA_SQL = SCHEMA_SQL + "\n" + _triggers()


def inspect_schema(db_path: Path | str) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return {"db_exists": False, "schema_state": "unapplied", "missing_tables": list(TABLES)}
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON"); con.execute("PRAGMA foreign_keys=ON")
    try:
        existing = {str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(set(TABLES) - existing)
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        trigger_count = int(con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_analysis_%_no_%'").fetchone()[0])
        evidence_payload_global_unique = False
        if "analysis_evidence_refs" in existing:
            for index in con.execute("PRAGMA index_list('analysis_evidence_refs')"):
                if not bool(index[2]):
                    continue
                columns = [str(item[2]) for item in con.execute(
                    f"PRAGMA index_info('{str(index[1])}')"
                )]
                if columns == ["payload_checksum"]:
                    evidence_payload_global_unique = True
                    break
        state = "partial" if missing else "applied"
        if not missing and evidence_payload_global_unique:
            state = "legacy"
        if not missing and (integrity != "ok" or fk or trigger_count != len(TABLES) * 2):
            state = "invalid"
        return {"db_exists": True, "schema_state": state, "missing_tables": missing,
                "integrity": integrity, "foreign_key_violations": len(fk),
                "append_only_trigger_count": trigger_count,
                "evidence_payload_global_unique": evidence_payload_global_unique}
    finally:
        con.close()


def migrate(db_path: Path | str, *, write: bool = False) -> dict[str, Any]:
    path = Path(db_path)
    before = inspect_schema(path)
    if not write:
        return {
            "write": False, "dry_run": True, "would_create": before["missing_tables"],
            "would_repair_evidence_uniqueness": before["schema_state"] == "legacy",
            "before": before,
        }
    if before["schema_state"] == "applied":
        return {"write": True, "migrated": False, "no_op": True, "after": before}
    if before["schema_state"] == "invalid":
        raise RuntimeError("analysis authority invalid; refusing overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect_rw(path, timeout=30)
    try:
        if before["schema_state"] == "legacy":
            con.executescript("""
BEGIN IMMEDIATE;
DROP TRIGGER IF EXISTS trg_analysis_evidence_refs_no_update;
DROP TRIGGER IF EXISTS trg_analysis_evidence_refs_no_delete;
ALTER TABLE analysis_evidence_refs RENAME TO analysis_evidence_refs_legacy;
CREATE TABLE analysis_evidence_refs (
    evidence_ref_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES analysis_claims(claim_id),
    evidence_ordinal INTEGER NOT NULL CHECK(evidence_ordinal>=0),
    authority_id TEXT NOT NULL CHECK(authority_id IN ('a.personal_change','s.external_fact')),
    record_type TEXT NOT NULL, record_id TEXT NOT NULL, record_checksum TEXT NOT NULL CHECK(length(record_checksum)=64),
    snapshot_id TEXT NOT NULL, snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash)=64),
    payload_json TEXT NOT NULL, payload_checksum TEXT NOT NULL CHECK(length(payload_checksum)=64), created_at TEXT NOT NULL,
    UNIQUE(claim_id,evidence_ordinal), UNIQUE(claim_id,authority_id,record_id)
);
INSERT INTO analysis_evidence_refs SELECT * FROM analysis_evidence_refs_legacy;
DROP TABLE analysis_evidence_refs_legacy;
""" + _triggers())
        else:
            con.executescript("BEGIN IMMEDIATE;\n" + FULL_SCHEMA_SQL)
        if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError("analysis schema foreign key failure")
        con.commit()
    except Exception:
        con.rollback(); raise
    finally:
        con.close()
    after = inspect_schema(path)
    if after["schema_state"] != "applied":
        raise RuntimeError("analysis schema post-validation failed")
    return {"write": True, "migrated": True, "no_op": False, "after": after}


__all__ = ["FULL_SCHEMA_SQL", "SCHEMA_SQL", "TABLES", "inspect_schema", "migrate"]
