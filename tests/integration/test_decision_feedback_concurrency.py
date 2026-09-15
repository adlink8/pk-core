from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from personal_knowledge.application.knowledge.lifecycle_events import ensure_lifecycle_schema
from personal_knowledge.application.knowledge.migrate_add_knowledge_unit_tables import SCHEMA_SQL
from personal_knowledge.intelligence.decision.runs import plan_run, publish_run, resolve_cognition_reference
from personal_knowledge.intelligence.decision.effectiveness import EffectivenessRule, assess_outcome, load_outcome
from personal_knowledge.intelligence.decision.schema import RecommendationDraft
from personal_knowledge.intelligence.decision.state_machine import (
    DecisionStateError,
    project_history,
    record_action,
    record_assessment,
    record_confirmation,
    record_outcome,
)
from personal_knowledge.intelligence.runs import plan_run as plan_state_run
from personal_knowledge.intelligence.runs import publish_run as publish_state_run
from personal_knowledge.intelligence.schema import EvidenceReference, StateAssertion


class Resolver:
    def resolve(self, ref: str, **_: Any) -> dict[str, Any]:
        return {"ref": ref, "artifact_type": "knowledge_unit", "status": "ok", "eligible": True,
                "metadata": {"privacy_class": "R4"}, "evidence_refs": [], "content": None}


def _published(tmp_path: Path, *, expires_at: str = "2026-08-01T00:00:00Z") -> tuple[Path, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "decision-state.sqlite"
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA_SQL)
    ensure_lifecycle_schema(con)
    for row in (("a.personal_change", "A", "personal_change_analysis", "R4", "a", "now"),
                ("a.decision_feedback", "A", "decision_feedback", "R4", "d", "now"),
                ("s.knowledge_unit", "S", "canonical_knowledge", "R4", "s", "now")):
        con.execute("INSERT INTO artifact_registry_entries VALUES (?,?,?,?,?,?)", row)
    con.execute("INSERT INTO artifact_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("av1", "s.knowledge_unit", "v1", "source", "sqlite_table", "canonical_knowledge_units",
                 "validated", "R4", None, None, "{}", "now"))
    con.execute("INSERT INTO serving_snapshots VALUES (?,?,?,?,?,?,?)",
                ("ss1", "{}", "snapshot-hash", "validated", "gate", "now", "now"))
    con.execute("INSERT INTO serving_snapshot_members VALUES (?,?,?,NULL)", ("ss1", "canonical_knowledge", "av1"))
    con.execute("UPDATE serving_authority SET active_snapshot_id='ss1',activated_at='now' WHERE singleton_id=1")
    con.commit(); con.close()
    resolver = Resolver()
    source = plan_state_run(db, [StateAssertion(
        assertion_kind="goal", provenance_class="fact", subject="user", domain="work", scope="personal",
        predicate="complete_target", value="D", valid_from="2026-07-18T00:00:00Z",
        observed_at="2026-07-18T00:00:00Z", evidence=(EvidenceReference(
            ref="ku1", artifact_type="knowledge_unit", serving_role="canonical_knowledge",
            artifact_version_id="av1", privacy_class="R4"),))], producer_version="phase25-v1",
        input_manifest={"source": "fixture"}, resolver=resolver)
    publish_state_run(db, source, write=True, resolver=resolver)
    ref = resolve_cognition_reference(db, source_run_id=source.run_id, record_id=None, cognitive_type="fact")
    draft = RecommendationDraft(
        subject="user", domain="work", scope="personal", recommendation_kind="next_step",
        target="close_target_d", horizon="next_session", rationale_codes=("goal_gap",),
        expected_benefit="complete target", costs_constraints=("human gates remain",),
        assumptions=("source remains valid",), contraindications=(), confidence=.8,
        uncertainty="release blocked", expires_at=expires_at, support=(ref,))
    run = plan_run(db, [draft], policy_id="bounded-next-step", policy_version="v1", input_manifest={})
    publish_run(db, run, write=True)
    return db, run.recommendations[0]


def _confirm(db: Path, rec: Any, **changes: Any) -> Any:
    args = dict(recommendation_id=rec.recommendation_id, recommendation_checksum=rec.payload_checksum,
                decision="accept", actor_class="user", actor_identity_hash="1" * 64,
                reason_code="user_selected", expected_sequence=1, idempotency_key="confirm-1",
                occurred_at="2026-07-18T01:00:00Z")
    args.update(changes)
    return record_confirmation(db, **args)


