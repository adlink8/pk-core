from __future__ import annotations

import sqlite3
import json
from pathlib import Path

import pytest

from personal_knowledge.application.knowledge.lifecycle_events import (
    LifecycleError,
    apply_manifest,
    build_manifest,
    ensure_lifecycle_schema,
    event_history,
    finalize_review,
    register_manifest,
    rollback_manifest,
)
from personal_knowledge.application.ku import main as ku_main
from personal_knowledge.application.knowledge.history_knowledge_units import list_history_for_subject


SCHEMA = """
CREATE TABLE canonical_knowledge_units (
 canonical_unit_id TEXT PRIMARY KEY, subject TEXT NOT NULL, unit_type TEXT NOT NULL,
 question TEXT NOT NULL, answer TEXT NOT NULL, confidence REAL NOT NULL,
 lifecycle TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
 run_id TEXT NOT NULL, merge_reason TEXT, supersedes_id TEXT, created_at TEXT NOT NULL
);
"""


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "lifecycle.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for unit_id, answer in (("old", "old answer"), ("new", "new answer")):
        con.execute(
            "INSERT INTO canonical_knowledge_units VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (unit_id, "subject", "preference", "question", answer, .9, "current", "current", 1, "run", None, None, "2026-01-01"),
        )
    con.commit(); con.close()
    return path


def _manifest(action: str = "supersede", **overrides):
    raw = {
        "unit_id": "old", "action": action, "expected_version": 1,
        "expected_lifecycle": "current", "target_unit_id": "new",
        "reason": "human reviewed transition", "evidence_refs": ["cm|1"],
        "decision": "approve", "changes": {},
    }
    raw.update(overrides)
    return build_manifest(
        [raw], source_snapshot_id="ss_test", reviewer_id="human-reviewer-01",
        reviewed_at="2026-07-17T12:00:00Z",
    )


def _state(path: Path):
    con = sqlite3.connect(path)
    value = con.execute("SELECT lifecycle,version,supersedes_id,answer FROM canonical_knowledge_units WHERE canonical_unit_id='old'").fetchone()
    events = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='knowledge_lifecycle_events'").fetchone()[0]
    event_count = con.execute("SELECT COUNT(*) FROM knowledge_lifecycle_events").fetchone()[0] if events else 0
    con.close()
    return value, event_count


def test_reviewed_manifest_applies_and_emits_event(tmp_path: Path) -> None:
    db, manifest = _db(tmp_path), _manifest()
    register_manifest(db, manifest, write=True)
    result = apply_manifest(db, manifest, actor_id="operator-01", evidence_validator=lambda refs: True)
    assert result["applied"] == 1
    state, count = _state(db)
    assert state[:3] == ("superseded", 2, "new")
    assert count == 1
    history = event_history(db, "old")
    assert history[0]["lifecycle_before"] == "current"
    assert history[0]["lifecycle_after"] == "superseded"
    assert len(history[0]["reviewer_id_hash"]) == 64
    report = list_history_for_subject(db, "subject", include_all_lifecycle=True)
    old = next(row for row in report.rows if row["unit_id"] == "old")
    assert old["lifecycle_events"][0]["event_type"] == "supersede"
    assert "answer" not in old["lifecycle_events"][0]


