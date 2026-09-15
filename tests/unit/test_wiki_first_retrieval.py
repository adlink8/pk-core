"""Wiki-first layered retrieval (compressed projection before KU / dialogue).

Public seam
-----------
- Interface: ``search_knowledge_units`` (CLI ``rag-search``, REST
  ``POST /search/semantic``, MCP ``search`` / ``search_semantic``,
  Pi ``knowledge.search``).
- Observable: layered policy returns ``retrieval_unit=wiki_page`` (then
  ``semantic_card``) ahead of KU / dialogue when those stores match;
  ``telemetry.first_contributing_layer`` names the first hit layer.
- Invariants: wiki and cards stores are opened read-only; fingerprints
  unchanged; wiki bodies are not written to KU/Chroma; ``legacy`` policy
  keeps the pre-wiki chain; missing stores degrade to the next layer.
- Focused command: ``pytest tests/unit/test_wiki_first_retrieval.py
  tests/unit/test_layered_fallback_contract.py -q``
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.retrieval._constants import LAYERED_FALLBACK_ORDER
from personal_knowledge.retrieval.semantic_search import search_knowledge_units
from personal_knowledge.wiki.derived_store import (
    SCHEMA_VERSION,
    ProjectionDependency,
    ProjectionPage,
    ProjectionVersion,
    connect_rw,
    insert_page,
    insert_version,
)
from personal_knowledge.wiki.materialization import (
    dependency_manifest_checksum,
    projection_checksum,
)
from personal_knowledge.wiki.page_reader import page_checksum, subject_topic_id

UNIQUE_SUBJECT = "WikiFirstUniqueTopicZXQ"
UNIQUE_CLAIM = "WikiFirstUniqueTopicZXQ 只作为主题页压缩摘要存在"


def _write_wiki_page(path: Path, *, subject: str, answer: str) -> str:
    con = connect_rw(path)
    try:
        normalized = subject.strip().lower()
        topic_id = subject_topic_id(normalized)
        body = {
            "schema": "wiki_page_body_v1",
            "topic": {
                "topic_id": topic_id,
                "topic_type": "subject",
                "canonical_key": f"subject:{normalized}",
                "display_label": f"subject:{normalized}",
            },
            "subject": normalized,
            "aggregation": {
                "unit_count": 1,
                "unit_type_counts": {"technical_conclusion": 1},
                "lifecycle_counts": {"current": 1},
            },
            "claims": [
                {
                    "claim_type": "knowledge_unit",
                    "unit_id": "cu|wiki-first-fixture",
                    "unit_type": "technical_conclusion",
                    "question": f"{subject} 是什么",
                    "answer": answer,
                    "confidence": 0.9,
                    "lifecycle": "current",
                    "evidence_refs": ["v2|cm|wiki-first-e1"],
                }
            ],
            "evidence_refs": ["v2|cm|wiki-first-e1"],
            "source_fingerprint": "wiki-first-fp",
        }
        body_json = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        checksum = page_checksum(body_json)
        deps = [
            ProjectionDependency(
                "knowledge_unit",
                normalized,
                expected_checksum="wiki-first-fp",
                order_key=f"knowledge_unit:{normalized}",
            )
        ]
        version = ProjectionVersion(
            topic_id=topic_id,
            topic_type="subject",
            projection_format_version=SCHEMA_VERSION,
            projection_version="pv_1",
            projection_checksum=projection_checksum(
                topic_id=topic_id,
                topic_type="subject",
                snapshot_bindings={"knowledge_unit": normalized},
                dependencies=deps,
                source_refs={},
            ),
            generated_at="2026-09-01T00:00:00Z",
            freshness_status="fresh",
            reason_codes=(),
            snapshot_bindings={"knowledge_unit": normalized},
            dependency_manifest_checksum=dependency_manifest_checksum(deps),
        )
        insert_version(con, version, deps)
        insert_page(
            con,
            ProjectionPage(
                topic_id=topic_id,
                topic_type="subject",
                projection_version="pv_1",
                page_body=body_json,
                page_checksum=checksum,
                generated_at="2026-09-01T00:00:00Z",
                snapshot_bindings={"knowledge_unit": normalized},
            ),
        )
        return topic_id
    finally:
        con.close()


def _write_cards_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE session_cards(
          session_id TEXT PRIMARY KEY, purpose TEXT, summary_md TEXT,
          card_json TEXT, n_messages INTEGER, truncated INTEGER,
          model TEXT, input_tokens INTEGER, output_tokens INTEGER, created_at TEXT,
          chunk_count INTEGER);
        CREATE TABLE ku_facts(
          fact_key TEXT PRIMARY KEY, session_id TEXT, fact TEXT,
          evidence_refs TEXT, confidence TEXT, valid_from TEXT,
          supersedes TEXT, status TEXT DEFAULT 'active', norm_prefix TEXT);
        """
    )
    con.execute(
        "insert into session_cards values (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "v2|cs|card-first-aaaa",
            "讨论 WikiFirstUniqueTopicZXQ 的会话目的",
            "会话卡压缩纪要",
            "{}",
            4,
            0,
            "test",
            1,
            1,
            "2026-09-01T00:00:00Z",
            1,
        ),
    )
    con.execute(
        "insert into ku_facts values (?,?,?,?,?,?,?,?,?)",
        (
            "kc|card-first",
            "v2|cs|card-first-aaaa",
            UNIQUE_CLAIM,
            '["v2|cm|card-e1"]',
            "high",
            "2026-09-01T00:00:00Z",
            None,
            "active",
            "wikifirstuniquetopiczxq",
        ),
    )
    con.commit()
    con.close()


