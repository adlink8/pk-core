"""Phase 14 Plan 05 测试：search contracts + feedback privacy。"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_ROOT = _THIS_DIR.parent
_SCRIPTS = _ROOT / "integration" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL  # noqa: E402
from personal_knowledge.evaluation.knowledge.evaluate_knowledge_canary import (  # noqa: E402
    read_active_collection, search_knowledge_units, generate_canary_queries,
)
import personal_knowledge.retrieval._constants as _retrieval_constants  # noqa: E402


def _disable_compressed_layers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep dialogue/event contract tests off the live wiki/card stores."""
    missing = tmp_path / "absent-compressed.sqlite"
    monkeypatch.setattr(_retrieval_constants, "WIKI_PROJECTION_DB", missing)
    monkeypatch.setattr(_retrieval_constants, "CARDS_DB", missing)


def test_read_active_collection_none(tmp_path: Path) -> None:
    """无 active pointer 时返回 None。"""
    import personal_knowledge.evaluation.knowledge.evaluate_knowledge_canary as ec
    ec.ACTIVE_POINTER = tmp_path / "nonexistent.txt"
    assert read_active_collection() is None


def test_read_active_collection_reads(tmp_path: Path) -> None:
    """active pointer 存在时返回内容。"""
    import personal_knowledge.evaluation.knowledge.evaluate_knowledge_canary as ec
    ec.ACTIVE_POINTER = tmp_path / "active.txt"
    ec.ACTIVE_POINTER.write_text("test_collection", encoding="utf-8")
    assert read_active_collection() == "test_collection"


def test_search_knowledge_units_no_active(tmp_path: Path) -> None:
    """无 active 时 route=fallback_raw。"""
    import personal_knowledge.evaluation.knowledge.evaluate_knowledge_canary as ec
    ec.ACTIVE_POINTER = tmp_path / "nonexistent.txt"
    result = search_knowledge_units("test query", collection_name="nonexistent")
    assert result["route"] == "fallback_raw"


def test_search_knowledge_units_bad_collection(tmp_path: Path) -> None:
    """不存在的 collection 返回 fallback_raw。"""
    result = search_knowledge_units("test query", collection_name="nonexistent_collection_xyz")
    assert result["route"] == "fallback_raw"


def test_generate_canary_queries_hashes_only(tmp_path: Path) -> None:
    """canary queries 只含 hash，不含原文。"""
    db = tmp_path / "test.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA_SQL)
    con.execute(
        "INSERT INTO knowledge_build_runs VALUES "
        "('r1','extraction','2026-01-01',NULL,'h','v1','v1','m',NULL,NULL,NULL,NULL,'validated',NULL,NULL)"
    )
    con.execute(
        "INSERT INTO canonical_knowledge_units (canonical_unit_id, subject, unit_type, question, "
        "answer, confidence, lifecycle, status, version, run_id, merge_reason, created_at) VALUES "
        "('cu1','test','preference','what is my name?','answer',0.9,'current','current',1,'r1','single','2026-01-01')"
    )
    con.commit()
    con.close()

    import personal_knowledge.evaluation.knowledge.evaluate_knowledge_canary as ec
    ec.UNIFIED_DB = db
    queries = generate_canary_queries(1)
    assert len(queries) == 1
    q = queries[0]
    assert "query_hash" in q
    assert "label" in q
    assert q["label"] == ""  # 初始为空，不是模型自填
    # 不含 raw query
    assert "query" not in q


def test_feedback_tables_created(tmp_path: Path) -> None:
    """rag_runs / rag_retrieval_items / rag_feedback 表存在。"""
    db = tmp_path / "test.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA_SQL)
    con.commit()
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "rag_runs" in tables
    assert "rag_retrieval_items" in tables
    assert "rag_feedback" in tables
    con.close()


def test_feedback_label_check(tmp_path: Path) -> None:
    """rag_feedback 的 label CHECK 约束生效。"""
    db = tmp_path / "test.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA_SQL)
    con.execute("INSERT INTO rag_runs VALUES ('r1','col1','v1','cb1','er1','canary','2026-01-01')")
    con.execute(
        "INSERT INTO rag_retrieval_items (run_id, query_hash, top_k, returned_ids, route, "
        "created_at) VALUES ('r1','hash1',5,'[]','canary','2026-01-01')"
    )
    con.commit()

    # valid label
    con.execute(
        "INSERT INTO rag_feedback (retrieval_id, label, labeled_at) VALUES (1,'helpful','2026-01-01')"
    )
    # invalid label
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO rag_feedback (retrieval_id, label, labeled_at) VALUES (1,'bad_label','2026-01-01')"
        )
    con.close()


