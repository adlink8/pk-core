from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from personal_knowledge.intelligence.decision.runs import plan_run as plan_decision_run, publish_run as publish_decision_run
from personal_knowledge.intelligence.proactive.runs import ProactiveValidationError, plan_run, publish_run
from personal_knowledge.intelligence.proactive.ranking import EvaluationContext
from personal_knowledge.intelligence.proactive.schema import CandidateDraft, CoordinationDraft, SupportReference, checksum
from personal_knowledge.intelligence.proactive.controls import ControlCommand, ControlTarget, append_control
from tests.integration.test_decision_feedback_runs import _database, _draft


def _upstream(tmp_path: Path):
    db, state_run_id = _database(tmp_path)
    decision = plan_decision_run(db, [_draft(db, state_run_id)], policy_id="p", policy_version="v1", input_manifest={})
    publish_decision_run(db, decision, write=True)
    con = sqlite3.connect(db)
    state = con.execute("SELECT output_manifest_checksum FROM personal_state_runs WHERE run_id=?", (state_run_id,)).fetchone()[0]
    seq = con.execute("SELECT publication_sequence FROM personal_state_publications WHERE run_id=?", (state_run_id,)).fetchone()[0]
    rec = con.execute("SELECT recommendation_id,payload_checksum FROM decision_recommendations").fetchone()
    con.close()
    support = SupportReference(
        authority_id="a.decision_feedback", record_type="recommendation", record_id=rec[0],
        record_checksum=rec[1], source_run_id=decision.run_id, source_run_checksum=decision.run_checksum,
        snapshot_id=decision.snapshot_id, snapshot_hash=decision.snapshot_hash,
    )
    draft = CoordinationDraft(
        relation_type="opportunity", subject="user", scope="personal",
        domains=("learning", "career"), valid_from="2026-07-18T00:00:00Z",
        valid_to="2026-08-01T00:00:00Z", observed_at="2026-07-18T00:00:00Z",
        rule_id="shared-target", rule_version="v1", confidence=0.8,
        uncertainty="fixture only", source_refs=(support,), resource_manifest=(),
    )
    return db, state_run_id, state, seq, decision, draft


def _protected(db: Path):
    con = sqlite3.connect(db)
    result = tuple(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
        "personal_state_runs", "decision_runs", "canonical_knowledge_units",
        "knowledge_lifecycle_events", "source_watermarks", "serving_snapshot_events",
    )) + (con.execute("SELECT active_snapshot_id FROM serving_authority WHERE singleton_id=1").fetchone()[0],)
    con.close()
    return result


def _candidate(draft: CoordinationDraft, *, subject: str = "user") -> CandidateDraft:
    return CandidateDraft(
        "cross_domain_opportunity", "inbox_item", subject, draft.scope, draft.domains,
        tuple(f"{ref.record_type}:{ref.record_id}" for ref in draft.source_refs),
        draft.valid_from, draft.valid_to or "2026-08-01T00:00:00Z", draft.source_refs,
        .8, .8, .8, .8, .9, .8, 0, "fixture contract only", ("fixture_only",),
    )


def _global_control(operation: str, key: str, *, expected: int = 0, rollback_of: str | None = None) -> ControlCommand:
    target = ControlTarget("a.proactive_intelligence", "global", "proactive", checksum({"global": "proactive"}))
    return ControlCommand(
        target, operation, "global", "user", checksum({"user": "fixture-owner"}),
        expected, key, "user_declared", "2026-07-18T12:00:00Z", None, rollback_of, {},
    )


def test_publication_is_atomic_idempotent_and_protected_authorities_are_unchanged(tmp_path: Path) -> None:
    db, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path)
    before = _protected(db)
    run = plan_run(db, [draft], source_run_id=state_id, source_run_checksum=state_checksum,
                   source_publication_sequence=seq, decision_run_id=decision.run_id,
                   decision_run_checksum=decision.run_checksum, coordination_policy="coord-v1",
                   ranking_policy="rank-v1", noise_policy="noise-v1", input_manifest={})
    assert publish_run(db, run, write=False)["written"] is False
    assert publish_run(db, run, write=True)["written"] is True
    assert publish_run(db, run, write=True)["existing"] is True
    assert _protected(db) == before
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM proactive_runs").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM proactive_coordination_items").fetchone()[0] == 1
    con.close()