@pytest.fixture
def isolated_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    wiki = tmp_path / "personal_wiki_projection.sqlite"
    cards = tmp_path / "semantic_mvp_v3.sqlite"
    _write_wiki_page(wiki, subject=UNIQUE_SUBJECT, answer=UNIQUE_CLAIM)
    _write_cards_db(cards)
    import personal_knowledge.retrieval._constants as constants
    import personal_knowledge.retrieval.semantic_cards as semantic_cards

    authority = tmp_path / "authority.sqlite"
    with sqlite3.connect(authority) as con:
        con.executescript("CREATE TABLE knowledge_units(unit_id TEXT,unit_type TEXT,subject TEXT,question TEXT,answer TEXT,confidence REAL,lifecycle TEXT,status TEXT,version INTEGER); CREATE TABLE knowledge_unit_evidence(unit_id TEXT,evidence_ref TEXT);")
        con.execute("INSERT INTO knowledge_units VALUES(?,?,?,?,?,?,?,?,?)", ("cu|wiki-first-fixture", "technical_conclusion", UNIQUE_SUBJECT, f"{UNIQUE_SUBJECT} 是什么", UNIQUE_CLAIM, .9, "current", "current", 1))
        con.execute("INSERT INTO knowledge_unit_evidence VALUES(?,?)", ("cu|wiki-first-fixture", "v2|cm|wiki-first-e1"))
    monkeypatch.setattr(constants, "UNIFIED_DB", authority)
    monkeypatch.setattr(constants, "WIKI_PROJECTION_DB", wiki)
    monkeypatch.setattr(constants, "CARDS_DB", cards)
    monkeypatch.setattr(semantic_cards, "CARDS_DB_PATH", cards)
    monkeypatch.setattr(
        "personal_knowledge.retrieval.semantic_search._read_knowledge_active_collection",
        lambda: "",
    )
    monkeypatch.setattr(
        "personal_knowledge.retrieval.semantic_search._search_dialogue_canonical_messages",
        lambda query, top_k=5: [],
    )
    return {"wiki": wiki, "cards": cards, "authority": authority}


def test_layered_order_starts_with_wiki_then_cards_then_ku() -> None:
    assert LAYERED_FALLBACK_ORDER[:3] == (
        "wiki_page",
        "semantic_card",
        "knowledge_unit",
    )


def test_wiki_match_is_first_contributing_layer(isolated_stores: dict[str, Path]) -> None:
    before = hashlib.sha256(isolated_stores["wiki"].read_bytes()).hexdigest()
    result = search_knowledge_units(
        UNIQUE_SUBJECT,
        top_k=5,
        fallback_policy="layered",
        allow_legacy_pad=False,
    )
    after = hashlib.sha256(isolated_stores["wiki"].read_bytes()).hexdigest()
    assert after == before
    assert result["results"], result
    assert result["results"][0]["retrieval_unit"] == "wiki_page"
    assert UNIQUE_SUBJECT.lower() in result["results"][0]["subject"].lower()
    assert result["telemetry"]["first_contributing_layer"] == "wiki_page"
    units = [item["retrieval_unit"] for item in result["results"]]
    if "semantic_card" in units:
        assert units.index("wiki_page") < units.index("semantic_card")