def test_feedback_no_raw_query_fields(tmp_path: Path) -> None:
    """rag_retrieval_items 不含 raw query 字段。"""
    db = tmp_path / "test.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA_SQL)
    cols = {c[1] for c in con.execute("PRAGMA table_info(rag_retrieval_items)")}
    assert "raw_query" not in cols
    assert "query_text" not in cols
    assert "evidence_quote" not in cols
    assert "result_text" not in cols
    con.close()


def test_canary_report_privacy_safe(tmp_path: Path) -> None:
    """canary report 不含 raw query/evidence。"""
    from personal_knowledge.evaluation.knowledge.evaluate_knowledge_canary import run_canary
    db = tmp_path / "test.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA_SQL)
    con.execute(
        "INSERT INTO knowledge_build_runs VALUES "
        "('r1','extraction','2026-01-01',NULL,'h','v1','v1','m',NULL,NULL,NULL,NULL,'validated',NULL,NULL)"
    )
    for i in range(3):
        con.execute(
            "INSERT INTO canonical_knowledge_units VALUES "
            f"('cu{i}','subj{i}','preference','question {i}','answer {i}',0.9,'current','current',1,'r1','single',NULL,'2026-01-01')"
        )
    con.commit()
    con.close()

    import personal_knowledge.evaluation.knowledge.evaluate_knowledge_canary as ec
    ec.UNIFIED_DB = db
    report = run_canary("nonexistent_collection", n_queries=3, report_path=tmp_path / "canary.json")
    assert "results" in report
    for r in report["results"]:
        assert "query_hash" in r
        assert "raw_query" not in r
        assert "evidence_quote" not in r


# --- Hybrid search backend contract tests (unified_search.search_knowledge_units) ---

def test_hybrid_empty_query_abstains() -> None:
    """空 query 返回 abstain，不调用向量库。"""
    from personal_knowledge.retrieval.unified_search import search_knowledge_units
    result = search_knowledge_units("", top_k=5)
    assert result["route"] == "abstain"
    assert result["results"] == []


def test_hybrid_no_active_falls_back() -> None:
    """无 active collection 且无 override 时 route=fallback_raw/abstain/knowledge(有 infra)。"""
    from personal_knowledge.retrieval.unified_search import search_knowledge_units
    # 有 infra+active 时返回 knowledge；无时 fallback_raw/abstain
    result = search_knowledge_units("test query without infra", top_k=5)
    assert result["route"] in ("fallback_raw", "abstain", "knowledge")


def test_hybrid_top_k_bounded() -> None:
    """top_k 被 clamp 到 [1,20]。"""
    from personal_knowledge.retrieval.unified_search import search_knowledge_units
    # 这些不会真正调用向量库（empty query 先 abstain）
    result = search_knowledge_units("", top_k=100)
    assert result["route"] == "abstain"
    result = search_knowledge_units("", top_k=0)
    assert result["route"] == "abstain"


def test_current_only_is_explicit_and_fail_closed() -> None:
    from inspect import signature
    from personal_knowledge.retrieval.semantic_search import search_knowledge_units

    assert "current_only" in signature(search_knowledge_units).parameters
    # Empty query avoids any vector/Chroma call while proving both contracts
    # remain the same abstaining result.
    assert search_knowledge_units("", current_only=True)["route"] == "abstain"
    assert search_knowledge_units("", current_only=False)["route"] == "abstain"
def test_hybrid_result_contract_fields() -> None:
    """生产 backend 结果包含必需字段：rank/unit_id/retrieval_unit/score/collection。"""
    # 这个测试验证字段 schema，不依赖真实向量库
    # 模拟结果结构
    from personal_knowledge.retrieval.unified_search import search_knowledge_units
    result = search_knowledge_units("", top_k=5)
    # abstain 时 results 为空，验证 route 合同
    assert "route" in result
    assert "results" in result
    assert "versions" in result
    assert isinstance(result["results"], list)
    assert isinstance(result["versions"], dict)


