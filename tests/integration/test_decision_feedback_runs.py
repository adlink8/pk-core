from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL
from personal_knowledge.application.knowledge.lifecycle_events import ensure_lifecycle_schema
from personal_knowledge.intelligence.decision.runs import (
    DecisionValidationError,
    plan_run,
    publish_run,
    resolve_cognition_reference,
)
from personal_knowledge.intelligence.decision.schema import RecommendationDraft
from personal_knowledge.intelligence.runs import plan_run as plan_personal_state_run
from personal_knowledge.intelligence.runs import publish_run as publish_personal_state_run
from personal_knowledge.intelligence.schema import EvidenceReference, StateAssertion


class StubResolver:
    def resolve(self, ref: str, **_: Any) -> dict[str, Any]:
        return {
            "ref": ref,
            "artifact_type": "knowledge_unit",
            "status": "ok",
            "eligible": True,
            "metadata": {"privacy_class": "R4", "fixture": "stable"},
            "evidence_refs": [],
            "content": None,
        }


def _database(tmp_path: Path) -> tuple[Path, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "decision-feedback.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA_SQL)
    ensure_lifecycle_schema(con)
    for row in (
        ("a.personal_change", "A", "personal_change_analysis", "R4", "a-hash", "now"),
        ("a.decision_feedback", "A", "decision_feedback", "R4", "d-hash", "now"),
        ("s.knowledge_unit", "S", "canonical_knowledge", "R4", "s-hash", "now"),
    ):
        con.execute("INSERT INTO artifact_registry_entries VALUES (?,?,?,?,?,?)", row)
    con.execute(
        "INSERT INTO artifact_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("av1", "s.knowledge_unit", "v1", "source-checksum", "sqlite_table",
         "canonical_knowledge_units", "validated", "R4", None, None, "{}", "now"),
    )
    con.execute(
        "INSERT INTO serving_snapshots VALUES (?,?,?,?,?,?,?)",
        ("ss1", "{}", "snapshot-hash-1", "validated", "gate", "now", "now"),
    )
    con.execute("INSERT INTO serving_snapshot_members VALUES (?,?,?,NULL)", ("ss1", "canonical_knowledge", "av1"))
    con.execute("UPDATE serving_authority SET active_snapshot_id='ss1',activated_at='now' WHERE singleton_id=1")
    con.commit()
    con.close()
    resolver = StubResolver()
    state_run = plan_personal_state_run(
        db_path,
        [StateAssertion(
            assertion_kind="goal", provenance_class="fact", subject="user",
            domain="work", scope="personal", predicate="complete_target", value="D",
            valid_from="2026-07-18T00:00:00Z", observed_at="2026-07-18T00:00:00Z",
            evidence=(EvidenceReference(ref="ku1", artifact_type="knowledge_unit",
                       serving_role="canonical_knowledge", artifact_version_id="av1", privacy_class="R4"),),
        )],
        producer_version="phase25-v1",
        input_manifest={"source": "fixture"},
        resolver=resolver,
    )
    publish_personal_state_run(db_path, state_run, write=True, resolver=resolver)
    return db_path, state_run.run_id


def _draft(db_path: Path, source_run_id: str) -> RecommendationDraft:
    ref = resolve_cognition_reference(
        db_path, source_run_id=source_run_id, record_id=None, cognitive_type="fact"
    )
    return RecommendationDraft(
        subject="user", domain="work", scope="personal",
        recommendation_kind="next_step", target="close_target_d", horizon="next_session",
        rationale_codes=("goal_gap",), expected_benefit="complete target",
        costs_constraints=("human gates remain",), assumptions=("source remains valid",),
        contraindications=(), confidence=0.8, uncertainty="release blocked",
        expires_at="2026-08-01T00:00:00Z", support=(ref,),
    )


def _counts(db_path: Path) -> dict[str, int | str | None]:
    con = sqlite3.connect(db_path)
    try:
        return {
            name: con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("decision_runs", "decision_recommendations", "decision_support_refs", "decision_events",
                         "canonical_knowledge_units", "knowledge_lifecycle_events", "source_watermarks")
        } | {"active_snapshot": con.execute("SELECT active_snapshot_id FROM serving_authority WHERE singleton_id=1").fetchone()[0]}
    finally:
        con.close()


def test_atomic_publication_has_exactly_one_bound_sequence_one_genesis_and_replays(tmp_path: Path) -> None:
    db_path, source_run_id = _database(tmp_path)
    before = _counts(db_path)
    run = plan_run(db_path, [_draft(db_path, source_run_id)], policy_id="bounded-next-step", policy_version="v1", input_manifest={"request": "target-d"})
    assert publish_run(db_path, run, write=False)["written"] is False
    assert _counts(db_path) == before
    first = publish_run(db_path, run, write=True)
    replay = publish_run(db_path, run, write=True)
    assert first["written"] is True and replay["existing"] is True
    after = _counts(db_path)
    assert after["decision_runs"] == after["decision_recommendations"] == after["decision_support_refs"] == after["decision_events"] == 1
    assert after["canonical_knowledge_units"] == before["canonical_knowledge_units"]
    assert after["knowledge_lifecycle_events"] == before["knowledge_lifecycle_events"]
    assert after["source_watermarks"] == before["source_watermarks"]
    assert after["active_snapshot"] == before["active_snapshot"] == "ss1"
    con = sqlite3.connect(db_path)
    event = con.execute("SELECT sequence,event_type,previous_event_checksum,payload_json FROM decision_events").fetchone()
    con.close()
    assert event[0:3] == (1, "recommendation_published", "GENESIS")
    for value in (run.run_id, run.run_checksum, source_run_id, run.source_run_checksum, "ss1", "snapshot-hash-1"):
        assert value in event[3]