def test_card_match_when_wiki_store_missing(
    isolated_stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import personal_knowledge.retrieval._constants as constants

    monkeypatch.setattr(
        constants, "WIKI_PROJECTION_DB", isolated_stores["wiki"].parent / "absent.sqlite"
    )
    result = search_knowledge_units(
        UNIQUE_SUBJECT,
        top_k=5,
        fallback_policy="layered",
        allow_legacy_pad=False,
    )
    assert result["results"], result
    assert result["results"][0]["retrieval_unit"] == "semantic_card"
    assert result["telemetry"]["first_contributing_layer"] == "semantic_card"


def test_legacy_policy_skips_wiki_prefix(isolated_stores: dict[str, Path]) -> None:
    result = search_knowledge_units(
        UNIQUE_SUBJECT,
        top_k=3,
        fallback_policy="legacy",
        allow_legacy_pad=False,
    )
    units = [item.get("retrieval_unit") for item in result.get("results") or []]
    assert "wiki_page" not in units
    assert result["telemetry"]["first_contributing_layer"] != "wiki_page"


def test_stale_wiki_projection_is_rejected_by_current_search(isolated_stores: dict[str, Path]) -> None:
    con = sqlite3.connect(isolated_stores["wiki"])
    con.execute("UPDATE wiki_projection_versions SET freshness_status='stale', reason_codes_json='[\"dependency_checksum_mismatch\"]'")
    con.commit(); con.close()
    result = search_knowledge_units(
        UNIQUE_SUBJECT, top_k=5, fallback_policy="layered", allow_legacy_pad=False
    )
    assert all(item["retrieval_unit"] != "wiki_page" for item in result["results"])
    assert result["telemetry"]["first_contributing_layer"] == "semantic_card"


def test_unknown_wiki_freshness_is_rejected_by_current_search(isolated_stores: dict[str, Path]) -> None:
    con = sqlite3.connect(isolated_stores["wiki"])
    con.execute("UPDATE wiki_projection_versions SET freshness_status='unknown'")
    con.commit(); con.close()
    result = search_knowledge_units(
        UNIQUE_SUBJECT, top_k=5, fallback_policy="layered", allow_legacy_pad=False
    )
    assert all(item["retrieval_unit"] != "wiki_page" for item in result["results"])


def test_card_cites_matched_fact_not_earlier_unrelated_fact(isolated_stores, monkeypatch):
    import personal_knowledge.retrieval._constants as constants
    monkeypatch.setattr(constants, "WIKI_PROJECTION_DB", isolated_stores["wiki"].parent / "absent.sqlite")
    with sqlite3.connect(isolated_stores["cards"]) as con:
        con.execute("INSERT INTO ku_facts VALUES (?,?,?,?,?,?,?,?,?)", (
            "kc|earlier-unrelated", "v2|cs|card-first-aaaa", "Unrelated breakfast preference",
            '["v2|cm|unrelated"]', "low", "2020-01-01T00:00:00Z", None, "active", "breakfast",
        ))
    result = search_knowledge_units(UNIQUE_SUBJECT, top_k=1, fallback_policy="layered", allow_legacy_pad=False)
    hit = result["results"][0]
    assert hit["answer"] == UNIQUE_CLAIM
    assert hit["source_message_ref"] == "v2|cm|card-e1"
    assert hit["confidence"] == 0.9


@pytest.mark.parametrize("change", [
    "UPDATE knowledge_units SET answer='revised conclusion'",
    "UPDATE knowledge_units SET confidence=0.1",
    "UPDATE knowledge_units SET status='withdrawn'",
    "DELETE FROM knowledge_unit_evidence",
])
def test_live_authority_change_invalidates_wiki_without_rebuild(isolated_stores, change):
    with sqlite3.connect(isolated_stores["authority"]) as con:
        con.execute(change)
    fingerprints = {key: path.read_bytes() for key, path in isolated_stores.items()}
    result = search_knowledge_units(UNIQUE_SUBJECT, top_k=1, fallback_policy="layered", allow_legacy_pad=False)
    assert result["results"][0]["retrieval_unit"] == "semantic_card"
    assert all(path.read_bytes() == fingerprints[key] for key, path in isolated_stores.items())