def test_finalize_review_binds_all_decisions_and_records_rejections(tmp_path: Path) -> None:
    proposal = build_manifest(
        [
            {"unit_id": "old", "action": "conflict", "expected_version": 1, "expected_lifecycle": "current", "reason": "r1", "evidence_refs": ["cm|1"]},
            {"unit_id": "new", "action": "conflict", "expected_version": 1, "expected_lifecycle": "current", "reason": "r2", "evidence_refs": ["cm|2"]},
        ],
        source_snapshot_id="ss_test",
    )
    review = {
        "proposal_manifest_id": proposal["manifest_id"], "proposal_checksum": proposal["manifest_checksum"],
        "reviewer_id": "human-reviewer-01", "reviewed_at": "2026-07-17T12:00:00Z",
        "decisions": [{"unit_id": "old", "decision": "approve"}, {"unit_id": "new", "decision": "reject"}],
    }
    pp, rp, out = tmp_path / "proposal.json", tmp_path / "review.json", tmp_path / "reviewed.json"
    pp.write_text(json.dumps(proposal), encoding="utf-8")
    rp.write_text(json.dumps(review), encoding="utf-8")
    result = finalize_review(pp, rp, out)
    assert [x["unit_id"] for x in result["actions"]] == ["old"]
    assert result["review_receipt"]["rejected_unit_ids"] == ["new"]
    assert out.exists()


def test_finalize_review_accepts_explicit_llm_provenance(tmp_path: Path) -> None:
    proposal = build_manifest(
        [{"unit_id": "old", "action": "conflict", "expected_version": 1,
          "expected_lifecycle": "current", "reason": "evidence conflict",
          "evidence_refs": ["cm|1"]}],
        source_snapshot_id="ss_test",
    )
    review = {
        "proposal_manifest_id": proposal["manifest_id"],
        "proposal_checksum": proposal["manifest_checksum"],
        "reviewer_type": "llm", "reviewer_id": "openai-gpt-5.6-luna",
        "model_id": "gpt-5.6-luna", "review_run_id": "lifecycle-run-1",
        "prompt_version": "phase24-lifecycle-v1", "reviewed_at": "2026-07-18T12:00:00Z",
        "decisions": [{"unit_id": "old", "decision": "approve", "confidence": 0.9}],
    }
    pp, rp, out = tmp_path / "proposal.json", tmp_path / "review.json", tmp_path / "reviewed.json"
    pp.write_text(json.dumps(proposal), encoding="utf-8")
    rp.write_text(json.dumps(review), encoding="utf-8")
    result = finalize_review(pp, rp, out)
    assert result["reviewer_type"] == "llm"
    assert result["model_id"] == "gpt-5.6-luna"


def test_finalize_review_persists_auditable_all_rejected_receipt(tmp_path: Path) -> None:
    proposal = build_manifest(
        [{"unit_id": "old", "action": "conflict", "expected_version": 1,
          "expected_lifecycle": "current", "reason": "unsupported proposal",
          "evidence_refs": ["cm|missing"]}],
        source_snapshot_id="ss_test",
    )
    review = {
        "proposal_manifest_id": proposal["manifest_id"],
        "proposal_checksum": proposal["manifest_checksum"],
        "reviewer_type": "llm", "reviewer_id": "openai-gpt-5.6-luna",
        "model_id": "gpt-5.6-luna", "review_run_id": "reject-run",
        "prompt_version": "phase24-lifecycle-v1", "reviewed_at": "2026-07-18T12:00:00Z",
        "decisions": [{"unit_id": "old", "decision": "reject", "confidence": 0.99}],
    }
    pp, rp, out = tmp_path / "proposal.json", tmp_path / "review.json", tmp_path / "receipt.json"
    pp.write_text(json.dumps(proposal), encoding="utf-8")
    rp.write_text(json.dumps(review), encoding="utf-8")
    receipt = finalize_review(pp, rp, out)
    assert receipt["review_status"] == "no_actions_approved"
    assert receipt["rejected_unit_ids"] == ["old"]
    with pytest.raises(LifecycleError):
        register_manifest(_db(tmp_path), receipt, write=False)


