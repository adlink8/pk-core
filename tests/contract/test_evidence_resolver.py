from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL
from personal_knowledge.retrieval.evidence import EvidenceResolver


def _fixture(tmp_path: Path) -> EvidenceResolver:
    unified, conv, google = tmp_path / "u.sqlite", tmp_path / "c.sqlite", tmp_path / "g.sqlite"
    con = sqlite3.connect(unified); con.executescript(SCHEMA_SQL)
    con.execute("INSERT INTO knowledge_build_runs (run_id,run_type,generated_at,input_hash,schema_version,status) VALUES ('run','merge','now','h','v1','current')")
    con.execute("INSERT INTO canonical_knowledge_units (canonical_unit_id,unit_type,subject,question,answer,confidence,run_id,created_at) VALUES ('ku1','personal_fact','s','q','answer',1,'run','now')")
    con.commit(); con.close()
    con = sqlite3.connect(conv)
    con.execute("CREATE TABLE canonical_sessions(canonical_session_id TEXT PRIMARY KEY,evidence_eligible INTEGER)")
    con.execute("CREATE TABLE canonical_messages(canonical_message_id TEXT PRIMARY KEY,canonical_session_id TEXT,role TEXT,content TEXT,evidence_scope TEXT,is_system INTEGER)")
    con.execute("INSERT INTO canonical_sessions VALUES ('s1',1),('s2',0)")
    con.execute("INSERT INTO canonical_messages VALUES ('cm|ok','s1','user','safe','user',0),('cm|secret','s2','system','secret body','system',1)")
    con.commit(); con.close()
    con = sqlite3.connect(google)
    con.execute("CREATE TABLE normalized_events(event_id TEXT PRIMARY KEY,title TEXT,privacy_tier TEXT)")
    con.execute("INSERT INTO normalized_events VALUES ('g|ok','topic','R4'),('g|blocked','private','blocked')")
    con.commit(); con.close()
    return EvidenceResolver(unified_db=unified, conversation_db=conv, google_db=google)


def test_typed_refs_resolve_with_metadata_only_by_default(tmp_path: Path) -> None:
    r = _fixture(tmp_path)
    assert r.resolve("ku1")["status"] == "ok"
    msg = r.resolve("cm|ok")
    assert msg["artifact_type"] == "canonical_message" and msg["content"] is None
    assert r.resolve("g|ok")["status"] == "ok"


def test_ineligible_refs_never_return_body(tmp_path: Path) -> None:
    r = _fixture(tmp_path)
    assert "content" not in r.resolve("cm|secret", include_content=True)
    assert "content" not in r.resolve("g|blocked", include_content=True)


def test_unknown_and_explicit_wrong_type_do_not_fall_through(tmp_path: Path) -> None:
    r = _fixture(tmp_path)
    assert r.resolve("cm|missing")["status"] == "missing"
    assert r.resolve("cm|ok", artifact_type="google_signal")["status"] == "missing"
    assert r.resolve("x", artifact_type="not-real")["status"] == "unknown_type"


def test_turn_resolves_composite_id_from_authoritative_json(tmp_path: Path) -> None:
    turns = tmp_path / "turns.json"
    turns.write_text(
        json.dumps(
            [{
                "session_id": "session-1",
                "turn_summaries": [{
                    "turn_id": "7",
                    "narrative": "用户讨论 Python 柱状图",
                    "source_refs": ["cm|1"],
                }],
            }],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    resolver = EvidenceResolver(
        unified_db=tmp_path / "missing.sqlite",
        conversation_db=tmp_path / "missing-conversation.sqlite",
        google_db=tmp_path / "missing-google.sqlite",
        turns_artifact=turns,
    )
    result = resolver.resolve(
        "session-1#7", artifact_type="turn", include_content=True
    )
    assert result["status"] == "ok"
    assert result["content"] == "用户讨论 Python 柱状图"
    assert result["metadata"]["session_id"] == "session-1"
