"""Dry-run-first migration for the independent External Context SQLite authority."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from typing import Any

from personal_knowledge.core.project_paths import EXTERNAL_CONTEXT_DB
from personal_knowledge.core.sqlite import connect_rw
from .registry import DEFAULT_REGISTRY, registry_checksum, source_definitions
from .schema import SCHEMA_VERSION, canonical_json


TABLES = (
    "external_source_registry", "external_import_runs", "external_observations",
    "external_facts", "external_fact_support", "external_lifecycle_events",
)
SNAPSHOT_TABLES = (
    "external_snapshots", "external_snapshot_members", "external_snapshot_watermarks",
    "external_snapshot_authority", "external_snapshot_events",
)
ALL_TABLES = TABLES + SNAPSHOT_TABLES
AUTHORITATIVE_TABLES = ALL_TABLES

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS external_source_registry (
    source_id                  TEXT PRIMARY KEY,
    authority_role             TEXT NOT NULL UNIQUE,
    definition_json            TEXT NOT NULL,
    definition_checksum        TEXT NOT NULL UNIQUE CHECK(length(definition_checksum) = 64),
    policy_version             TEXT NOT NULL,
    registered_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_import_runs (
    run_id                     TEXT PRIMARY KEY,
    source_id                  TEXT NOT NULL REFERENCES external_source_registry(source_id),
    source_definition_checksum TEXT NOT NULL CHECK(length(source_definition_checksum) = 64),
    input_manifest_json        TEXT NOT NULL,
    input_manifest_checksum    TEXT NOT NULL CHECK(length(input_manifest_checksum) = 64),
    status                     TEXT NOT NULL CHECK(status IN ('validated','published','rejected')),
    started_at                 TEXT NOT NULL,
    published_at               TEXT,
    UNIQUE(source_id, input_manifest_checksum)
);

CREATE TABLE IF NOT EXISTS external_observations (
    observation_id             TEXT PRIMARY KEY,
    run_id                     TEXT NOT NULL REFERENCES external_import_runs(run_id),
    source_id                  TEXT NOT NULL REFERENCES external_source_registry(source_id),
    observation_kind           TEXT NOT NULL,
    value_json                 TEXT NOT NULL,
    publication_time           TEXT NOT NULL,
    valid_from                 TEXT NOT NULL,
    valid_to                   TEXT,
    observed_at                TEXT NOT NULL,
    ingested_at                TEXT NOT NULL,
    region                     TEXT NOT NULL,
    payload_checksum           TEXT NOT NULL UNIQUE CHECK(length(payload_checksum) = 64),
    CHECK(valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE IF NOT EXISTS external_facts (
    fact_id                    TEXT PRIMARY KEY,
    run_id                     TEXT NOT NULL REFERENCES external_import_runs(run_id),
    subject                    TEXT NOT NULL,
    predicate                  TEXT NOT NULL,
    value_json                 TEXT NOT NULL,
    valid_from                 TEXT NOT NULL,
    valid_to                   TEXT,
    region                     TEXT NOT NULL,
    source_quality             REAL NOT NULL CHECK(source_quality >= 0.0 AND source_quality <= 1.0),
    fact_confidence            REAL NOT NULL CHECK(fact_confidence >= 0.0 AND fact_confidence <= 1.0),
    lifecycle                  TEXT NOT NULL CHECK(lifecycle IN ('current','stale','superseded','conflict','invalid')),
    payload_checksum           TEXT NOT NULL UNIQUE CHECK(length(payload_checksum) = 64),
    CHECK(valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE IF NOT EXISTS external_fact_support (
    support_id                 TEXT PRIMARY KEY,
    fact_id                    TEXT NOT NULL REFERENCES external_facts(fact_id),
    observation_id             TEXT NOT NULL REFERENCES external_observations(observation_id),
    support_checksum           TEXT NOT NULL UNIQUE CHECK(length(support_checksum) = 64),
    UNIQUE(fact_id, observation_id)
);

CREATE TABLE IF NOT EXISTS external_lifecycle_events (
    event_id                   TEXT PRIMARY KEY,
    fact_id                    TEXT NOT NULL REFERENCES external_facts(fact_id),
    sequence                   INTEGER NOT NULL CHECK(sequence >= 1),
    event_type                 TEXT NOT NULL CHECK(event_type IN ('created','staled','superseded','conflicted','invalidated')),
    previous_event_checksum    TEXT NOT NULL,
    payload_json               TEXT NOT NULL,
    payload_checksum           TEXT NOT NULL UNIQUE CHECK(length(payload_checksum) = 64),
    occurred_at                TEXT NOT NULL,
    UNIQUE(fact_id, sequence),
    CHECK((sequence = 1 AND previous_event_checksum = 'GENESIS') OR
          (sequence > 1 AND length(previous_event_checksum) = 64))
);

CREATE TABLE IF NOT EXISTS external_snapshots (
    snapshot_id                TEXT PRIMARY KEY,
    manifest_json              TEXT NOT NULL,
    manifest_hash              TEXT NOT NULL UNIQUE CHECK(length(manifest_hash) = 64),
    prepared_at                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_snapshot_watermarks (
    watermark_id               TEXT PRIMARY KEY,
    snapshot_id                TEXT NOT NULL REFERENCES external_snapshots(snapshot_id),
    source_id                  TEXT NOT NULL REFERENCES external_source_registry(source_id),
    run_id                     TEXT NOT NULL REFERENCES external_import_runs(run_id),
    input_manifest_checksum    TEXT NOT NULL CHECK(length(input_manifest_checksum) = 64),
    observed_at                TEXT NOT NULL,
    ingested_at                TEXT NOT NULL,
    watermark_checksum         TEXT NOT NULL CHECK(length(watermark_checksum) = 64),
    UNIQUE(snapshot_id, source_id, run_id)
);

CREATE TABLE IF NOT EXISTS external_snapshot_members (
    snapshot_id                TEXT NOT NULL REFERENCES external_snapshots(snapshot_id),
    fact_id                    TEXT NOT NULL REFERENCES external_facts(fact_id),
    fact_checksum              TEXT NOT NULL CHECK(length(fact_checksum) = 64),
    lifecycle                  TEXT NOT NULL CHECK(lifecycle IN ('current','stale','superseded','conflict','invalid')),
    region                     TEXT NOT NULL,
    watermark_id               TEXT NOT NULL REFERENCES external_snapshot_watermarks(watermark_id),
    PRIMARY KEY(snapshot_id, fact_id)
);

CREATE TABLE IF NOT EXISTS external_snapshot_events (
    event_id                   TEXT PRIMARY KEY,
    sequence                   INTEGER NOT NULL UNIQUE CHECK(sequence >= 1),
    event_type                 TEXT NOT NULL CHECK(event_type IN ('prepared','validated','activated','rollback','forward_restore','refused')),
    snapshot_id                TEXT REFERENCES external_snapshots(snapshot_id),
    snapshot_hash              TEXT CHECK(snapshot_hash IS NULL OR length(snapshot_hash) = 64),
    previous_snapshot_id       TEXT REFERENCES external_snapshots(snapshot_id),
    previous_event_checksum    TEXT NOT NULL,
    payload_json               TEXT NOT NULL,
    event_checksum             TEXT NOT NULL UNIQUE CHECK(length(event_checksum) = 64),
    occurred_at                TEXT NOT NULL,
    CHECK((sequence = 1 AND previous_event_checksum = 'GENESIS') OR
          (sequence > 1 AND length(previous_event_checksum) = 64))
);

CREATE TABLE IF NOT EXISTS external_snapshot_authority (
    authority_sequence         INTEGER PRIMARY KEY CHECK(authority_sequence >= 1),
    snapshot_id                TEXT NOT NULL REFERENCES external_snapshots(snapshot_id),
    snapshot_hash              TEXT NOT NULL CHECK(length(snapshot_hash) = 64),
    action                     TEXT NOT NULL CHECK(action IN ('activate','rollback','forward_restore')),
    previous_snapshot_id       TEXT REFERENCES external_snapshots(snapshot_id),
    activation_event_id        TEXT NOT NULL UNIQUE REFERENCES external_snapshot_events(event_id),
    activated_at               TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_external_import_source ON external_import_runs(source_id);
CREATE INDEX IF NOT EXISTS idx_external_observation_run ON external_observations(run_id);
CREATE INDEX IF NOT EXISTS idx_external_fact_run ON external_facts(run_id);
CREATE INDEX IF NOT EXISTS idx_external_fact_lifecycle ON external_facts(lifecycle);
CREATE INDEX IF NOT EXISTS idx_external_support_fact ON external_fact_support(fact_id);
CREATE INDEX IF NOT EXISTS idx_external_event_fact ON external_lifecycle_events(fact_id, sequence);
CREATE INDEX IF NOT EXISTS idx_external_snapshot_member ON external_snapshot_members(snapshot_id, fact_id);
CREATE INDEX IF NOT EXISTS idx_external_snapshot_watermark ON external_snapshot_watermarks(snapshot_id, source_id);
CREATE INDEX IF NOT EXISTS idx_external_snapshot_event ON external_snapshot_events(sequence);
"""