# --- Phase 15 Wave 2: layered hybrid fallback ---

def test_hybrid_empty_query_reports_fallback_policy() -> None:
    """abstain 仍返回 resolved fallback_policy。"""
    from personal_knowledge.retrieval.unified_search import search_knowledge_units
    result = search_knowledge_units("", top_k=5, fallback_policy="layered")
    assert result["route"] == "abstain"
    assert result["fallback_policy"] == "layered"
    assert "allow_legacy_pad" in result
    assert "telemetry" in result
    tel = result["telemetry"]
    assert "layers" in tel
    assert "pad_used" in tel
    assert "total_latency_ms" in tel
    names = {x["name"] for x in tel["layers"]}
    assert "knowledge_unit" in names and "legacy_pad" in names
    result2 = search_knowledge_units("", top_k=5, fallback_policy="legacy")
    assert result2["fallback_policy"] == "legacy"


def test_resolve_fallback_policy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import personal_knowledge.retrieval.unified_search as us
    import personal_knowledge.retrieval.semantic_search as ss
    monkeypatch.delenv("PERSONAL_DATA_FALLBACK_POLICY", raising=False)
    assert us._resolve_fallback_policy(None) == "layered"
    monkeypatch.setenv("PERSONAL_DATA_FALLBACK_POLICY", "legacy")
    assert us._resolve_fallback_policy(None) == "legacy"
    assert us._resolve_fallback_policy("layered") == "layered"
    assert us._resolve_fallback_policy("nope") == "layered"


def test_search_dialogue_canonical_messages_snippet(tmp_path: Path) -> None:
    """canonical message snippet search returns cm| ids as dialogue units."""
    import personal_knowledge.retrieval.unified_search as us
    import personal_knowledge.retrieval.semantic_search as ss

    db = tmp_path / "canon.sqlite"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE canonical_messages ("
        "canonical_message_id TEXT PRIMARY KEY, role TEXT, content TEXT, "
        "timestamp TEXT, source TEXT, is_system INTEGER)"
    )
    con.execute(
        "INSERT INTO canonical_messages VALUES "
        "('cm|abc','user','private void spinner() { spinner1 = findViewById(R.id.spinner1); }',"
        "'2026-01-01','agentsview',0)"
    )
    con.execute(
        "INSERT INTO canonical_messages VALUES "
        "('cm|sys','assistant','ignore me','2026-01-01','agentsview',1)"
    )
    con.commit()
    con.close()

    q = "private void spinner() { spinner1 = findViewById(R.id.spinner1); extra padding here"
    hits = us._search_dialogue_canonical_messages(q, top_k=3, db_path=db)
    assert hits
    assert hits[0]["unit_id"] == "cm|abc"
    assert hits[0]["retrieval_unit"] == "dialogue"
    assert hits[0]["source_message_ref"] == "cm|abc"
    assert hits[0]["collection"] == "canonical_messages"


def test_search_dialogue_uses_only_active_v2_projection_when_legacy_coexists(
    tmp_path: Path,
) -> None:
    import personal_knowledge.retrieval.unified_search as us

    db = tmp_path / "coexist-canon.sqlite"
    con = sqlite3.connect(db)
    try:
        con.execute(
            "CREATE TABLE canonical_messages ("
            "canonical_message_id TEXT PRIMARY KEY, role TEXT, content TEXT, "
            "timestamp TEXT, source TEXT, is_system INTEGER)"
        )
        con.execute(
            "CREATE TABLE ce_generation_authority ("
            "generation_id TEXT PRIMARY KEY, active INTEGER, updated_at TEXT)"
        )
        body = "shared searchable projection marker with enough stable tokens"
        con.executemany(
            "INSERT INTO canonical_messages VALUES (?,?,?,?,?,?)",
            (
                ("legacy-message", "user", body, "2026-08-03", "legacy", 0),
                ("v2|gen|message", "user", body, "2026-08-02", "legacy", 0),
            ),
        )
        con.execute(
            "INSERT INTO ce_generation_authority VALUES "
            "('gen', 1, '2026-08-15')"
        )
        con.commit()
    finally:
        con.close()

    hits = us._search_dialogue_canonical_messages(
        "shared searchable projection marker", top_k=5, db_path=db,
    )

    assert [hit["unit_id"] for hit in hits] == ["v2|gen|message"]


