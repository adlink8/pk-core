from __future__ import annotations

import sqlite3
from pathlib import Path

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL
from personal_knowledge.application.serving.snapshots import activate_snapshot, get_snapshot, prepare_snapshot, validate_snapshot
from personal_knowledge.evaluation.retrieval_adapters import capture_serving_binding
from personal_knowledge.retrieval.serving import ServingSnapshotResolver


def _active(tmp_path: Path) -> tuple[Path, Path, str]:
    db, pointer = tmp_path / "u.sqlite", tmp_path / "active.txt"
    con = sqlite3.connect(db); con.executescript(SCHEMA_SQL); con.close()
    members = {
        "knowledge_retrieval": {"version": "kv1", "checksum": "kck", "location_kind": "chroma_collection", "location_ref": "ku_snapshot", "metadata": {"unit_count": 2}},
        "canonical_message": {"version": "cv1", "checksum": "cck", "location_kind": "sqlite_view", "location_ref": "canonical_messages"},
    }
    draft = prepare_snapshot(db, members, eval_gate_ref="gate", write=True)
    assert validate_snapshot(db, draft["snapshot_id"], collection_inspector=lambda _: {"exists": True, "checksum": "kck", "count": 2})["ok"]
    activate_snapshot(db, draft["snapshot_id"], pointer_path=pointer)
    return db, pointer, draft["snapshot_id"]


def test_resolver_prefers_sqlite_authority_and_reports_pointer_drift(tmp_path: Path) -> None:
    db, pointer, snapshot_id = _active(tmp_path)
    pointer.write_text("wrong", encoding="utf-8")
    state = ServingSnapshotResolver(db, pointer).resolve()
    assert state.snapshot_id == snapshot_id
    assert state.member("knowledge_retrieval")["location_ref"] == "ku_snapshot"
    assert state.drift == ["knowledge_active_pointer"]


def test_shared_search_contract_exposes_one_snapshot(monkeypatch, tmp_path: Path) -> None:
    import personal_knowledge.retrieval.semantic_search as ss
    db, pointer, snapshot_id = _active(tmp_path)
    monkeypatch.setattr(ss._C, "UNIFIED_DB", db)
    monkeypatch.setattr(ss._C, "DB_DIR", pointer.parent)
    # The product pointer name is fixed by the compatibility contract.
    product_pointer = pointer.parent / "knowledge_index_active.txt"
    product_pointer.write_text("ku_snapshot", encoding="utf-8")
    result = ss.search_knowledge_units("", fallback_policy="layered")
    assert result["serving_snapshot_id"] == snapshot_id
    assert result["snapshot_consistency"] == "enforced"
    assert result["snapshot_drift"] == []
    versions = {x["name"]: x.get("version") for x in result["telemetry"]["layers"]}
    assert versions["knowledge_unit"] == "kv1"
    assert versions["canonical_messages"] == "cv1"


def test_explicit_evaluation_snapshot_does_not_change_active_authority(tmp_path: Path) -> None:
    db, pointer, active_id = _active(tmp_path)
    candidate = prepare_snapshot(
        db,
        {
            "knowledge_retrieval": {
                "version": "kv2", "checksum": "kck2",
                "location_kind": "chroma_collection", "location_ref": "ku_candidate",
                "metadata": {"unit_count": 3},
            },
            "canonical_message": {
                "version": "cv1", "checksum": "cck",
                "location_kind": "sqlite_view", "location_ref": "canonical_messages",
            },
        },
        write=True,
    )

    binding = capture_serving_binding(db, candidate["snapshot_id"])

    assert binding["ok"] is True
    assert binding["snapshot_id"] == candidate["snapshot_id"]
    assert binding["members"]["knowledge_retrieval"]["location_ref"] == "ku_candidate"
    assert get_snapshot(db, candidate["snapshot_id"])["status"] == "draft"
    assert pointer.read_text(encoding="utf-8") == "ku_snapshot"
    assert capture_serving_binding(db)["snapshot_id"] == active_id
