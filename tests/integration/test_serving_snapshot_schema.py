from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import (
    SCHEMA_SQL,
    inspect,
    migrate,
    plan_serving_bootstrap,
)


def _seed(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO artifact_registry_entries VALUES (?,?,?,?,?,?)",
        ("s.knowledge_unit", "S", "canonical_knowledge", "R4", "definition", "2026-07-17"),
    )
    con.execute(
        "INSERT INTO artifact_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("av1", "s.knowledge_unit", "v1", "abc", "sqlite_table", "canonical_knowledge_units", "published", "R4", None, None, "{}", "2026-07-17"),
    )
    con.execute(
        "INSERT INTO serving_snapshots VALUES (?,?,?,?,?,?,?)",
        ("ss1", json.dumps({"id": "ss1"}), "manifest", "validated", "gate1", "2026-07-17", "2026-07-17"),
    )
    con.execute("INSERT INTO serving_snapshot_members VALUES (?,?,?,NULL)", ("ss1", "canonical_knowledge", "av1"))
    con.execute("UPDATE serving_authority SET active_snapshot_id='ss1', activated_at='2026-07-17' WHERE singleton_id=1")
    con.commit()


def test_schema_is_idempotent_and_fk_clean(tmp_path: Path) -> None:
    db = tmp_path / "serving.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    con.executescript(SCHEMA_SQL)
    con.execute("PRAGMA foreign_keys=ON")
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("SELECT COUNT(*) FROM serving_authority").fetchone()[0] == 1
    con.close()


def test_active_snapshot_members_and_artifact_versions_are_immutable(tmp_path: Path) -> None:
    db = tmp_path / "serving.sqlite"
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA_SQL)
    _seed(con)
    with pytest.raises(sqlite3.IntegrityError, match="active snapshot members"):
        con.execute("UPDATE serving_snapshot_members SET serving_role='other' WHERE snapshot_id='ss1'")
    with pytest.raises(sqlite3.IntegrityError, match="artifact versions are immutable"):
        con.execute("UPDATE artifact_versions SET checksum='changed' WHERE artifact_version_id='av1'")
    con.close()


def test_constraints_reject_dangling_members_and_duplicate_roles(tmp_path: Path) -> None:
    db = tmp_path / "serving.sqlite"
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA_SQL)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO serving_snapshot_members VALUES ('missing','role','missing',NULL)")
    _seed(con)
    con.execute("INSERT INTO artifact_registry_entries VALUES ('r.other','R','other','R4','d','now')")
    con.execute("INSERT INTO artifact_versions VALUES ('av2','r.other','v1','def','chroma_collection','c','published','R4',NULL,NULL,'{}','now')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO serving_snapshot_members VALUES ('ss1','canonical_knowledge','av2',NULL)")
    con.close()


def test_empty_hashes_are_rejected(tmp_path: Path) -> None:
    db = tmp_path / "serving.sqlite"
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA_SQL)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO artifact_registry_entries VALUES ('d.bad','D','bad','R4','','now')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO serving_snapshots VALUES ('ss','','','draft',NULL,'now',NULL)")
    con.close()


def test_migrate_dry_run_writes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "empty.sqlite"
    sqlite3.connect(db).close()
    before = db.read_bytes()
    result = migrate(db, write=False)
    assert result["dry_run"] is True
    assert db.read_bytes() == before
    assert "serving_snapshots" in result["would_create"]


def test_bootstrap_plan_is_draft_only_and_read_only(tmp_path: Path) -> None:
    db = tmp_path / "serving.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    con.close()
    before = db.read_bytes()
    result = plan_serving_bootstrap(db)
    assert result["active"] is False
    assert result["mode"] == "draft_only"
    assert result["missing_proofs"] == ["exactly_one_active_knowledge_index"]
    assert db.read_bytes() == before
