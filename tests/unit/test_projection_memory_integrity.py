"""Retrieval results stay bound to the claim/fact that produced the match."""
from __future__ import annotations

import sqlite3
import pytest

from personal_knowledge.retrieval.layers.semantic_card import SemanticCardLayer
from personal_knowledge.retrieval.layers.base import SearchState


def _state() -> SearchState:
    return SearchState(
        top_k=3, source=None, include_evidence=False, policy="layered",
        pad_allowed=False, snapshot_enforced=False, serving=None,
    )


def test_semantic_card_preserves_matched_fact_evidence(tmp_path, monkeypatch) -> None:
    db = tmp_path / "cards.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE session_cards(session_id TEXT PRIMARY KEY, purpose TEXT,
          summary_md TEXT, card_json TEXT);
        CREATE TABLE ku_facts(fact_key TEXT PRIMARY KEY, session_id TEXT,
          fact TEXT, evidence_refs TEXT, confidence TEXT, valid_from TEXT,
          fact_key_sort TEXT, status TEXT);
        """
    )
    con.execute("INSERT INTO session_cards VALUES (?,?,?,?)", ("s1", "记忆检索", "摘要", "{}"))
    con.execute(
        "INSERT INTO ku_facts VALUES (?,?,?,?,?,?,?,?)",
        ("f1", "s1", "记忆检索事实", '["v2|cm|e1", "v2|cm|e2"]', "high", "2026", "f1", "active"),
    )
    con.commit(); con.close()

    layer = SemanticCardLayer()
    import personal_knowledge.retrieval._constants as constants
    import personal_knowledge.retrieval.semantic_cards as cards
    monkeypatch.setattr(constants, "CARDS_DB", db)
    monkeypatch.setattr(cards, "CARDS_DB_PATH", db)
    rows = layer.retrieve("记忆检索", _state())
    assert rows
    assert rows[0]["source_message_ref"] == "v2|cm|e1"
    assert rows[0]["evidence_refs"] == ["v2|cm|e1", "v2|cm|e2"]
    assert rows[0]["confidence"] == 0.9
    assert rows[0]["lifecycle"] == "current"


def test_materialized_wiki_preserves_resolvable_claim_refs(tmp_path, monkeypatch):
    from tools.semantic.materialize_wiki import build_page_body, write_topic_page
    from personal_knowledge.retrieval.layers.wiki_page import search_wiki_pages

    store = tmp_path / "wiki.sqlite"
    body = build_page_body("PowerShell", [{
        "unit_id": "ku-shell", "unit_type": "preference", "subject": "shell",
        "question": "Which shell?", "answer": "Use PowerShell", "confidence": 0.9,
        "lifecycle": "current", "version": 1, "source_session_id": "s1",
    }], {"ku-shell": ["v2|cm|e1", "v2|cm|e2"]})
    authority = tmp_path / "authority.sqlite"
    with sqlite3.connect(authority) as con:
        con.executescript("CREATE TABLE knowledge_units(unit_id TEXT,unit_type TEXT,subject TEXT,question TEXT,answer TEXT,confidence REAL,lifecycle TEXT,status TEXT,version INTEGER); CREATE TABLE knowledge_unit_evidence(unit_id TEXT,evidence_ref TEXT);")
        con.execute("INSERT INTO knowledge_units VALUES(?,?,?,?,?,?,?,?,?)", ("ku-shell", "preference", "shell", "Which shell?", "Use PowerShell", .9, "current", "current", 1))
        con.executemany("INSERT INTO knowledge_unit_evidence VALUES(?,?)", [("ku-shell", "v2|cm|e1"), ("ku-shell", "v2|cm|e2")])
    import personal_knowledge.retrieval._constants as constants
    monkeypatch.setattr(constants, "UNIFIED_DB", authority)
    write_topic_page(store, "PowerShell", body)
    rows = search_wiki_pages("PowerShell", store=store, limit=3)
    assert rows
    assert rows[0]["source_message_ref"] == "v2|cm|e1"
    assert rows[0]["evidence_refs"] == ["v2|cm|e1", "v2|cm|e2"]

    before = authority.read_bytes()
    assert search_wiki_pages("PowerShell", store=store, limit=3)
    assert authority.read_bytes() == before
    with sqlite3.connect(authority) as con:
        con.execute("UPDATE knowledge_units SET lifecycle='superseded'")
    assert search_wiki_pages("PowerShell", store=store, limit=3) == []
