import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.application.conversation.authority_publish import authority_publish


def _seed(db: Path) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE preexisting (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        con.execute("INSERT INTO preexisting (id, label) VALUES (?, ?)", (1, "alpha"))
        con.commit()
    finally:
        con.close()


def _rows(db: Path, sql: str):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _table_exists(db: Path, name: str) -> bool:
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None
    finally:
        con.close()


def test_blocking_gate_leaves_authority_untouched(tmp_path):
    db = tmp_path / "authority.sqlite"
    _seed(db)
    before = _rows(db, "SELECT id, label FROM preexisting")
    assert before == [(1, "alpha")]

    def apply(con):
        con.execute("CREATE TABLE new_table (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        con.execute("INSERT INTO new_table (id, label) VALUES (?, ?)", (7, "seven"))

    report = authority_publish(db, apply, gates=[lambda con: ["boom"]])

    assert report.published is False
    assert report.blocked_reasons == ("boom",)
    assert _table_exists(db, "new_table") is False
    assert _rows(db, "SELECT id, label FROM preexisting") == before


def test_passing_gates_publish_new_data(tmp_path):
    db = tmp_path / "authority.sqlite"
    _seed(db)

    def apply(con):
        con.execute("CREATE TABLE new_table (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        con.execute("INSERT INTO new_table (id, label) VALUES (?, ?)", (7, "seven"))

    report = authority_publish(db, apply, gates=[])

    assert report.published is True
    assert report.blocked_reasons == ()
    assert _table_exists(db, "new_table") is True
    assert _rows(db, "SELECT id, label FROM new_table") == [(7, "seven")]


def test_apply_exception_rolls_back_and_propagates(tmp_path):
    db = tmp_path / "authority.sqlite"
    _seed(db)

    def apply(con):
        con.execute("CREATE TABLE doomed (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        con.execute("INSERT INTO doomed (id, label) VALUES (?, ?)", (9, "nine"))
        raise RuntimeError("apply failed")

    with pytest.raises(RuntimeError, match="apply failed"):
        authority_publish(db, apply, gates=[])

    assert _table_exists(db, "doomed") is False
    assert _rows(db, "SELECT id, label FROM preexisting") == [(1, "alpha")]


def test_publish_keeps_backup_of_pre_publish_state(tmp_path):
    db = tmp_path / "authority.sqlite"
    _seed(db)
    backup = tmp_path / "authority.backup.sqlite"

    def apply(con):
        con.execute("CREATE TABLE new_table (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        con.execute("INSERT INTO new_table (id, label) VALUES (?, ?)", (7, "seven"))

    report = authority_publish(db, apply, gates=[], backup=True)

    assert report.published is True
    assert backup.exists() is True
    assert _table_exists(backup, "preexisting") is True
    assert _rows(backup, "SELECT id, label FROM preexisting") == [(1, "alpha")]
    assert _table_exists(backup, "new_table") is False
    assert _table_exists(db, "new_table") is True


def test_publish_without_existing_db_creates_no_backup(tmp_path):
    db = tmp_path / "fresh.sqlite"
    assert db.exists() is False

    def apply(con):
        con.execute("CREATE TABLE new_table (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        con.execute("INSERT INTO new_table (id, label) VALUES (?, ?)", (7, "seven"))

    report = authority_publish(db, apply, gates=[], backup=True)

    assert report.published is True
    assert _table_exists(db, "new_table") is True
    assert (tmp_path / "fresh.backup.sqlite").exists() is False


def test_blocked_gate_leaves_no_backup_behind(tmp_path):
    db = tmp_path / "authority.sqlite"
    _seed(db)

    def apply(con):
        con.execute("CREATE TABLE new_table (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        con.execute("INSERT INTO new_table (id, label) VALUES (?, ?)", (7, "seven"))

    report = authority_publish(db, apply, gates=[lambda con: ["boom"]], backup=True)

    assert report.published is False
    assert report.blocked_reasons == ("boom",)
    assert (tmp_path / "authority.backup.sqlite").exists() is False
    assert _table_exists(db, "new_table") is False