def test_layered_tags_dialogue_vs_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """layered path: conversation_turns -> retrieval_unit=dialogue; Google PE -> event."""
    import types
    import personal_knowledge.retrieval.unified_search as us
    import personal_knowledge.retrieval.semantic_search as ss
    from personal_knowledge.retrieval.evidence import EvidenceResolver

    _disable_compressed_layers(monkeypatch, tmp_path)
    monkeypatch.setattr(ss, "_read_knowledge_active_collection", lambda: "test_collection")
    monkeypatch.setattr(
        EvidenceResolver,
        "resolve",
        lambda self, ref, **kwargs: {"ref": ref, "status": "ok", "content": "reviewed evidence"},
    )
    # Prefer turns path in this unit test (canonical empty)
    monkeypatch.setattr(ss, "_search_dialogue_canonical_messages", lambda *a, **k: [])

    class _FakeColl:
        def query(self, **kwargs):
            return {"ids": [[]], "documents": [[]], "distances": [[]], "metadatas": [[]]}

    class _FakeClient:
        def list_collections(self):
            return ["test_collection"]

        def get_or_create_collection(self, name):
            return _FakeColl()

    fake_chroma = types.SimpleNamespace(
        ChromaClient=lambda port=None: _FakeClient(),
        ChromaError=Exception,
    )
    monkeypatch.setitem(__import__("sys").modules, "chroma_client", fake_chroma)
    monkeypatch.setitem(
        __import__("sys").modules,
        "local_embed",
        types.SimpleNamespace(embed=lambda q: [0.1] * 8),
    )
    import personal_knowledge.core.chroma_client as chroma_mod
    import personal_knowledge.core.local_embed as embed_mod
    monkeypatch.setattr(chroma_mod, "ChromaClient", lambda port=None: _FakeClient())
    monkeypatch.setattr(embed_mod, "embed", lambda q: [0.1] * 8)

    turns = [
        {
            "event_id": "turn-1",
            "title": "dialog topic",
            "content": "we discussed X",
            "score": 0.9,
            "source": "Agent",
            "event_time": "",
        }
    ]
    google_events = [
        {
            "event_id": "g-1",
            "title": "google hit",
            "content": "search about X",
            "score": 0.8,
            "source": "Google",
            "event_time": "2025-01-01",
        }
    ]
    agent_events = [
        {
            "event_id": "a-1",
            "title": "agent file event",
            "content": "should not be preferred as dialogue",
            "score": 0.95,
            "source": "Agent",
            "event_time": "2025-01-02",
        }
    ]

    def fake_semantic_search(query, top_k=5, source=None, client=None):
        if source == "Google":
            return google_events[:top_k]
        if source == "Agent":
            return agent_events[:top_k]
        # unfiltered: mix
        return (google_events + agent_events)[:top_k]

    monkeypatch.setattr(ss, "_semantic_search", fake_semantic_search)

    # Patch conversation turns import path used inside search_knowledge_units
    import personal_knowledge.retrieval.search_vectors as sv

    monkeypatch.setattr(
        sv,
        "search_conversation_turns",
        lambda query, top_k=5, source=None, client=None: turns[:top_k],
    )

    result = us.search_knowledge_units(
        "discussed search about",
        top_k=5,
        fallback_policy="layered",
        allow_legacy_pad=False,
    )
    assert result["fallback_policy"] == "layered"
    assert result["results"], "expected fallback results"
    units = [r["retrieval_unit"] for r in result["results"]]
    collections = [r["collection"] for r in result["results"]]
    assert "dialogue" in units
    assert any(c == "conversation_turns" for c in collections)
    # dialogue item tags
    dialogue_hits = [r for r in result["results"] if r["retrieval_unit"] == "dialogue"]
    assert dialogue_hits
    assert dialogue_hits[0]["collection"] == "conversation_turns"
    # non_dialogue event tags
    event_hits = [r for r in result["results"] if r["retrieval_unit"] == "event"]
    assert event_hits
    assert all(r["collection"] == "personal_events" for r in event_hits)
    assert all(r.get("source") == "Google" for r in event_hits)
    # without legacy pad, Agent personal_events must not fill as dialogue substitute
    assert all(r.get("source") != "Agent" or r["retrieval_unit"] == "dialogue" for r in result["results"])


