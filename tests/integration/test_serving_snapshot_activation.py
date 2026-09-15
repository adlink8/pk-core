from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL
from personal_knowledge.application.serving.snapshots import (
    activate_snapshot, bootstrap_current_snapshot, get_active_snapshot, prepare_snapshot,
    repair_pointer_projection, rollback_snapshot, validate_snapshot,
)


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "db.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)
    con.close()
    return path


def _member(collection: str, checksum: str = "ck", count: int = 2) -> dict:
    return {"version": collection, "checksum": checksum, "location_kind": "chroma_collection", "location_ref": collection, "metadata": {"unit_count": count}}


def _inspect(name: str) -> dict:
    return {"exists": True, "checksum": "ck", "count": 2}


def _validated(db: Path, collection: str) -> str:
    draft = prepare_snapshot(db, {"knowledge_retrieval": _member(collection)}, eval_gate_ref="gate-pass", write=True)
    assert validate_snapshot(db, draft["snapshot_id"], collection_inspector=_inspect, required_roles={"knowledge_retrieval"})["ok"]
    return draft["snapshot_id"]


def test_prepare_dry_run_and_validation_refusal_do_not_activate(tmp_path: Path) -> None:
    db = _db(tmp_path)
    dry = prepare_snapshot(db, {"knowledge_retrieval": _member("cand")})
    assert dry["written"] is False
    derived_id = dry["manifest"]["members"]["knowledge_retrieval"]["artifact_version_id"]
    assert derived_id.startswith("av_")
    assert get_active_snapshot(db) is None


def test_validation_refuses_manifest_member_drift(tmp_path: Path) -> None:
    db = _db(tmp_path)
    written = prepare_snapshot(
        db,
        {"knowledge_retrieval": _member("cand")},
        eval_gate_ref="gate-pass",
        write=True,
    )
    con = sqlite3.connect(db)
    manifest = json.loads(
        con.execute(
            "SELECT manifest_json FROM serving_snapshots WHERE snapshot_id=?",
            (written["snapshot_id"],),
        ).fetchone()[0]
    )
    manifest["members"]["knowledge_retrieval"]["artifact_version_id"] = "av_drift"
    con.execute(
        "UPDATE serving_snapshots SET manifest_json=? WHERE snapshot_id=?",
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")), written["snapshot_id"]),
    )
    con.commit()
    con.close()
    result = validate_snapshot(
        db,
        written["snapshot_id"],
        collection_inspector=_inspect,
        required_roles={"knowledge_retrieval"},
    )
    assert "manifest_member_mismatch:knowledge_retrieval:artifact_version_id" in result["errors"]


def test_failed_gate_and_evidence_integrity_refuse_snapshot(tmp_path: Path) -> None:
    db = _db(tmp_path)
    written = prepare_snapshot(db, {"knowledge_retrieval": _member("cand")}, eval_gate_ref="gate-fail", write=True)
    refused = validate_snapshot(
        db,
        written["snapshot_id"],
        collection_inspector=_inspect,
        require_gate=True,
        gate_validator=lambda _: False,
        integrity_validator=lambda _: {"ok": False, "errors": ["evidence_integrity_failed"]},
    )
    assert refused["ok"] is False
    assert {"eval_gate_not_passed", "evidence_integrity_failed"} <= set(refused["errors"])
    assert get_active_snapshot(db) is None
    written = prepare_snapshot(db, {"knowledge_retrieval": _member("cand")}, write=True)
    bad = validate_snapshot(db, written["snapshot_id"], collection_inspector=lambda _: {"exists": True, "checksum": "wrong", "count": 2})
    assert bad["ok"] is False
    assert get_active_snapshot(db) is None


def test_activation_failure_before_commit_preserves_previous(tmp_path: Path) -> None:
    db = _db(tmp_path)
    pointer = tmp_path / "active.txt"
    old = _validated(db, "old")
    activate_snapshot(db, old, pointer_path=pointer)
    new = _validated(db, "new")
    with pytest.raises(RuntimeError, match="injected"):
        activate_snapshot(db, new, pointer_path=pointer, inject_failure="before_commit")
    assert get_active_snapshot(db)["snapshot_id"] == old
    assert pointer.read_text() == "old"


def test_projection_failure_does_not_split_sqlite_authority(tmp_path: Path) -> None:
    db = _db(tmp_path)
    pointer = tmp_path / "active.txt"
    old = _validated(db, "old")
    activate_snapshot(db, old, pointer_path=pointer)
    new = _validated(db, "new")
    result = activate_snapshot(db, new, pointer_path=pointer, inject_failure="projection")
    assert result["ok"] is True and result["projection_ok"] is False
    assert get_active_snapshot(db)["snapshot_id"] == new
    assert pointer.read_text() == "old"


def test_rollback_reactivates_prior_snapshot_without_deleting_history(tmp_path: Path) -> None:
    db = _db(tmp_path)
    pointer = tmp_path / "active.txt"
    old = _validated(db, "old")
    new = _validated(db, "new")
    activate_snapshot(db, old, pointer_path=pointer)
    activate_snapshot(db, new, pointer_path=pointer)
    result = rollback_snapshot(db, old, pointer_path=pointer)
    assert result["ok"] is True
    assert get_active_snapshot(db)["snapshot_id"] == old
    assert pointer.read_text() == "old"
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM serving_snapshots").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM serving_snapshot_events WHERE action='rollback'").fetchone()[0] == 1
    con.close()


def test_pointer_repair_is_dry_run_first_and_logs_explicit_write(tmp_path: Path) -> None:
    db = _db(tmp_path)
    pointer = tmp_path / "active.txt"
    snapshot = _validated(db, "authoritative")
    activate_snapshot(db, snapshot, pointer_path=pointer)
    pointer.write_text("drifted", encoding="utf-8")
    dry = repair_pointer_projection(db, pointer)
    assert dry["drift"] is True and dry["written"] is False
    assert pointer.read_text() == "drifted"
    fixed = repair_pointer_projection(db, pointer, write=True)
    assert fixed["ok"] and fixed["written"] and pointer.read_text() == "authoritative"
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM serving_snapshot_events WHERE action='projection_repair'").fetchone()[0] == 1
    con.close()


def test_bootstrap_discovers_proofs_and_never_activates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    gate = tmp_path / "gate.json"
    gate.write_text('{"status":"PASS"}', encoding="utf-8")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO knowledge_build_runs (run_id,run_type,generated_at,input_hash,schema_version,status) VALUES ('run','merge','now','h','v1','current')")
    con.execute("INSERT INTO canonical_knowledge_units (canonical_unit_id,subject,unit_type,question,answer,confidence,status,run_id,created_at) VALUES ('cu1','s','personal_fact','q','a',1,'current','run','now')")
    con.execute("INSERT INTO knowledge_index_versions (version_id,build_id,collection_name,canonical_build_id,unit_count,status,created_at,checksum,activated_at) VALUES ('kiv','run','knowledge_collection','run',1,'active','now','ck','now')")
    con.commit(); con.close()
    monkeypatch.setattr("personal_knowledge.application.serving.snapshots._collection_inspector", lambda _: {"exists": True, "checksum": "ck", "count": 1})
    dry = bootstrap_current_snapshot(db, eval_gate=gate)
    assert dry["ok"] is True and dry["written"] is False
    assert len(dry["would_record"]) == 10
    assert get_active_snapshot(db) is None
    written = bootstrap_current_snapshot(db, eval_gate=gate, write=True)
    assert written["ok"] and written["written"] and written["status"] == "draft"
    assert get_active_snapshot(db) is None
