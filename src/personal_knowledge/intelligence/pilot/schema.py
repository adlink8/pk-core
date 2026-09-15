"""Independent append-only schema for the low-risk project pilot."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from personal_knowledge.core.sqlite import connect_rw
from personal_knowledge.intelligence.analysis.schema import canonical_json, checksum


REGISTRY_ID = "a.project_pilot"
SCHEMA_VERSION = "project_pilot_v1"
TABLES = ("pilot_cases", "pilot_recommendations", "pilot_protocols", "pilot_events")


class PilotSchemaError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def require_checksum(name: str, value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise PilotSchemaError("invalid_checksum", name)


@dataclass(frozen=True)
class ProjectCase:
    case_id: str
    source_run_id: str
    source_candidate_id: str
    source_run_checksum: str
    source_candidate_checksum: str
    binding_hash: str
    personal_snapshot_id: str
    personal_snapshot_hash: str
    external_snapshot_id: str
    external_snapshot_hash: str
    request_checksum: str
    response_checksum: str
    goal: str
    constraints: tuple[str, ...]
    weights: Mapping[str, float]
    risk_budget: str
    no_action_baseline: Mapping[str, Any]
    alternatives: tuple[Mapping[str, Any], ...]
    stop_conditions: tuple[str, ...]
    confirmation_event_id: str
    payload_checksum: str

    def __post_init__(self) -> None:
        if self.risk_budget != "low" or not self.goal.strip() or not self.constraints:
            raise PilotSchemaError("case_policy_invalid")
        if not self.no_action_baseline or not self.alternatives or not self.stop_conditions:
            raise PilotSchemaError("case_structure_incomplete")
        if abs(sum(float(item) for item in self.weights.values()) - 1.0) > 1e-9:
            raise PilotSchemaError("weights_not_normalized")
        for name in (
            "source_run_checksum", "source_candidate_checksum", "binding_hash",
            "personal_snapshot_hash", "external_snapshot_hash", "request_checksum",
            "response_checksum", "payload_checksum",
        ):
            require_checksum(name, str(getattr(self, name)))


@dataclass(frozen=True)
class RecommendationCandidate:
    recommendation_id: str
    case_id: str
    option_id: str
    option: Mapping[str, Any]
    status: str
    reason_codes: tuple[str, ...]
    payload_checksum: str

    def __post_init__(self) -> None:
        if self.status not in {"candidate", "abstain"}:
            raise PilotSchemaError("recommendation_status_invalid")
        if self.status == "candidate" and (not self.option_id or not self.option):
            raise PilotSchemaError("recommendation_option_required")
        if self.status == "abstain" and not self.reason_codes:
            raise PilotSchemaError("recommendation_abstain_reason_required")
        require_checksum("payload_checksum", self.payload_checksum)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pilot_cases (
    case_id TEXT PRIMARY KEY, registry_id TEXT NOT NULL CHECK(registry_id='a.project_pilot'),
    source_run_id TEXT NOT NULL, source_candidate_id TEXT NOT NULL,
    source_run_checksum TEXT NOT NULL CHECK(length(source_run_checksum)=64),
    source_candidate_checksum TEXT NOT NULL CHECK(length(source_candidate_checksum)=64),
    binding_hash TEXT NOT NULL CHECK(length(binding_hash)=64),
    personal_snapshot_id TEXT NOT NULL, personal_snapshot_hash TEXT NOT NULL CHECK(length(personal_snapshot_hash)=64),
    external_snapshot_id TEXT NOT NULL, external_snapshot_hash TEXT NOT NULL CHECK(length(external_snapshot_hash)=64),
    request_checksum TEXT NOT NULL CHECK(length(request_checksum)=64),
    response_checksum TEXT NOT NULL CHECK(length(response_checksum)=64),
    confirmation_event_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), created_at TEXT NOT NULL,
    UNIQUE(source_candidate_id,confirmation_event_id)
);
CREATE TABLE IF NOT EXISTS pilot_recommendations (
    recommendation_id TEXT PRIMARY KEY, case_id TEXT NOT NULL UNIQUE REFERENCES pilot_cases(case_id),
    option_id TEXT NOT NULL, recommendation_status TEXT NOT NULL CHECK(recommendation_status IN ('candidate','abstain')),
    payload_json TEXT NOT NULL, payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_protocols (
    protocol_id TEXT PRIMARY KEY, case_id TEXT NOT NULL UNIQUE REFERENCES pilot_cases(case_id),
    protocol_status TEXT NOT NULL CHECK(protocol_status IN ('pending','preregistered','closed')),
    payload_json TEXT NOT NULL, payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_events (
    event_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES pilot_cases(case_id),
    sequence INTEGER NOT NULL CHECK(sequence>=1), event_type TEXT NOT NULL,
    previous_event_checksum TEXT NOT NULL, payload_json TEXT NOT NULL,
    payload_checksum TEXT NOT NULL UNIQUE CHECK(length(payload_checksum)=64), occurred_at TEXT NOT NULL,
    UNIQUE(case_id,sequence), CHECK(sequence>1 OR previous_event_checksum='GENESIS')
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
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        existing = {str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(set(TABLES) - existing)
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        triggers = int(con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_pilot_%_no_%'"
        ).fetchone()[0])
        state = "partial" if missing else "applied"
        if not missing and (integrity != "ok" or fk or triggers != len(TABLES) * 2):
            state = "invalid"
        return {"db_exists": True, "schema_state": state, "missing_tables": missing,
                "integrity": integrity, "foreign_key_violations": len(fk),
                "append_only_trigger_count": triggers}
    finally:
        con.close()


def migrate(db_path: Path | str, *, write: bool = False) -> dict[str, Any]:
    path = Path(db_path)
    before = inspect_schema(path)
    if not write:
        return {"write": False, "dry_run": True, "would_create": before["missing_tables"], "before": before}
    if before["schema_state"] == "applied":
        return {"write": True, "migrated": False, "no_op": True, "after": before}
    if before["schema_state"] == "invalid":
        raise PilotSchemaError("pilot_authority_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect_rw(path, timeout=30)
    try:
        con.executescript("BEGIN IMMEDIATE;\n" + FULL_SCHEMA_SQL)
        if con.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError("pilot schema foreign key failure")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    after = inspect_schema(path)
    if after["schema_state"] != "applied":
        raise PilotSchemaError("pilot_schema_post_validation_failed")
    return {"write": True, "migrated": True, "no_op": False, "after": after}


def payload_checksum(value: Mapping[str, Any]) -> str:
    return checksum(value)


__all__ = [
    "FULL_SCHEMA_SQL", "ProjectCase", "RecommendationCandidate", "PilotSchemaError",
    "REGISTRY_ID", "SCHEMA_VERSION", "TABLES", "canonical_json", "inspect_schema",
    "migrate", "payload_checksum", "require_checksum",
]
