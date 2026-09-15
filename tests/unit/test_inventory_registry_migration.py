"""Unified full/delta inventory parent registry migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import (
    SCHEMA_SQL,
    inspect_inventory_registry,
    migrate_inventory_registry,
)


OLD_RUN_ITEMS_SQL = """
CREATE TABLE knowledge_run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES knowledge_build_runs(run_id),
    inventory_id TEXT NOT NULL REFERENCES knowledge_inventory(inventory_id),
    position INTEGER NOT NULL,
    evidence_ref TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','in_flight','retryable','succeeded','abstained','terminal_failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_started_at TEXT,
    last_error_class TEXT,
    cache_key TEXT,
    response_hash TEXT,
    unit_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    UNIQUE(run_id, position)
);
"""

OLD_GATES_SQL = """
CREATE TABLE knowledge_extraction_gates (
    gate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES knowledge_build_runs(run_id),
    inventory_id TEXT NOT NULL REFERENCES knowledge_inventory(inventory_id),
    gate_status TEXT NOT NULL
        CHECK(gate_status IN ('passed','failed','awaiting_pilot_threshold')),
    gate_json TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);
"""


def _insert_build_run(con: sqlite3.Connection, run_id: str = "ir_test") -> None:
    con.execute(
        "INSERT INTO knowledge_build_runs "
        "(run_id, run_type, generated_at, source_build_id, input_hash, "
        "schema_version, status) VALUES (?,?,?,?,?,?,?)",
        (run_id, "incremental", "2026-01-01", "src", "hash", "v1", "pending"),
    )


def _build_old_fk_fixture(db: Path) -> None:
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("DROP TABLE knowledge_run_items")
    con.execute("DROP TABLE knowledge_extraction_gates")
    con.execute(OLD_RUN_ITEMS_SQL)
    con.execute(OLD_GATES_SQL)
    _insert_build_run(con)
    con.execute(
        "INSERT INTO knowledge_inventory VALUES "
        "('inv_full','2026-01-01','canon','before',1,1,'dataset',NULL,NULL,'{}')"
    )
    con.execute(
        "INSERT INTO knowledge_delta_inventories VALUES "
        "('di_delta','before','after','ordered',1,0,0,'model','p1','s1','cfg','2026-01-02')"
    )
    con.execute(
        "INSERT INTO knowledge_run_items "
        "(run_id, inventory_id, position, evidence_ref) "
        "VALUES ('ir_test','di_delta',0,'cm1')"
    )
    con.execute(
        "INSERT INTO knowledge_extraction_gates VALUES "
        "('gate1','ir_test','di_delta','passed','{}','2026-01-03')"
    )
    con.commit()
    con.close()


def test_fresh_schema_registers_full_and_delta_inventories(tmp_path: Path) -> None:
    db = tmp_path / "fresh.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    con.execute("PRAGMA foreign_keys = ON")
    _insert_build_run(con)
    con.execute(
        "INSERT INTO knowledge_inventory VALUES "
        "('inv_full','2026-01-01','canon','before',1,1,'dataset',NULL,NULL,'{}')"
    )
    con.execute(
        "INSERT INTO knowledge_delta_inventories VALUES "
        "('di_delta','before','after','ordered',1,0,0,'model','p1','s1','cfg','2026-01-02')"
    )
    con.execute(
        "INSERT INTO knowledge_run_items "
        "(run_id, inventory_id, position, evidence_ref) "
        "VALUES ('ir_test','di_delta',0,'cm1')"
    )
    con.execute(
        "INSERT INTO knowledge_extraction_gates VALUES "
        "('gate1','ir_test','di_delta','passed','{}','2026-01-03')"
    )
    con.commit()
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    kinds = dict(
        con.execute(
            "SELECT inventory_id, inventory_kind FROM knowledge_inventory_registry"
        ).fetchall()
    )
    con.close()
    assert kinds == {"inv_full": "full", "di_delta": "delta"}


def test_migration_rebuilds_fk_targets_and_preserves_rows(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite"
    backup = tmp_path / "backup" / "old.pre-inventory-registry.sqlite"
    _build_old_fk_fixture(db)

    before = inspect_inventory_registry(db)
    assert before["foreign_key_violations_total"] == 2
    assert before["run_items_inventory_fk_target"] == "knowledge_inventory"
    assert before["gates_inventory_fk_target"] == "knowledge_inventory"

    dry = migrate_inventory_registry(db, write=False, backup_path=backup)
    assert dry["dry_run"] is True
    assert not backup.exists()

    result = migrate_inventory_registry(db, write=True, backup_path=backup)
    assert result["migrated"] is True
    assert backup.exists()
    assert result["backup"]["integrity_check"] == "ok"
    assert result["rebuilt"]["knowledge_run_items"] == {"before": 1, "after": 1}
    assert result["rebuilt"]["knowledge_extraction_gates"] == {
        "before": 1,
        "after": 1,
    }

    after = inspect_inventory_registry(db)
    assert after["healthy"] is True
    assert after["foreign_key_violations_total"] == 0
    assert after["counts"]["run_items"] == 1
    assert after["counts"]["gates"] == 1

    backup_state = inspect_inventory_registry(backup)
    assert backup_state["foreign_key_violations_total"] == 2
    assert backup_state["run_items_inventory_fk_target"] == "knowledge_inventory"

    repeated = migrate_inventory_registry(db, write=True)
    assert repeated["no_op"] is True
