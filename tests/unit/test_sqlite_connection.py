from __future__ import annotations

import sqlite3

import pytest

from personal_knowledge.core.sqlite import assert_foreign_key_integrity, connect_rw


def test_connect_rw_enables_foreign_keys_and_rejects_orphans(tmp_path) -> None:
    con = connect_rw(tmp_path / "policy.sqlite")
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    con.executescript(
        "CREATE TABLE parent (id TEXT PRIMARY KEY);"
        "CREATE TABLE child (parent_id TEXT REFERENCES parent(id));"
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO child(parent_id) VALUES ('missing')")
    con.close()


def test_integrity_gate_rejects_preexisting_violation(tmp_path) -> None:
    db = tmp_path / "legacy.sqlite"
    raw = sqlite3.connect(db)
    raw.executescript(
        "PRAGMA foreign_keys = OFF;"
        "CREATE TABLE parent (id TEXT PRIMARY KEY);"
        "CREATE TABLE child (parent_id TEXT REFERENCES parent(id));"
        "INSERT INTO child(parent_id) VALUES ('missing');"
    )
    raw.commit()
    raw.close()

    con = connect_rw(db)
    with pytest.raises(RuntimeError, match="foreign key check failed before publish"):
        assert_foreign_key_integrity(con)
    con.close()
