"""Append-only SQLite authority for guarded decision sessions."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw

from .models import REGISTRY_ID, SCHEMA_VERSION


TABLES = (
    "orchestration_sessions", "orchestration_events",
    "orchestration_confirmations", "orchestration_invocations",
)


DDL = f"""
CREATE TABLE IF NOT EXISTS orchestration_sessions (
 session_id TEXT PRIMARY KEY,
 registry_id TEXT NOT NULL CHECK(registry_id='{REGISTRY_ID}'),
 schema_version TEXT NOT NULL CHECK(schema_version='{SCHEMA_VERSION}'),
 domain TEXT NOT NULL CHECK(domain='project'),
 risk_budget TEXT NOT NULL CHECK(risk_budget='low'),
 actor_identity_hash TEXT NOT NULL,
 binding_json TEXT NOT NULL,
 binding_hash TEXT NOT NULL,
 manifest_json TEXT NOT NULL,
 manifest_checksum TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orchestration_confirmations (
 confirmation_digest TEXT PRIMARY KEY,
 session_id TEXT NOT NULL,
 operation TEXT NOT NULL,
 preview_checksum TEXT NOT NULL,
 actor_identity_hash TEXT NOT NULL,
 expected_sequence INTEGER NOT NULL CHECK(expected_sequence >= 0),
 expires_at TEXT NOT NULL,
 consumed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orchestration_events (
 event_id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES orchestration_sessions(session_id),
 sequence INTEGER NOT NULL CHECK(sequence >= 1),
 operation TEXT NOT NULL,
 from_state TEXT NOT NULL,
 to_state TEXT NOT NULL,
 previous_event_checksum TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 payload_checksum TEXT NOT NULL,
 event_checksum TEXT NOT NULL,
 idempotency_key TEXT NOT NULL,
 actor_identity_hash TEXT NOT NULL,
 confirmation_digest TEXT NOT NULL REFERENCES orchestration_confirmations(confirmation_digest),
 occurred_at TEXT NOT NULL,
 UNIQUE(session_id, sequence),
 UNIQUE(session_id, idempotency_key),
 UNIQUE(confirmation_digest)
);
CREATE TABLE IF NOT EXISTS orchestration_invocations (
 invocation_id TEXT PRIMARY KEY,
 reservation_id TEXT NOT NULL,
 session_id TEXT NOT NULL REFERENCES orchestration_sessions(session_id),
 operation TEXT NOT NULL CHECK(operation='generate'),
 idempotency_key TEXT NOT NULL,
 request_checksum TEXT NOT NULL,
 stage TEXT NOT NULL CHECK(stage IN ('reserved','completed','abstained')),
 result_json TEXT,
 result_checksum TEXT,
 occurred_at TEXT NOT NULL,
 UNIQUE(session_id, operation, idempotency_key, stage)
);
CREATE INDEX IF NOT EXISTS idx_orchestration_events_session
 ON orchestration_events(session_id, sequence);
"""


def inspect_schema(db_path: Path | str) -> dict[str, Any]:
    path = Path(db_path)
    if not path.is_file():
        return {"schema_state": "unapplied", "tables": [], "immutable_triggers": 0}
    con = sqlite3.connect(str(path))
    try:
        tables = {str(row[0]) for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        present = [name for name in TABLES if name in tables]
        triggers = int(con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_orchestration_%_immutable'"
        ).fetchone()[0])
        state = "applied" if len(present) == len(TABLES) and triggers == len(TABLES) * 2 else (
            "unapplied" if not present else "partial"
        )
        return {"schema_state": state, "tables": present, "immutable_triggers": triggers}
    finally:
        con.close()


def apply_schema(db_path: Path | str) -> dict[str, Any]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect_rw(path, timeout=30)
    try:
        con.executescript(DDL)
        for table in TABLES:
            for verb in ("UPDATE", "DELETE"):
                suffix = verb.lower()
                con.execute(
                    f"CREATE TRIGGER IF NOT EXISTS trg_{table}_{suffix}_immutable "
                    f"BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'append_only'); END"
                )
        assert_foreign_key_integrity(con)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    result = inspect_schema(path)
    if result["schema_state"] != "applied":
        raise RuntimeError("orchestration_schema_postcheck_failed")
    return result


__all__ = ["DDL", "TABLES", "apply_schema", "inspect_schema"]
