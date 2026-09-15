"""Append-only authority for paired recommendation calibration."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from personal_knowledge.core.sqlite import connect_rw


REGISTRY_ID = "a.recommendation_calibration"
SCHEMA_VERSION = "recommendation_calibration_v1"
TABLES = ("calibration_protocols", "calibration_cohort_members", "calibration_arms",
          "calibration_measurements", "calibration_verdicts", "calibration_proposals",
          "calibration_events")


class CalibrationSchemaError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code; self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS calibration_protocols (
 protocol_id TEXT PRIMARY KEY, registry_id TEXT NOT NULL CHECK(registry_id='a.recommendation_calibration'),
 protocol_status TEXT NOT NULL CHECK(protocol_status='frozen'), payload_json TEXT NOT NULL,
 payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), frozen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibration_cohort_members (
 member_id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES calibration_protocols(protocol_id),
 ordinal INTEGER NOT NULL CHECK(ordinal>=0), case_id TEXT NOT NULL, case_checksum TEXT NOT NULL CHECK(length(case_checksum)=64),
 outcome_event_checksum TEXT NOT NULL CHECK(length(outcome_event_checksum)=64), payload_json TEXT NOT NULL,
 payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), created_at TEXT NOT NULL,
 UNIQUE(protocol_id,ordinal), UNIQUE(protocol_id,case_id)
);
CREATE TABLE IF NOT EXISTS calibration_arms (
 arm_id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES calibration_protocols(protocol_id),
 member_id TEXT NOT NULL REFERENCES calibration_cohort_members(member_id), arm_kind TEXT NOT NULL CHECK(arm_kind IN ('personalized','generic')),
 blind_label TEXT NOT NULL, request_json TEXT NOT NULL, request_checksum TEXT NOT NULL CHECK(length(request_checksum)=64),
 response_json TEXT, response_checksum TEXT, receipt_json TEXT, receipt_checksum TEXT, created_at TEXT NOT NULL,
 UNIQUE(protocol_id,member_id,arm_kind), UNIQUE(protocol_id,blind_label)
);
CREATE TABLE IF NOT EXISTS calibration_measurements (
 measurement_id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES calibration_protocols(protocol_id),
 arm_id TEXT NOT NULL REFERENCES calibration_arms(arm_id), metric_name TEXT NOT NULL,
 value_json TEXT NOT NULL, payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), observed_at TEXT NOT NULL,
 UNIQUE(arm_id,metric_name)
);
CREATE TABLE IF NOT EXISTS calibration_verdicts (
 verdict_id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL UNIQUE REFERENCES calibration_protocols(protocol_id),
 verdict_status TEXT NOT NULL CHECK(verdict_status IN ('PASS','FAIL','INCONCLUSIVE')),
 payload_json TEXT NOT NULL, payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibration_proposals (
 proposal_id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES calibration_protocols(protocol_id),
 verdict_id TEXT NOT NULL REFERENCES calibration_verdicts(verdict_id), proposal_status TEXT NOT NULL CHECK(proposal_status IN ('candidate','rejected','revoked','restored')),
 parent_version TEXT NOT NULL, parent_checksum TEXT NOT NULL CHECK(length(parent_checksum)=64),
 payload_json TEXT NOT NULL, payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibration_events (
 event_id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES calibration_protocols(protocol_id),
 sequence INTEGER NOT NULL CHECK(sequence>=1), event_type TEXT NOT NULL, previous_event_checksum TEXT NOT NULL,
 payload_json TEXT NOT NULL, payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), occurred_at TEXT NOT NULL,
 UNIQUE(protocol_id,sequence), CHECK(sequence>1 OR previous_event_checksum='GENESIS')
);
"""


def _triggers() -> str:
    return "\n".join(
        f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{action.lower()} BEFORE {action} ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"
        for table in TABLES for action in ("UPDATE", "DELETE")
    )


FULL_SCHEMA_SQL = SCHEMA_SQL + _triggers()


def inspect_schema(db_path: Path | str) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists(): return {"schema_state": "unapplied", "missing_tables": list(TABLES)}
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON"); con.execute("PRAGMA foreign_keys=ON")
    try:
        existing = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(set(TABLES)-existing)
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        triggers = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_calibration_%_no_%'").fetchone()[0]
        state = "partial" if missing else "applied"
        if not missing and (integrity != "ok" or fk or triggers != len(TABLES)*2): state = "invalid"
        return {"schema_state": state, "missing_tables": missing, "integrity": integrity,
                "foreign_key_violations": len(fk), "append_only_trigger_count": triggers}
    finally: con.close()


def migrate(db_path: Path | str, *, write: bool = False) -> dict[str, Any]:
    path = Path(db_path); before = inspect_schema(path)
    if not write: return {"dry_run": True, "would_create": before["missing_tables"], "before": before}
    if before["schema_state"] == "applied": return {"no_op": True, "after": before}
    if before["schema_state"] == "invalid": raise CalibrationSchemaError("calibration_schema_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect_rw(path, timeout=30)
    try:
        con.executescript("BEGIN IMMEDIATE;" + FULL_SCHEMA_SQL)
        if con.execute("PRAGMA foreign_key_check").fetchone(): raise CalibrationSchemaError("calibration_fk_failure")
        con.commit()
    except Exception: con.rollback(); raise
    finally: con.close()
    after = inspect_schema(path)
    if after["schema_state"] != "applied": raise CalibrationSchemaError("calibration_schema_postcheck_failed")
    return {"migrated": True, "after": after}


__all__ = ["CalibrationSchemaError", "FULL_SCHEMA_SQL", "REGISTRY_ID", "SCHEMA_VERSION", "TABLES", "inspect_schema", "migrate"]