@pytest.mark.parametrize("failure", ["after_recommendation", "after_genesis"])
def test_fault_injection_rolls_back_run_recommendation_support_and_genesis(tmp_path: Path, failure: str) -> None:
    db_path, source_run_id = _database(tmp_path)
    run = plan_run(db_path, [_draft(db_path, source_run_id)], policy_id="p", policy_version="v1", input_manifest={})
    before = _counts(db_path)
    with pytest.raises(RuntimeError, match="injected decision publication failure"):
        publish_run(db_path, run, write=True, inject_failure_at=failure)
    assert _counts(db_path) == before


def test_cross_snapshot_unpublished_stale_and_tampered_inputs_fail_without_rows(tmp_path: Path) -> None:
    db_path, source_run_id = _database(tmp_path)
    draft = _draft(db_path, source_run_id)
    with pytest.raises(DecisionValidationError, match="cross_snapshot_support"):
        plan_run(db_path, [replace(draft, support=(replace(draft.support[0], snapshot_id="other"),))], policy_id="p", policy_version="v1", input_manifest={})
    with pytest.raises(DecisionValidationError, match="source_run_checksum_mismatch"):
        plan_run(db_path, [replace(draft, support=(replace(draft.support[0], source_run_checksum="0" * 64),))], policy_id="p", policy_version="v1", input_manifest={})
    con = sqlite3.connect(db_path)
    con.execute("DROP TRIGGER trg_personal_state_publications_immutable_delete")
    con.execute("DELETE FROM personal_state_publications WHERE run_id=?", (source_run_id,))
    con.commit(); con.close()
    with pytest.raises(DecisionValidationError, match="source_run_unpublished"):
        plan_run(db_path, [draft], policy_id="p", policy_version="v1", input_manifest={})
    assert _counts(db_path)["decision_runs"] == 0


def test_missing_or_tampered_genesis_and_manifest_are_rejected_on_replay(tmp_path: Path) -> None:
    db_path, source_run_id = _database(tmp_path)
    run = plan_run(db_path, [_draft(db_path, source_run_id)], policy_id="p", policy_version="v1", input_manifest={})
    publish_run(db_path, run, write=True)
    con = sqlite3.connect(db_path)
    con.execute("DROP TRIGGER trg_decision_events_immutable_delete")
    con.execute("DELETE FROM decision_events")
    con.commit(); con.close()
    with pytest.raises(DecisionValidationError, match="genesis_missing"):
        publish_run(db_path, run, write=True)


def test_tampered_persisted_recommendation_and_run_manifest_fail_closed(tmp_path: Path) -> None:
    db_path, source_run_id = _database(tmp_path)
    run = plan_run(db_path, [_draft(db_path, source_run_id)], policy_id="p", policy_version="v1", input_manifest={})
    publish_run(db_path, run, write=True)
    con = sqlite3.connect(db_path)
    con.execute("DROP TRIGGER trg_decision_recommendations_immutable_update")
    con.execute("UPDATE decision_recommendations SET payload_json='{}'")
    con.commit(); con.close()
    with pytest.raises(DecisionValidationError, match="recommendation_checksum_mismatch"):
        publish_run(db_path, run, write=True)

    other_db, other_source = _database(tmp_path / "other")
    other = plan_run(other_db, [_draft(other_db, other_source)], policy_id="p", policy_version="v1", input_manifest={})
    publish_run(other_db, other, write=True)
    con = sqlite3.connect(other_db)
    con.execute("DROP TRIGGER trg_decision_runs_immutable_update")
    con.execute("UPDATE decision_runs SET output_manifest_json='{}'")
    con.commit(); con.close()
    with pytest.raises(DecisionValidationError, match="existing_run_checksum_mismatch"):
        publish_run(other_db, other, write=True)


def test_policy_change_creates_new_immutable_run_on_same_phase25_source(tmp_path: Path) -> None:
    db_path, source_run_id = _database(tmp_path)
    draft = _draft(db_path, source_run_id)
    first = plan_run(db_path, [draft], policy_id="p", policy_version="v1", input_manifest={})
    second = plan_run(db_path, [draft], policy_id="p", policy_version="v2", input_manifest={})
    assert first.run_id != second.run_id
    publish_run(db_path, first, write=True)
    publish_run(db_path, second, write=True)
    assert _counts(db_path)["decision_runs"] == 2