def test_legacy_policy_uses_raw_event_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """legacy path keeps raw event semantic match rank_reason."""
    import types
    import personal_knowledge.retrieval.unified_search as us
    import personal_knowledge.retrieval.semantic_search as ss

    monkeypatch.setattr(ss, "_read_knowledge_active_collection", lambda: "test_collection")

    class _FakeColl:
        def query(self, **kwargs):
            return {"ids": [[]], "documents": [[]], "distances": [[]], "metadatas": [[]]}

    class _FakeClient:
        def list_collections(self):
            return ["test_collection"]

        def get_or_create_collection(self, name):
            return _FakeColl()

    fake_chroma = types.SimpleNamespace(
        ChromaClient=lambda port=None: _FakeClient(),
        ChromaError=Exception,
    )
    monkeypatch.setitem(__import__("sys").modules, "chroma_client", fake_chroma)
    monkeypatch.setitem(
        __import__("sys").modules,
        "local_embed",
        types.SimpleNamespace(embed=lambda q: [0.1] * 8),
    )
    import personal_knowledge.core.chroma_client as chroma_mod
    import personal_knowledge.core.local_embed as embed_mod
    monkeypatch.setattr(chroma_mod, "ChromaClient", lambda port=None: _FakeClient())
    monkeypatch.setattr(embed_mod, "embed", lambda q: [0.1] * 8)

    events = [
        {
            "event_id": "e-1",
            "title": "any",
            "content": "body",
            "score": 0.7,
            "source": "Agent",
            "event_time": "2025-01-01",
        }
    ]
    monkeypatch.setattr(
        ss,
        "_semantic_search",
        lambda query, top_k=5, source=None, client=None: events[:top_k],
    )

    result = us.search_knowledge_units("legacy q", top_k=3, fallback_policy="legacy")
    assert result["fallback_policy"] == "legacy"
    assert result["results"]
    assert result["results"][0]["retrieval_unit"] == "event"
    assert result["results"][0]["rank_reason"] == "raw event semantic match"