@pytest.mark.parametrize("field", ["source_run_checksum", "decision_run_checksum", "snapshot_hash"])
def test_mixed_or_stale_bindings_publish_nothing(tmp_path: Path, field: str) -> None:
    db, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path)
    kwargs = dict(source_run_id=state_id, source_run_checksum=state_checksum,
                  source_publication_sequence=seq, decision_run_id=decision.run_id,
                  decision_run_checksum=decision.run_checksum, coordination_policy="c",
                  ranking_policy="r", noise_policy="n", input_manifest={})
    if field == "source_run_checksum": kwargs[field] = "0" * 64
    elif field == "decision_run_checksum": kwargs[field] = "0" * 64
    else: draft = replace(draft, source_refs=(replace(draft.source_refs[0], snapshot_hash="other"),))
    with pytest.raises(ProactiveValidationError):
        plan_run(db, [draft], **kwargs)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM proactive_runs").fetchone()[0] == 0


def test_fault_rolls_back_all_typed_rows(tmp_path: Path) -> None:
    db, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path)
    run = plan_run(db, [draft], source_run_id=state_id, source_run_checksum=state_checksum,
                   source_publication_sequence=seq, decision_run_id=decision.run_id,
                   decision_run_checksum=decision.run_checksum, coordination_policy="c",
                   ranking_policy="r", noise_policy="n", input_manifest={})
    with pytest.raises(RuntimeError, match="injected"):
        publish_run(db, run, write=True, inject_failure_at="after_coordination")
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM proactive_runs").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM proactive_coordination_items").fetchone()[0] == 0
    con.close()


def test_changed_input_creates_new_run_and_concurrent_replay_converges(tmp_path: Path) -> None:
    db, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path)
    common = dict(source_run_id=state_id, source_run_checksum=state_checksum,
                  source_publication_sequence=seq, decision_run_id=decision.run_id,
                  decision_run_checksum=decision.run_checksum, coordination_policy="c-v1",
                  ranking_policy="r-v1", noise_policy="n-v1")
    first = plan_run(db, [draft], input_manifest={"request": "one"}, **common)
    second = plan_run(db, [draft], input_manifest={"request": "two"}, **common)
    assert first.run_id != second.run_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish_run(db, first, write=True), range(2)))
    assert sorted((item["written"], item["existing"]) for item in results) == [(False, True), (True, False)]
    assert publish_run(db, second, write=True)["written"] is True
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM proactive_runs").fetchone()[0] == 2


def test_source_and_existing_row_tamper_fail_closed(tmp_path: Path) -> None:
    db, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path)
    run = plan_run(db, [draft], source_run_id=state_id, source_run_checksum=state_checksum,
                   source_publication_sequence=seq, decision_run_id=decision.run_id,
                   decision_run_checksum=decision.run_checksum, coordination_policy="c",
                   ranking_policy="r", noise_policy="n", input_manifest={})
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER trg_personal_state_runs_immutable_update")
    con.execute("UPDATE personal_state_runs SET output_manifest_json='{}' WHERE run_id=?", (state_id,))
    con.commit(); con.close()
    with pytest.raises(ProactiveValidationError, match="source_output_manifest_tampered"):
        publish_run(db, run, write=True)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM proactive_runs").fetchone()[0] == 0

    other, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path / "other")
    run = plan_run(other, [draft], source_run_id=state_id, source_run_checksum=state_checksum,
                   source_publication_sequence=seq, decision_run_id=decision.run_id,
                   decision_run_checksum=decision.run_checksum, coordination_policy="c",
                   ranking_policy="r", noise_policy="n", input_manifest={})
    publish_run(other, run, write=True)
    con = sqlite3.connect(other)
    con.execute("DROP TRIGGER trg_proactive_coordination_items_immutable_update")
    con.execute("UPDATE proactive_coordination_items SET payload_json='{}'")
    con.commit(); con.close()
    with pytest.raises(ProactiveValidationError, match="existing_coordination_tampered"):
        publish_run(other, run, write=True)


