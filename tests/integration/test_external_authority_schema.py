from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.external_context.migrate import TABLES, migrate


def _seed(db: Path) -> None:
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    source_id, definition_checksum = con.execute(
        "SELECT source_id,definition_checksum FROM external_source_registry ORDER BY source_id LIMIT 1"
    ).fetchone()
    sixty_four = "a" * 64
    con.execute(
        "INSERT INTO external_import_runs VALUES (?,?,?,?,?,?,?,?)",
        ("eir_1", source_id, definition_checksum, "{}", sixty_four, "published", "now", "now"),
    )
    con.execute(
        "INSERT INTO external_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("eo_1", "eir_1", source_id, "release", '"3.14"', "now", "now", None, "now", "now", "global", "b" * 64),
    )
    con.execute(
        "INSERT INTO external_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ef_1", "eir_1", "python", "latest_release", '"3.14"', "now", None, "global", .95, .9, "current", "c" * 64),
    )
    con.execute(
        "INSERT INTO external_fact_support VALUES (?,?,?,?)",
        ("efs_1", "ef_1", "eo_1", "d" * 64),
    )
    con.execute(
        "INSERT INTO external_lifecycle_events VALUES (?,?,?,?,?,?,?,?)",
        ("ele_1", "ef_1", 1, "created", "GENESIS", "{}", "e" * 64, "now"),
    )
    con.commit()
    con.close()


def test_dry_run_does_not_create_or_change_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    result = migrate(missing)
    assert result["dry_run"] is True
    assert not missing.exists()
    db = tmp_path / "empty.sqlite"
    sqlite3.connect(db).close()
    before = db.read_bytes()
    migrate(db)
    assert db.read_bytes() == before


def test_write_is_idempotent_fk_clean_and_projects_exact_allowlist(tmp_path: Path) -> None:
    db = tmp_path / "external.sqlite"
    first = migrate(db, write=True)
    second = migrate(db, write=True)
    assert first["migrated"] is True
    assert second["no_op"] is True
    con = sqlite3.connect(db)
    assert {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= set(TABLES)
    assert con.execute("SELECT COUNT(*) FROM external_source_registry").fetchone()[0] == 2
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()


def test_authority_rejects_updates_deletes_duplicates_and_dangling_support(tmp_path: Path) -> None:
    db = tmp_path / "external.sqlite"
    migrate(db, write=True)
    _seed(db)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    for table in TABLES:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute(f"UPDATE {table} SET rowid=rowid WHERE rowid=(SELECT MIN(rowid) FROM {table})")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute(f"DELETE FROM {table} WHERE rowid=(SELECT MIN(rowid) FROM {table})")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO external_fact_support VALUES ('bad','missing','missing',?)", ("f" * 64,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO external_lifecycle_events VALUES (?,?,?,?,?,?,?,?)",
            ("duplicate", "ef_1", 1, "created", "GENESIS", "{}", "f" * 64, "now"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO external_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("bad_quality", "eir_1", "python", "bad", '"x"', "now", None, "global", 1.1, .9, "current", "f" * 64),
        )
    con.close()
