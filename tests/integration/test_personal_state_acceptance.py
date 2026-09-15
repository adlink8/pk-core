from __future__ import annotations

import json
import sqlite3

from personal_knowledge.intelligence.cli import main as cli_main, run_acceptance
from tests.contract.test_personal_state_interfaces import _service
from tests.integration.test_personal_state_runs import _database


def _analysis_counts(db_path):
    con = sqlite3.connect(db_path)
    try:
        return tuple(
            con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "personal_state_runs", "personal_state_assertions", "personal_state_evidence",
                "personal_state_changes", "personal_state_risks",
            )
        )
    finally:
        con.close()


def test_acceptance_replays_without_any_authority_or_analysis_mutation(tmp_path) -> None:
    db_path, _, _, _ = _service(tmp_path)
    pointer = tmp_path / "active.txt"
    pointer.write_text("fixture-active", encoding="utf-8")
    counts_before = _analysis_counts(db_path)

    first = run_acceptance(db_path, pointer_path=pointer, limit=5)
    second = run_acceptance(db_path, pointer_path=pointer, limit=5)

    assert first == second
    assert first["ok"] is True
    assert first["mutations"] == 0
    assert first["private_bodies"] == 0
    assert first["candidate"]["persisted_rows"] == 0
    assert first["fingerprints"]["unchanged"] is True
    assert first["fingerprints"]["before"] == first["fingerprints"]["after"]
    assert first["run_plan"]["run_plan_id"].startswith("psp_")
    assert len(first["run_plan"]["checksum"]) == 64
    assert _analysis_counts(db_path) == counts_before


def test_acceptance_reflects_global_review_and_fixture_lifecycle_gates(tmp_path) -> None:
    db_path, _, _, _ = _service(tmp_path)
    result = run_acceptance(db_path, pointer_path=tmp_path / "missing.txt")

    assert result["status"] == "release_blocked"
    assert result["phase24"]["release_blocked"] is True
    assert result["phase24"]["human_review_strict"]["ok"] is True
    assert result["phase24"]["lifecycle_strict"]["ok"] is False
    statuses = {row["checkpoint"]: row["status"] for row in result["phase24"]["checkpoints"]}
    assert set(statuses.values()) == {"passed"}


def test_acceptance_cli_emits_metadata_only_json(tmp_path, capsys) -> None:
    db_path, _, _, _ = _service(tmp_path)
    pointer = tmp_path / "active.txt"
    pointer.write_text("fixture-active", encoding="utf-8")
    code = cli_main([
        "--db", str(db_path), "acceptance", "--dry-run", "--metadata-only",
        "--active-pointer", str(pointer), "--json",
    ])
    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert result["dry_run"] is result["metadata_only"] is True
    assert result["mutations"] == result["private_bodies"] == 0


def test_acceptance_allows_only_explicit_empty_analysis_states(tmp_path) -> None:
    applied = _database(tmp_path)
    applied_result = run_acceptance(applied, pointer_path=tmp_path / "missing.txt")
    assert applied_result["ok"] is True
    assert applied_result["run_plan"]["candidate_reason"] == "run_missing"
    assert applied_result["intelligence_gate"]["ok"] is True

    unapplied_dir = tmp_path / "unapplied"
    unapplied_dir.mkdir()
    unapplied = _database(unapplied_dir)
    con = sqlite3.connect(unapplied)
    con.executescript(
        "DROP TABLE personal_state_risks;"
        "DROP TABLE personal_state_changes;"
        "DROP TABLE personal_state_evidence;"
        "DROP TABLE personal_state_assertions;"
        "DROP TABLE personal_state_publications;"
        "DROP TABLE personal_state_runs;"
    )
    con.close()
    unapplied_result = run_acceptance(unapplied, pointer_path=tmp_path / "missing.txt")
    assert unapplied_result["ok"] is True
    assert unapplied_result["run_plan"]["candidate_reason"] == "analysis_schema_unapplied"
    assert unapplied_result["intelligence_gate"]["ok"] is True


def test_acceptance_blocks_corrupted_assertion_state(tmp_path) -> None:
    db_path, _, _, second = _service(tmp_path)
    con = sqlite3.connect(db_path)
    con.execute("DROP TRIGGER trg_personal_state_assertions_immutable_update")
    con.execute(
        "UPDATE personal_state_assertions SET value_json='\"tampered\"' WHERE run_id=?",
        (second.run_id,),
    )
    con.commit()
    con.close()
    result = run_acceptance(db_path, pointer_path=tmp_path / "missing.txt")
    assert result["ok"] is False
    assert result["status"] == "release_blocked"
    assert result["candidate"]["computed"] is False
    assert result["intelligence_gate"]["ok"] is False
    assert result["intelligence_gate"]["error"]["code"] == "assertion_payload_mismatch"


def test_acceptance_blocks_committed_run_without_publication_sequence(tmp_path) -> None:
    db_path, _, _, second = _service(tmp_path)
    con = sqlite3.connect(db_path)
    con.execute("DROP TRIGGER trg_personal_state_publications_immutable_delete")
    con.execute("DELETE FROM personal_state_publications WHERE run_id=?", (second.run_id,))
    con.commit()
    con.close()
    result = run_acceptance(db_path, pointer_path=tmp_path / "missing.txt")
    assert result["ok"] is False
    assert result["candidate"]["computed"] is False
    assert result["intelligence_gate"]["error"]["code"] == "publication_sequence_missing"