def test_partial_proactive_schema_fails_closed(tmp_path: Path) -> None:
    db, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path)
    con = sqlite3.connect(db)
    con.execute("DROP TABLE proactive_surface_events")
    con.commit(); con.close()
    with pytest.raises(ProactiveValidationError, match="proactive_schema_partial"):
        plan_run(db, [draft], source_run_id=state_id, source_run_checksum=state_checksum,
                 source_publication_sequence=seq, decision_run_id=decision.run_id,
                 decision_run_checksum=decision.run_checksum, coordination_policy="c",
                 ranking_policy="r", noise_policy="n", input_manifest={})


def test_candidate_support_and_evaluation_publish_atomically_and_replay(tmp_path: Path) -> None:
    db, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path)
    run = plan_run(db, [draft], source_run_id=state_id, source_run_checksum=state_checksum,
                   source_publication_sequence=seq, decision_run_id=decision.run_id,
                   decision_run_checksum=decision.run_checksum, coordination_policy="coord-v1",
                   ranking_policy="importance-v1", noise_policy="noise-v1", input_manifest={},
                   candidate_drafts=(_candidate(draft),), evaluation_context=EvaluationContext.fixed())
    assert publish_run(db, run, write=True)["written"] is True
    assert publish_run(db, run, write=True)["existing"] is True
    con = sqlite3.connect(db)
    assert tuple(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
        "proactive_runs", "proactive_candidates", "proactive_candidate_support", "proactive_evaluations"
    )) == (1, 1, 1, 1)
    assert con.execute("SELECT result FROM proactive_evaluations").fetchone()[0] == "eligible"
    con.close()


@pytest.mark.parametrize("mode", ["missing", "extra", "payload", "record"])
def test_exact_replay_rejects_candidate_support_drift(tmp_path: Path, mode: str) -> None:
    db, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path)
    run = plan_run(db, [draft], source_run_id=state_id, source_run_checksum=state_checksum,
                   source_publication_sequence=seq, decision_run_id=decision.run_id,
                   decision_run_checksum=decision.run_checksum, coordination_policy="c",
                   ranking_policy="r", noise_policy="n", input_manifest={},
                   candidate_drafts=(_candidate(draft),))
    publish_run(db, run, write=True)
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER trg_proactive_candidate_support_immutable_delete")
    con.execute("DROP TRIGGER trg_proactive_candidate_support_immutable_update")
    if mode == "missing":
        con.execute("DELETE FROM proactive_candidate_support")
    elif mode == "extra":
        row = list(con.execute("SELECT * FROM proactive_candidate_support").fetchone())
        row[0] = "pcs_" + "f" * 24
        row[4] = "forged-extra-record"
        con.execute("INSERT INTO proactive_candidate_support VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    elif mode == "payload":
        con.execute("UPDATE proactive_candidate_support SET payload_json='{}'")
    else:
        con.execute("UPDATE proactive_candidate_support SET record_checksum=?", ("0" * 64,))
    con.commit(); con.close()
    with pytest.raises(ProactiveValidationError, match="existing_support_tampered"):
        publish_run(db, run, write=True)


@pytest.mark.parametrize("failure", ["after_candidates", "after_evaluations"])
def test_candidate_publication_fault_has_zero_partial_rows(tmp_path: Path, failure: str) -> None:
    db, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path)
    run = plan_run(db, [draft], source_run_id=state_id, source_run_checksum=state_checksum,
                   source_publication_sequence=seq, decision_run_id=decision.run_id,
                   decision_run_checksum=decision.run_checksum, coordination_policy="c",
                   ranking_policy="r", noise_policy="n", input_manifest={},
                   candidate_drafts=(_candidate(draft),))
    with pytest.raises(RuntimeError, match="injected"):
        publish_run(db, run, write=True, inject_failure_at=failure)
    con = sqlite3.connect(db)
    assert tuple(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
        "proactive_runs", "proactive_coordination_items", "proactive_candidates",
        "proactive_candidate_support", "proactive_evaluations",
    )) == (0, 0, 0, 0, 0)
    con.close()


