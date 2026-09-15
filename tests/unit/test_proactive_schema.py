from __future__ import annotations

import sqlite3

import pytest

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL, inspect
from personal_knowledge.intelligence.proactive.schema import CANONICAL_DOMAINS


TABLES = {
    "proactive_runs",
    "proactive_coordination_items",
    "proactive_candidates",
    "proactive_candidate_support",
    "proactive_evaluations",
    "proactive_control_events",
    "proactive_surface_events",
}


def test_schema_has_exactly_eight_domains_and_seven_additive_tables(tmp_path) -> None:
    assert CANONICAL_DOMAINS == (
        "learning", "career", "project", "health", "finance",
        "relationship", "time", "energy",
    )
    con = sqlite3.connect(tmp_path / "schema.sqlite")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA_SQL)
    names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names
    con.executescript(SCHEMA_SQL)
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()


def test_migration_inspect_tracks_all_proactive_tables(tmp_path) -> None:
    db = tmp_path / "schema.sqlite"
    sqlite3.connect(db).close()
    assert TABLES <= set(inspect(db)["missing_tables"])

    con = sqlite3.connect(db)
    con.executescript(SCHEMA_SQL)
    con.close()
    assert not (TABLES & set(inspect(db)["missing_tables"]))


def test_every_proactive_table_is_immutable(tmp_path) -> None:
    con = sqlite3.connect(tmp_path / "schema.sqlite")
    con.executescript(SCHEMA_SQL)
    triggers = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    for table in TABLES:
        assert f"trg_{table}_immutable_update" in triggers
        assert f"trg_{table}_immutable_delete" in triggers
    con.close()


def test_unknown_domain_fails_closed() -> None:
    from personal_knowledge.intelligence.proactive.schema import canonical_domain

    assert canonical_domain("work") == "career"
    with pytest.raises(ValueError, match="unknown_domain"):
        canonical_domain("private_guess")
