"""Neutral lifecycle schema + read-only lifecycle checks (OC-10).

``LIFECYCLE_SCHEMA_SQL`` / ``ensure_lifecycle_schema`` / ``lifecycle_status``
were relocated from ``application/knowledge/lifecycle_events.py`` so the
``application`` layer, the ``intelligence`` CLI acceptance gates and any other
consumer can share one canonical implementation without cross-package imports.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any


LIFECYCLE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_lifecycle_manifests (
    manifest_id TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL,
    manifest_checksum TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('proposed','reviewed','applied','rejected','rolled_back')),
    reviewer_id_hash TEXT,
    reviewed_at TEXT,
    actor_id TEXT,
    source_snapshot_id TEXT,
    created_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE TABLE IF NOT EXISTS knowledge_lifecycle_actions (
    action_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL REFERENCES knowledge_lifecycle_manifests(manifest_id),
    ordinal INTEGER NOT NULL,
    unit_id TEXT NOT NULL REFERENCES canonical_knowledge_units(canonical_unit_id),
    action TEXT NOT NULL CHECK(action IN ('supersede','conflict','correct','restore','deprecate')),
    expected_version INTEGER NOT NULL,
    expected_lifecycle TEXT NOT NULL,
    target_unit_id TEXT,
    reason TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    UNIQUE(manifest_id, ordinal),
    UNIQUE(manifest_id, unit_id)
);
CREATE TABLE IF NOT EXISTS knowledge_lifecycle_events (
    event_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL REFERENCES knowledge_lifecycle_manifests(manifest_id),
    action_id TEXT NOT NULL REFERENCES knowledge_lifecycle_actions(action_id),
    unit_id TEXT NOT NULL REFERENCES canonical_knowledge_units(canonical_unit_id),
    event_type TEXT NOT NULL,
    lifecycle_before TEXT NOT NULL,
    lifecycle_after TEXT NOT NULL,
    version_before INTEGER NOT NULL,
    version_after INTEGER NOT NULL,
    supersedes_before TEXT,
    supersedes_after TEXT,
    reason TEXT NOT NULL,
    reviewer_id_hash TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    rollback_of_event_id TEXT REFERENCES knowledge_lifecycle_events(event_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_unit_corrections (
    correction_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES knowledge_lifecycle_events(event_id),
    unit_id TEXT NOT NULL REFERENCES canonical_knowledge_units(canonical_unit_id),
    field_name TEXT NOT NULL CHECK(field_name IN ('question','answer')),
    before_hash TEXT NOT NULL,
    after_hash TEXT NOT NULL,
    before_value_json TEXT NOT NULL,
    after_value_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_unit ON knowledge_lifecycle_events(unit_id, created_at);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_manifest ON knowledge_lifecycle_events(manifest_id);
"""


def ensure_lifecycle_schema(con: sqlite3.Connection) -> None:
    # 既有库的 knowledge_lifecycle_actions 带着旧 CHECK（不含 'deprecate'），
    # SQLite 无法 ALTER CHECK，需要整表重建迁移。注意三点：
    # 1. DROP 被引用表在 foreign_keys=ON 下会因隐式 DELETE 违反 FK；
    # 2. RENAME 自 SQLite 3.25 起会改写其他表的 FK 引用文本，foreign_keys=OFF
    #    并不阻止——必须 legacy_alter_table=ON（实证：knowledge_unit_corrections
    #    的 FK 被改写到中间表名，DROP 中间表后留下悬空引用，doctor FK 检查 FAIL）；
    # 3. 迁移全程 foreign_keys=OFF 防 DROP 隐式 DELETE；events 表若已被改写
    #    到中间表名也一并重建。
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='knowledge_lifecycle_actions'"
    ).fetchone()
    needs_actions = row is not None and "'deprecate'" not in str(row[0])
    has_legacy_copy = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_lifecycle_actions_pre_deprecate'"
    ).fetchone() is not None
    ev = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='knowledge_lifecycle_events'"
    ).fetchone()
    needs_events = ev is not None and "knowledge_lifecycle_actions_pre_deprecate" in str(ev[0])
    if not (needs_actions or has_legacy_copy or needs_events):
        con.executescript(LIFECYCLE_SCHEMA_SQL)
        return
    fk_on = bool(int(con.execute("PRAGMA foreign_keys").fetchone()[0]))
    legacy = bool(int(con.execute("PRAGMA legacy_alter_table").fetchone()[0]))
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("PRAGMA legacy_alter_table=ON")
    try:
        if needs_actions:
            con.execute(
                "ALTER TABLE knowledge_lifecycle_actions RENAME TO knowledge_lifecycle_actions_pre_deprecate"
            )
        if needs_events:
            con.execute(
                "ALTER TABLE knowledge_lifecycle_events RENAME TO knowledge_lifecycle_events_pre_deprecate"
            )
        con.executescript(LIFECYCLE_SCHEMA_SQL)
        if needs_actions or has_legacy_copy:
            con.execute(
                "INSERT INTO knowledge_lifecycle_actions SELECT * FROM knowledge_lifecycle_actions_pre_deprecate"
            )
            con.execute("DROP TABLE knowledge_lifecycle_actions_pre_deprecate")
        if needs_events:
            con.execute(
                "INSERT INTO knowledge_lifecycle_events SELECT * FROM knowledge_lifecycle_events_pre_deprecate"
            )
            con.execute("DROP TABLE knowledge_lifecycle_events_pre_deprecate")
        con.commit()  # 结束隐式事务，否则下面的 PRAGMA 在事务内静默无效
    finally:
        con.execute(f"PRAGMA legacy_alter_table={'ON' if legacy else 'OFF'}")
        con.execute(f"PRAGMA foreign_keys={'ON' if fk_on else 'OFF'}")


def lifecycle_status(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"ok": False, "checks": {"db_exists": False}}
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"knowledge_lifecycle_manifests", "knowledge_lifecycle_actions", "knowledge_lifecycle_events", "knowledge_unit_corrections"}
        schema_ready = required.issubset(tables)
        manifests = events = 0
        event_types: dict[str, int] = {}
        if schema_ready:
            manifests = int(con.execute("SELECT COUNT(*) FROM knowledge_lifecycle_manifests WHERE status='applied'").fetchone()[0])
            events = int(con.execute("SELECT COUNT(*) FROM knowledge_lifecycle_events").fetchone()[0])
            event_types = {str(row[0]): int(row[1]) for row in con.execute("SELECT event_type,COUNT(*) FROM knowledge_lifecycle_events GROUP BY event_type")}
        checks = {
            "schema_ready": schema_ready,
            "reviewed_manifest_applied": manifests > 0,
            "event_ledger_nonempty": events > 0,
            "supersede_event": event_types.get("supersede", 0) > 0,
            "conflict_event": event_types.get("conflict", 0) > 0,
            "correction_event": event_types.get("correct", 0) > 0,
            "restore_event": event_types.get("restore", 0) > 0,
        }
        return {"ok": all(checks.values()), "checks": checks, "applied_manifests": manifests, "event_count": events, "event_types": event_types}
    finally:
        con.close()


__all__ = ["LIFECYCLE_SCHEMA_SQL", "ensure_lifecycle_schema", "lifecycle_status"]