def _action(db: Path, rec: Any, **changes: Any) -> Any:
    args = dict(recommendation_id=rec.recommendation_id, recommendation_checksum=rec.payload_checksum,
                action_state="planned", source_class="user_attested", actor_class="user",
                actor_identity_hash="1" * 64, reason_code="user_planned", expected_sequence=2,
                idempotency_key="action-1", occurred_at="2026-07-18T01:01:00Z")
    args.update(changes)
    return record_action(db, **args)


def test_confirmation_and_action_extend_genesis_without_premise_or_execution_side_effects(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM personal_state_assertions").fetchone()[0]
    confirmation = _confirm(db, rec)
    assert confirmation.sequence == 2
    assert project_history(db, rec.recommendation_id).confirmation_state == "accepted"
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM decision_actions").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM personal_state_assertions").fetchone()[0] == before
    con.close()
    action = _action(db, rec)
    state = project_history(db, rec.recommendation_id)
    assert action.sequence == 3 and state.action_state == "planned"
    assert [event.sequence for event in state.events] == [1, 2, 3]
    assert state.events[1].previous_event_checksum == state.events[0].payload_checksum
    assert state.events[2].previous_event_checksum == state.events[1].payload_checksum


@pytest.mark.parametrize("decision", ["reject", "defer"])
def test_decision_transitions_do_not_authorize_action(tmp_path: Path, decision: str) -> None:
    db, rec = _published(tmp_path)
    _confirm(db, rec, decision=decision)
    with pytest.raises(DecisionStateError, match="illegal_action_transition"):
        _action(db, rec)


