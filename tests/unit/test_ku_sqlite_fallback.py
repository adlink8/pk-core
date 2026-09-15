"""Empty Chroma KU collection falls back to current SQLite units (read-only).

Public seam: ``search_knowledge_units`` knowledge_unit layer.
Does not rewrite ``knowledge_index_active.txt`` or promote a collection.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.application.knowledge.schema_ddl import SCHEMA_SQL
from personal_knowledge.retrieval.semantic_search import search_knowledge_units

UNIQUE = "KuSqliteUniqueZXQ"


def _write_ku_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA_SQL)
    con.execute(
        "INSERT INTO knowledge_build_runs VALUES "
        "('run1','extraction','2026-01-01',NULL,'h','v1','v1','m',NULL,NULL,NULL,NULL,'current',NULL,NULL)"
    )
    con.execute(
        "INSERT INTO knowledge_units (unit_id, run_id, unit_type, subject, question, answer, "
        "confidence, evidence_quote, lifecycle, source_message_ref, source_agent, "
        "evidence_scope, status, version, created_at, supersedes_id) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "v1|ku-sqlite-unique",
            "run1",
            "technical_conclusion",
            UNIQUE,
            f"{UNIQUE} 是什么",
            f"{UNIQUE} 只存在于 sqlite current KU",
            0.9,
            "quote",
            "current",
            "",
            "test",
            "user",
            "current",
            1,
            "2026-09-01T00:00:00Z",
            None,
        ),
    )
    con.commit()
    con.close()


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "personal_system.sqlite"
    pointer = tmp_path / "knowledge_index_active.txt"
    pointer.write_text("knowledge_units_empty_kg_test\n", encoding="utf-8")
    _write_ku_db(db)
    import personal_knowledge.retrieval._constants as constants

    monkeypatch.setattr(constants, "UNIFIED_DB", db)
    monkeypatch.setattr(constants, "DB_DIR", tmp_path)
    monkeypatch.setattr(constants, "WIKI_PROJECTION_DB", tmp_path / "absent-wiki.sqlite")
    monkeypatch.setattr(constants, "CARDS_DB", tmp_path / "absent-cards.sqlite")
    monkeypatch.setattr(
        "personal_knowledge.retrieval.semantic_search._read_knowledge_active_collection",
        lambda: "",
    )
    monkeypatch.setattr(
        "personal_knowledge.retrieval.semantic_search._search_dialogue_canonical_messages",
        lambda *a, **k: [],
    )
    return db


def test_sqlite_current_ku_used_when_chroma_empty(isolated: Path) -> None:
    before = hashlib.sha256(isolated.read_bytes()).hexdigest()
    result = search_knowledge_units(
        UNIQUE, top_k=5, fallback_policy="layered", allow_legacy_pad=False
    )
    after = hashlib.sha256(isolated.read_bytes()).hexdigest()
    assert after == before
    assert result["results"], result
    hit = next(
        item for item in result["results"] if item.get("retrieval_unit") == "knowledge_unit"
    )
    assert UNIQUE in hit["subject"]
    assert result["telemetry"]["first_contributing_layer"] == "knowledge_unit"


def test_active_pointer_file_not_rewritten(
    isolated: Path, tmp_path: Path
) -> None:
    pointer = tmp_path / "knowledge_index_active.txt"
    before = pointer.read_text(encoding="utf-8")
    search_knowledge_units(
        UNIQUE, top_k=3, fallback_policy="layered", allow_legacy_pad=False
    )
    assert pointer.read_text(encoding="utf-8") == before