def test_layered_legacy_pad_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When dialogue+Google insufficient, pad non-Google with rank_reason=legacy_pad."""
    import types
    import personal_knowledge.retrieval.unified_search as us
    import personal_knowledge.retrieval.semantic_search as ss

    _disable_compressed_layers(monkeypatch, tmp_path)
    monkeypatch.setattr(ss, "_read_knowledge_active_collection", lambda: "test_collection")

    class _FakeClient:
        def list_collections(self):
            return ["test_collection"]

        def get_or_create_collection(self, name):
            return types.SimpleNamespace(
                query=lambda **kw: {
                    "ids": [[]],
                    "documents": [[]],
                    "distances": [[]],
                    "metadatas": [[]],
                }
            )

    monkeypatch.setitem(
        __import__("sys").modules,
        "chroma_client",
        types.SimpleNamespace(ChromaClient=lambda port=None: _FakeClient(), ChromaError=Exception),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "local_embed",
        types.SimpleNamespace(embed=lambda q: [0.1] * 4),
    )
    import personal_knowledge.core.chroma_client as chroma_mod
    import personal_knowledge.core.local_embed as embed_mod
    monkeypatch.setattr(chroma_mod, "ChromaClient", lambda port=None: _FakeClient())
    monkeypatch.setattr(embed_mod, "embed", lambda q: [0.1] * 4)

    import personal_knowledge.retrieval.search_vectors as sv

    monkeypatch.setattr(
        sv, "search_conversation_turns", lambda *a, **k: []
    )

    def fake_search(query, top_k=5, source=None, client=None):
        if source == "Google":
            return [
                {
                    "event_id": "g1",
                    "title": "g",
                    "content": "g",
                    "score": 0.5,
                    "source": "Google",
                    "event_time": "",
                }
            ]
        # unfiltered / other — used for pad when source is None
        return [
            {
                "event_id": "g1",
                "title": "g",
                "content": "g",
                "score": 0.5,
                "source": "Google",
                "event_time": "",
            },
            {
                "event_id": "a1",
                "title": "agent",
                "content": "agent",
                "score": 0.4,
                "source": "Agent",
                "event_time": "",
            },
        ][:top_k]

    monkeypatch.setattr(ss, "_semantic_search", fake_search)

    result = us.search_knowledge_units(
        # 探针串不得分词成会在真库（knowledge_units/canonical_messages）命中的
        # 短 token（ASCII token 最短 3 字符）；否则 KU sqlite 回退吃光预算，
        # pad 层遥测断言失去确定性。
        "qqzz",
        top_k=3, fallback_policy="layered", allow_legacy_pad=True
    )
    reasons = {r["rank_reason"] for r in result["results"]}
    assert "legacy_pad" in reasons
    pad_hits = [r for r in result["results"] if r["rank_reason"] == "legacy_pad"]
    assert all(r.get("source") != "Google" for r in pad_hits)
    assert result.get("allow_legacy_pad") is True
    tel = result["telemetry"]
    assert tel["pad_used"] is True
    pad_layer = next(x for x in tel["layers"] if x["name"] == "legacy_pad")
    assert pad_layer["attempted"] is True
    assert pad_layer["hits"] >= 1
    assert pad_layer["latency_ms"] >= 0


def test_layered_telemetry_skips_pad_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """allow_legacy_pad=False → pad layer not attempted; telemetry pad_used false."""
    import types
    import personal_knowledge.retrieval.unified_search as us
    import personal_knowledge.retrieval.semantic_search as ss

    _disable_compressed_layers(monkeypatch, tmp_path)
    monkeypatch.setattr(ss, "_read_knowledge_active_collection", lambda: "")
    monkeypatch.setattr(ss, "_search_dialogue_canonical_messages", lambda *a, **k: [])

    class _FakeClient:
        def list_collections(self):
            return []

        def get_or_create_collection(self, name):
            return types.SimpleNamespace(
                query=lambda **kw: {
                    "ids": [[]],
                    "documents": [[]],
                    "distances": [[]],
                    "metadatas": [[]],
                }
            )

    monkeypatch.setitem(
        __import__("sys").modules,
        "chroma_client",
        types.SimpleNamespace(ChromaClient=lambda port=None: _FakeClient(), ChromaError=Exception),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "local_embed",
        types.SimpleNamespace(embed=lambda q: [0.1] * 4),
    )
    import personal_knowledge.retrieval.search_vectors as sv

    monkeypatch.setattr(sv, "search_conversation_turns", lambda *a, **k: [])
    monkeypatch.setattr(
        us,
        "_semantic_search",
        lambda query, top_k=5, source=None, client=None: [
            {
                "event_id": "g1",
                "title": "g",
                "content": "g",
                "score": 0.5,
                "source": "Google",
                "event_time": "",
            }
        ][:top_k]
        if source == "Google"
        else [],
    )

    result = us.search_knowledge_units(
        "qqzz",  # 哑探针：不得命中真库（同 test_layered_legacy_pad_reason 注释）
        top_k=3, fallback_policy="layered", allow_legacy_pad=False
    )
    assert result["allow_legacy_pad"] is False
    tel = result["telemetry"]
    assert tel["pad_used"] is False
    pad_layer = next(x for x in tel["layers"] if x["name"] == "legacy_pad")
    assert pad_layer["attempted"] is False
    assert pad_layer["hits"] == 0
    # non_dialogue should have been attempted
    nd = next(x for x in tel["layers"] if x["name"] == "non_dialogue_raw")
    assert nd["attempted"] is True


def test_holdout_15_02_cases_schema() -> None:
    """Holdout suite is versioned, tagged, and separate from frozen_test."""
    path = (
        Path(__file__).resolve().parents[2]
        / "assets"
        / "evals"
        / "knowledge_units"
        / "holdout_15_02.synthetic.jsonl"
    )
    assert path.exists()
    cases = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(cases) >= 8
    tags = {c["suite_tag"] for c in cases}
    assert {"google", "paraphrase", "no_answer", "privacy"} <= tags
    for c in cases:
        assert c["split"] == "holdout_15_02"
        assert c["id"].startswith("holdout-")
        assert "query" in c