def test_revoke_is_only_valid_after_accept_and_before_action(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    _confirm(db, rec)
    receipt = _confirm(db, rec, decision="revoke_before_action", expected_sequence=2, idempotency_key="revoke")
    assert receipt.sequence == 3
    assert project_history(db, rec.recommendation_id).confirmation_state == "revoked"
    with pytest.raises(DecisionStateError, match="illegal_action_transition"):
        _action(db, rec, expected_sequence=3)


def test_complete_action_lifecycle_is_sequence_ordered_even_at_same_second(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    stamp = "2026-07-18T01:00:00Z"
    _confirm(db, rec, occurred_at=stamp)
    _action(db, rec, occurred_at=stamp)
    _action(db, rec, action_state="started", expected_sequence=3,
            idempotency_key="action-started", reason_code="user_started", occurred_at=stamp)
    _action(db, rec, action_state="completed", expected_sequence=4,
            idempotency_key="action-completed", reason_code="user_completed", occurred_at=stamp)
    state = project_history(db, rec.recommendation_id)
    assert state.action_state == "completed"
    assert [event.sequence for event in state.events] == [1, 2, 3, 4, 5]
    assert all(
        current.previous_event_checksum == previous.payload_checksum
        for previous, current in zip(state.events, state.events[1:])
    )


@pytest.mark.parametrize("terminal", ["abandoned", "not_taken"])
def test_planned_action_can_end_without_claiming_execution(tmp_path: Path, terminal: str) -> None:
    db, rec = _published(tmp_path)
    _confirm(db, rec)
    _action(db, rec)
    _action(db, rec, action_state=terminal, expected_sequence=3,
            idempotency_key=f"action-{terminal}", reason_code=f"user_{terminal}")
    assert project_history(db, rec.recommendation_id).action_state == terminal


def test_expiry_actor_checksum_and_executable_payload_fail_closed(tmp_path: Path) -> None:
    db, rec = _published(tmp_path, expires_at="2026-07-18T00:30:00Z")
    for changes, code in (({"occurred_at": "2026-07-18T01:00:00Z"}, "recommendation_expired"),
                          ({"actor_class": "agent"}, "human_actor_required"),
                          ({"recommendation_checksum": "0" * 64}, "recommendation_checksum_mismatch")):
        with pytest.raises(DecisionStateError, match=code):
            _confirm(db, rec, **changes)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM decision_confirmations").fetchone()[0] == 0
    con.close()

    db2, rec2 = _published(tmp_path / "other")
    _confirm(db2, rec2)
    with pytest.raises(DecisionStateError, match="forbidden_action_field"):
        _action(db2, rec2, metadata={"command": "run something"})


def test_idempotency_replay_conflict_and_stale_two_writer_contention(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    first = _confirm(db, rec)
    replay = _confirm(db, rec)
    assert replay == first
    with pytest.raises(DecisionStateError, match="idempotency_conflict"):
        _confirm(db, rec, reason_code="changed")

    db2, rec2 = _published(tmp_path / "race")
    def writer(key: str) -> str:
        try:
            _confirm(db2, rec2, idempotency_key=key)
            return "written"
        except DecisionStateError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, ("writer-a", "writer-b")))
    assert sorted(results) == ["stale_expected_sequence", "written"]


@pytest.mark.parametrize("failure", ["after_typed_record", "after_event"])
def test_injected_insert_failures_roll_back_both_rows(tmp_path: Path, failure: str) -> None:
    db, rec = _published(tmp_path)
    with pytest.raises(RuntimeError, match="injected decision state failure"):
        _confirm(db, rec, inject_failure_at=failure)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM decision_confirmations").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 1
    con.close()


@pytest.mark.parametrize("failure", ["after_typed_record", "after_event"])
def test_injected_action_failures_roll_back_typed_row_and_event(tmp_path: Path, failure: str) -> None:
    db, rec = _published(tmp_path)
    _confirm(db, rec)
    with pytest.raises(RuntimeError, match="injected decision state failure"):
        _action(db, rec, inject_failure_at=failure)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM decision_actions").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 2
    con.close()


def test_missing_tampered_or_out_of_order_genesis_fails_closed(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER trg_decision_events_immutable_update")
    con.execute("UPDATE decision_events SET payload_json='{}' WHERE sequence=1")
    con.commit(); con.close()
    with pytest.raises(DecisionStateError, match="event_checksum_mismatch"):
        project_history(db, rec.recommendation_id)


def test_missing_genesis_and_checksum_chain_tamper_fail_closed(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER trg_decision_events_immutable_delete")
    con.execute("DELETE FROM decision_events WHERE sequence=1")
    con.commit(); con.close()
    with pytest.raises(DecisionStateError, match="genesis_missing"):
        project_history(db, rec.recommendation_id)

    db2, rec2 = _published(tmp_path / "chain")
    _confirm(db2, rec2)
    con = sqlite3.connect(db2)
    con.execute("DROP TRIGGER trg_decision_events_immutable_update")
    con.execute("UPDATE decision_events SET previous_event_checksum=? WHERE sequence=2", ("0" * 64,))
    con.commit(); con.close()
    with pytest.raises(DecisionStateError, match="event_chain_mismatch"):
        project_history(db2, rec2.recommendation_id)


def _complete(db: Path, rec: Any) -> None:
    _confirm(db, rec)
    _action(db, rec)
    _action(db, rec, action_state="started", expected_sequence=3,
            idempotency_key="action-started", reason_code="user_started")
    _action(db, rec, action_state="completed", expected_sequence=4,
            idempotency_key="action-completed", reason_code="user_completed")


def _outcome(db: Path, rec: Any, **changes: Any) -> Any:
    con = sqlite3.connect(db)
    action_id, action_checksum = con.execute(
        "SELECT action_id,payload_checksum FROM decision_actions WHERE action_state='completed'"
    ).fetchone()
    support = con.execute(
        "SELECT cognitive_type,authority_id,record_id,record_checksum,source_run_id,snapshot_id,snapshot_hash "
        "FROM decision_support_refs WHERE recommendation_id=?", (rec.recommendation_id,)
    ).fetchone()
    con.close()
    args = dict(
        recommendation_id=rec.recommendation_id,
        recommendation_checksum=rec.payload_checksum,
        action_id=action_id,
        action_checksum=action_checksum,
        source_class="user_reported",
        actor_class="user",
        actor_identity_hash="1" * 64,
        measurement_definition="weekly completed focus blocks",
        metric="focus_blocks",
        baseline_value=2.0,
        target_value=4.0,
        observed_value=5.0,
        unit="count/week",
        direction="increase",
        window_start="2026-07-18T01:00:00Z",
        window_end="2026-07-25T01:00:00Z",
        adherence_status="adhered",
        evidence_refs=(dict(zip(
            ("cognitive_type", "authority_id", "record_id", "record_checksum", "source_run_id", "snapshot_id", "snapshot_hash"),
            support,
        )),),
        confidence=.8,
        uncertainty=(),
        confounders=(),
        concurrent_actions=(),
        expected_sequence=5,
        idempotency_key="outcome-1",
        occurred_at="2026-07-25T01:01:00Z",
    )
    args.update(changes)
    return record_outcome(db, **args)


def test_outcome_extends_completed_action_with_idempotent_checksum_chain(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    _complete(db, rec)
    first = _outcome(db, rec)
    assert first.sequence == 6
    assert _outcome(db, rec) == first
    state = project_history(db, rec.recommendation_id)
    assert state.events[-1].event_type == "outcome"
    with pytest.raises(DecisionStateError, match="idempotency_conflict"):
        _outcome(db, rec, observed_value=6.0)


def test_outcome_rejects_invalid_action_binding_sequence_and_cross_snapshot_ref(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    _complete(db, rec)
    for changes, code in (
        ({"action_checksum": "0" * 64}, "action_checksum_mismatch"),
        ({"expected_sequence": 4}, "stale_expected_sequence"),
        ({"evidence_refs": ({
            "cognitive_type": "fact", "authority_id": "a.personal_change", "record_id": "missing",
            "record_checksum": "c" * 64, "source_run_id": "missing", "snapshot_id": "other",
            "snapshot_hash": "other",
        },)}, "cross_snapshot_evidence"),
    ):
        with pytest.raises(DecisionStateError, match=code):
            _outcome(db, rec, **changes)


def test_concurrent_outcome_writers_allow_one_sequence_owner(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    _complete(db, rec)
    def writer(key: str) -> str:
        try:
            _outcome(db, rec, idempotency_key=key)
            return "written"
        except DecisionStateError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, ("outcome-a", "outcome-b")))
    assert sorted(results) == ["stale_expected_sequence", "written"]


def test_assessment_is_immutable_non_causal_and_tamper_checked(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    _complete(db, rec)
    outcome_receipt = _outcome(db, rec)
    outcome = load_outcome(db, outcome_receipt.record_id)
    rule = EffectivenessRule("observed_goal_attainment", "1", "focus_blocks", "count/week", "increase", 86400)
    assessment = assess_outcome(outcome, rule, action_state="completed")
    receipt = record_assessment(
        db, assessment=assessment, expected_sequence=6, idempotency_key="assessment-1",
        occurred_at="2026-07-25T01:02:00Z",
    )
    assert receipt.sequence == 7
    assert project_history(db, rec.recommendation_id).events[-1].event_type == "assessment"
    assert record_assessment(
        db, assessment=assessment, expected_sequence=6, idempotency_key="assessment-1",
        occurred_at="2026-07-25T01:02:00Z",
    ) == receipt
    con = sqlite3.connect(db)
    assert con.execute("SELECT causal_claim FROM decision_effectiveness").fetchone()[0] == 0
    con.execute("DROP TRIGGER trg_decision_outcomes_immutable_update")
    con.execute("UPDATE decision_outcomes SET payload_json='{}'")
    con.commit(); con.close()
    with pytest.raises(ValueError, match="outcome_checksum_mismatch"):
        load_outcome(db, outcome_receipt.record_id)


@pytest.mark.parametrize(
    ("outcome_changes", "assessment_changes"),
    (
        ({"observed_value": None}, {"verdict": "effective", "limitations": (), "confidence": .8}),
        ({"confounders": ("seasonality",)}, {"verdict": "effective", "limitations": (), "confidence": .8}),
    ),
)
def test_assessment_write_rejects_forged_derivation(tmp_path, outcome_changes, assessment_changes) -> None:
    db, rec = _published(tmp_path)
    _complete(db, rec)
    outcome_receipt = _outcome(db, rec, **outcome_changes)
    outcome = load_outcome(db, outcome_receipt.record_id)
    rule = EffectivenessRule("observed_goal_attainment", "1", "focus_blocks", "count/week", "increase", 86400)
    derived = assess_outcome(outcome, rule, action_state="completed")
    forged = replace(derived, **assessment_changes)

    with pytest.raises(DecisionStateError, match="assessment_derivation_mismatch"):
        record_assessment(
            db, assessment=forged, expected_sequence=6, idempotency_key="assessment-forged",
            occurred_at="2026-07-25T01:02:00Z",
        )
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM decision_effectiveness").fetchone()[0] == 0


def test_assessment_write_rejects_unknown_rule_version(tmp_path: Path) -> None:
    db, rec = _published(tmp_path)
    _complete(db, rec)
    outcome_receipt = _outcome(db, rec)
    outcome = load_outcome(db, outcome_receipt.record_id)
    assessment = assess_outcome(
        outcome,
        EffectivenessRule("unregistered", "999", "focus_blocks", "count/week", "increase", 86400),
        action_state="completed",
    )

    with pytest.raises(DecisionStateError, match="unknown_effectiveness_rule"):
        record_assessment(
            db, assessment=assessment, expected_sequence=6, idempotency_key="assessment-unknown",
            occurred_at="2026-07-25T01:02:00Z",
        )