def test_prior_candidate_suppresses_exact_duplicate_but_material_change_versions(tmp_path: Path) -> None:
    db, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path)
    common = dict(source_run_id=state_id, source_run_checksum=state_checksum,
                  source_publication_sequence=seq, decision_run_id=decision.run_id,
                  decision_run_checksum=decision.run_checksum, coordination_policy="c",
                  ranking_policy="r", noise_policy="n")
    first = plan_run(db, [draft], input_manifest={"cycle": 1}, candidate_drafts=(_candidate(draft),), **common)
    publish_run(db, first, write=True)
    duplicate = plan_run(db, [draft], input_manifest={"cycle": 2}, candidate_drafts=(_candidate(draft),), **common)
    assert duplicate.candidates[0].novelty == 0
    assert duplicate.evaluations[0].reason_codes == ("duplicate_no_material_change",)
    changed = plan_run(db, [draft], input_manifest={"cycle": 3},
                       candidate_drafts=(replace(_candidate(draft), severity=.2),), **common)
    assert changed.candidates[0].dedup_key != first.candidates[0].dedup_key
    assert changed.candidates[0].novelty == 1


def test_concurrent_candidate_replay_converges_to_one_immutable_bundle(tmp_path: Path) -> None:
    db, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path)
    run = plan_run(db, [draft], source_run_id=state_id, source_run_checksum=state_checksum,
                   source_publication_sequence=seq, decision_run_id=decision.run_id,
                   decision_run_checksum=decision.run_checksum, coordination_policy="c",
                   ranking_policy="r", noise_policy="n", input_manifest={},
                   candidate_drafts=(_candidate(draft),))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish_run(db, run, write=True), range(2)))
    assert sorted((item["written"], item["existing"]) for item in results) == [(False, True), (True, False)]
    con = sqlite3.connect(db)
    assert tuple(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
        "proactive_runs", "proactive_candidates", "proactive_candidate_support", "proactive_evaluations"
    )) == (1, 1, 1, 1)
    con.close()


def test_control_frontier_change_rejects_stale_run_and_trust_veto_beats_importance(tmp_path: Path) -> None:
    db, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path)
    common = dict(
        source_run_id=state_id, source_run_checksum=state_checksum,
        source_publication_sequence=seq, decision_run_id=decision.run_id,
        decision_run_checksum=decision.run_checksum, coordination_policy="c",
        ranking_policy="r", noise_policy="n", candidate_drafts=(_candidate(draft),),
    )
    stale = plan_run(db, [draft], input_manifest={"cycle": "stale"}, **common)
    before = _protected(db)
    suppression = append_control(db, _global_control("suppress", "suppress"), write=True).event
    assert _protected(db) == before
    with pytest.raises(ProactiveValidationError, match="control_frontier_changed"):
        publish_run(db, stale, write=True)
    controlled = plan_run(db, [draft], input_manifest={"cycle": "controlled"}, **common)
    assert controlled.control_frontier_checksum != stale.control_frontier_checksum
    assert controlled.evaluations[0].result == "abstained"
    assert controlled.evaluations[0].reason_codes == ("trust_veto",)
    assert controlled.candidates[0].importance.final_score >= .8

    append_control(db, _global_control("restore", "restore", expected=1, rollback_of=suppression.event_id), write=True)
    restored = plan_run(db, [draft], input_manifest={"cycle": "restored"}, **common)
    assert restored.control_frontier_checksum != controlled.control_frontier_checksum
    assert restored.evaluations[0].result == "eligible"
    assert _protected(db) == before


def test_correction_request_never_mutates_canonical_or_lifecycle_authority(tmp_path: Path) -> None:
    db, *_ = _upstream(tmp_path)
    before = _protected(db)
    receipt = append_control(db, _global_control("correct", "correction"), write=True)
    assert receipt.event.outcome == "canonical_correction_requested"
    assert _protected(db) == before
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM knowledge_lifecycle_manifests").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM knowledge_lifecycle_events").fetchone()[0] == 0
    con.close()