def test_tampered_stale_and_unreviewed_fail_without_changes(tmp_path: Path) -> None:
    db = _db(tmp_path)
    manifest = _manifest()
    register_manifest(db, manifest, write=True)
    tampered = {**manifest, "source_snapshot_id": "other"}
    with pytest.raises(LifecycleError, match="checksum"):
        apply_manifest(db, tampered, actor_id="operator-01", evidence_validator=lambda refs: True)
    stale = _manifest(expected_version=2)
    register_manifest(db, stale, write=True)
    with pytest.raises(LifecycleError, match="stale"):
        apply_manifest(db, stale, actor_id="operator-01", evidence_validator=lambda refs: True)
    unreviewed = build_manifest(
        [{"unit_id": "old", "action": "conflict", "expected_version": 1, "expected_lifecycle": "current", "reason": "r", "evidence_refs": ["cm|1"]}],
        source_snapshot_id="ss_test",
    )
    with pytest.raises(LifecycleError, match="approve|reviewer"):
        register_manifest(db, unreviewed, write=True)
    assert _state(db)[0][:2] == ("current", 1)


def test_fault_injection_rolls_back_unit_and_event(tmp_path: Path) -> None:
    db, manifest = _db(tmp_path), _manifest("conflict", target_unit_id="")
    register_manifest(db, manifest, write=True)
    with pytest.raises(RuntimeError, match="injected"):
        apply_manifest(db, manifest, actor_id="operator-01", evidence_validator=lambda refs: True, inject_failure_after=1)
    assert _state(db) == (("current", 1, None, "old answer"), 0)


def test_manifest_rollback_restores_state_with_linked_event(tmp_path: Path) -> None:
    db, manifest = _db(tmp_path), _manifest()
    register_manifest(db, manifest, write=True)
    applied = apply_manifest(db, manifest, actor_id="operator-01", evidence_validator=lambda refs: True)
    rolled = rollback_manifest(db, manifest["manifest_id"], actor_id="operator-02")
    assert rolled["ok"] is True
    state, count = _state(db)
    assert state[:3] == ("current", 3, None)
    assert count == 2
    history = event_history(db, "old")
    assert history[-1]["event_type"] == "rollback"
    assert history[-1]["rollback_of_event_id"] == applied["events"][0]


def test_correction_records_hashes_and_restore_is_explicit(tmp_path: Path) -> None:
    db = _db(tmp_path)
    correction = _manifest("correct", target_unit_id="", changes={"answer": "corrected answer"})
    register_manifest(db, correction, write=True)
    apply_manifest(db, correction, actor_id="operator-01", evidence_validator=lambda refs: True)
    state, _ = _state(db)
    assert state == ("current", 2, None, "corrected answer")
    con = sqlite3.connect(db)
    hashes = con.execute("SELECT before_hash,after_hash,before_value_json,after_value_json FROM knowledge_unit_corrections").fetchone()
    con.close()
    assert hashes[0] != hashes[1]
    assert "old answer" in hashes[2] and "corrected answer" in hashes[3]

    restore = _manifest("restore", target_unit_id="", expected_version=2)
    register_manifest(db, restore, write=True)
    apply_manifest(db, restore, actor_id="operator-01", evidence_validator=lambda refs: True)
    assert _state(db)[0][:2] == ("current", 3)


def test_deprecate_sets_deprecated_lifecycle_and_rolls_back(tmp_path: Path) -> None:
    db = _db(tmp_path)
    manifest = _manifest("deprecate", target_unit_id="")
    register_manifest(db, manifest, write=True)
    apply_manifest(db, manifest, actor_id="operator-01", evidence_validator=lambda refs: True)
    state, _ = _state(db)
    assert state[:3] == ("deprecated", 2, None)
    history = event_history(db, "old")
    assert history[0]["event_type"] == "deprecate"
    rolled = rollback_manifest(db, manifest["manifest_id"], actor_id="operator-02")
    assert rolled["ok"] is True
    assert _state(db)[0][:2] == ("current", 3)