def _append_only_triggers() -> str:
    statements: list[str] = []
    for table in AUTHORITATIVE_TABLES:
        for action in ("UPDATE", "DELETE"):
            trigger = f"trg_{table}_no_{action.lower()}"
            statements.append(
                f"CREATE TRIGGER IF NOT EXISTS {trigger} BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"
            )
    return "\n".join(statements)


FULL_SCHEMA_SQL = SCHEMA_SQL + "\n" + _append_only_triggers()


def _read_connection(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def inspect_schema(
    db_path: Path = EXTERNAL_CONTEXT_DB,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    path = Path(db_path)
    expected = source_definitions(Path(registry_path))
    expected_checksum = registry_checksum(Path(registry_path))
    if not path.exists():
        return {
            "db_exists": False, "db_path": str(path), "schema_state": "unapplied",
            "schema_version": SCHEMA_VERSION, "existing_tables": [],
            "missing_tables": list(TABLES), "source_projection_count": 0,
            "expected_source_count": len(expected), "registry_checksum": expected_checksum,
            "foreign_key_violations": 0, "integrity_check": "not_run",
        }
    con = _read_connection(path)
    try:
        existing = {
            str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        present = sorted(set(ALL_TABLES) & existing)
        missing = sorted(set(ALL_TABLES) - existing)
        source_rows: list[sqlite3.Row | tuple[Any, ...]] = []
        projection_valid = False
        if "external_source_registry" in existing:
            source_rows = con.execute(
                "SELECT source_id,authority_role,definition_json,definition_checksum "
                "FROM external_source_registry ORDER BY source_id"
            ).fetchall()
            expected_rows = sorted(
                (
                    item.source_id, item.authority_role,
                    canonical_json(asdict(item)), item.definition_checksum,
                )
                for item in expected
            )
            projection_valid = list(source_rows) == expected_rows
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        trigger_count = int(con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_external_%_no_%'"
        ).fetchone()[0])
        state = "partial" if missing else "applied"
        if not missing and (not projection_valid or fk or integrity != "ok" or trigger_count != len(ALL_TABLES) * 2):
            state = "invalid"
        return {
            "db_exists": True, "db_path": str(path), "schema_state": state,
            "schema_version": SCHEMA_VERSION, "existing_tables": present,
            "missing_tables": missing, "source_projection_count": len(source_rows),
            "expected_source_count": len(expected), "source_projection_valid": projection_valid,
            "registry_checksum": expected_checksum, "append_only_trigger_count": trigger_count,
            "foreign_key_violations": len(fk), "integrity_check": integrity,
        }
    finally:
        con.close()


def migrate(
    db_path: Path = EXTERNAL_CONTEXT_DB,
    *,
    write: bool = False,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    path = Path(db_path)
    registry = Path(registry_path)
    before = inspect_schema(path, registry)
    if not write:
        return {
            "dry_run": True, "write": False, "db_path": str(path),
            "would_create": before["missing_tables"],
            "would_project_sources": before["expected_source_count"], "before": before,
        }
    if before["schema_state"] == "applied":
        return {"migrated": False, "no_op": True, "write": True, "after": before}
    if before["schema_state"] == "invalid":
        raise RuntimeError("external authority is invalid; refusing to overwrite append-only history")
    path.parent.mkdir(parents=True, exist_ok=True)
    definitions = source_definitions(registry)
    con = connect_rw(path, timeout=30)
    try:
        con.executescript("BEGIN IMMEDIATE;\n" + FULL_SCHEMA_SQL)
        for item in definitions:
            con.execute(
                "INSERT OR IGNORE INTO external_source_registry "
                "(source_id,authority_role,definition_json,definition_checksum,policy_version,registered_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    item.source_id, item.authority_role, canonical_json(asdict(item)),
                    item.definition_checksum, "1", "2026-07-18T00:00:00Z",
                ),
            )
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        if fk or integrity != "ok":
            raise sqlite3.IntegrityError(
                f"external authority validation failed: fk={len(fk)} integrity={integrity}"
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    after = inspect_schema(path, registry)
    if after["schema_state"] != "applied":
        raise RuntimeError(f"post-migration verification failed: {after}")
    return {"migrated": True, "no_op": False, "write": True, "after": after}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="External Context authority schema migration")
    parser.add_argument("--db", type=Path, default=EXTERNAL_CONTEXT_DB)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--write", action="store_true", help="Create/upgrade the independent authority")
    args = parser.parse_args(argv)
    try:
        result = migrate(args.db, write=args.write, registry_path=args.registry)
    except Exception as exc:
        result = {"ok": False, "error": {"code": "migration_failed", "detail": str(exc)}}
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALL_TABLES", "FULL_SCHEMA_SQL", "SCHEMA_SQL", "SNAPSHOT_TABLES", "TABLES",
    "inspect_schema", "migrate",
]