def test_ensure_schema_migrates_legacy_actions_check_for_deprecate(tmp_path: Path) -> None:
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE knowledge_lifecycle_manifests (
            manifest_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL,
            manifest_checksum TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('proposed','reviewed','applied','rejected','rolled_back')),
            reviewer_id_hash TEXT, reviewed_at TEXT, actor_id TEXT,
            source_snapshot_id TEXT, created_at TEXT NOT NULL, applied_at TEXT
        );
        CREATE TABLE knowledge_lifecycle_actions (
            action_id TEXT PRIMARY KEY,
            manifest_id TEXT NOT NULL REFERENCES knowledge_lifecycle_manifests(manifest_id),
            ordinal INTEGER NOT NULL,
            unit_id TEXT NOT NULL REFERENCES canonical_knowledge_units(canonical_unit_id),
            action TEXT NOT NULL CHECK(action IN ('supersede','conflict','correct','restore')),
            expected_version INTEGER NOT NULL, expected_lifecycle TEXT NOT NULL,
            target_unit_id TEXT, reason TEXT NOT NULL,
            evidence_refs_json TEXT NOT NULL, changes_json TEXT NOT NULL,
            UNIQUE(manifest_id, ordinal), UNIQUE(manifest_id, unit_id)
        );
        """
    )
    con.execute(
        "INSERT INTO knowledge_lifecycle_manifests VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("m1", "{}", "sum1", "applied", None, None, None, None, "2026-01-01", None),
    )
    con.execute(
        "INSERT INTO knowledge_lifecycle_actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("a1", "m1", 1, "old", "conflict", 1, "current", None, "r", "[]", "{}"),
    )
    con.commit()
    con.execute("PRAGMA foreign_keys=ON")  # 镜像 connect_rw：FK 开启下的迁移
    # 第三方表引用 events：迁移的 RENAME 不得改写其 FK 文本
    # （实证事故 2026-07-27：knowledge_unit_corrections 被改写到中间表名，
    # DROP 中间表后留下悬空 FK，doctor sqlite_foreign_keys FAIL）
    con.executescript(
        """
        CREATE TABLE knowledge_unit_corrections (
            correction_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES knowledge_lifecycle_events(event_id),
            unit_id TEXT NOT NULL REFERENCES canonical_knowledge_units(canonical_unit_id)
        );
        """
    )
    ensure_lifecycle_schema(con)
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='knowledge_lifecycle_actions'").fetchone()[0]
    assert "'deprecate'" in sql
    assert con.execute("SELECT action_id, action FROM knowledge_lifecycle_actions").fetchone() == ("a1", "conflict")
    assert "pre_deprecate" not in con.execute(
        "SELECT sql FROM sqlite_master WHERE name='knowledge_unit_corrections'"
    ).fetchone()[0]
    con.execute(
        "INSERT INTO knowledge_lifecycle_actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("a2", "m1", 2, "new", "deprecate", 1, "current", None, "r", "[]", "{}"),
    )
    con.commit()
    # 模拟曾被污染的状态：FK=ON 下 rename 会把 events 的 FK 引用改写到中间表
    con.execute("ALTER TABLE knowledge_lifecycle_actions RENAME TO knowledge_lifecycle_actions_pre_deprecate")
    assert "pre_deprecate" in con.execute(
        "SELECT sql FROM sqlite_master WHERE name='knowledge_lifecycle_events'"
    ).fetchone()[0]
    ensure_lifecycle_schema(con)
    assert "pre_deprecate" not in con.execute(
        "SELECT sql FROM sqlite_master WHERE name='knowledge_lifecycle_events'"
    ).fetchone()[0]
    assert con.execute("SELECT COUNT(*) FROM knowledge_lifecycle_actions").fetchone()[0] == 2
    con.close()


def test_cli_status_is_strict_and_direct_heuristic_write_is_retired(tmp_path: Path, capsys) -> None:
    db = _db(tmp_path)
    code = ku_main(["lifecycle-status", "--strict", "--db", str(db)])
    assert code == 1
    assert '"ok": false' in capsys.readouterr().out.lower()
    code = ku_main(["reconcile", "--write", "--i-know", "--db", str(db)])
    assert code == 2
    assert "retired" in capsys.readouterr().err.lower()
