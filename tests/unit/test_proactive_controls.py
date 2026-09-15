from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from personal_knowledge.intelligence.proactive.controls import (
    ControlCommand,
    ControlTarget,
    active_control_frontier,
    append_control,
    project_controls,
)
from personal_knowledge.intelligence.proactive.schema import checksum
from tests.integration.test_proactive_runs import _candidate, _upstream
from personal_knowledge.intelligence.proactive.runs import plan_run, publish_run


AS_OF = "2026-07-18T12:00:00Z"
ACTOR = checksum({"user": "fixture-owner"})


def _published_candidate(tmp_path):
    db, state_id, state_checksum, seq, decision, draft = _upstream(tmp_path)
    run = plan_run(
        db, [draft], source_run_id=state_id, source_run_checksum=state_checksum,
        source_publication_sequence=seq, decision_run_id=decision.run_id,
        decision_run_checksum=decision.run_checksum, coordination_policy="c",
        ranking_policy="r", noise_policy="n", input_manifest={},
        candidate_drafts=(_candidate(draft),),
    )
    publish_run(db, run, write=True)
    candidate = run.candidates[0]
    return db, ControlTarget("a.proactive_intelligence", "candidate", candidate.candidate_id, candidate.payload_checksum)


def _command(target, operation, key, *, expected=0, scope="global", expires_at=None,
             rollback_of=None, details=None):
    return ControlCommand(
        target=target, operation=operation, scope=scope, actor_class="user",
        actor_identity_hash=ACTOR, expected_sequence=expected, idempotency_key=key,
        reason_code="user_declared", created_at=AS_OF, expires_at=expires_at,
        rollback_of_event_id=rollback_of, details=details or {},
    )


@pytest.mark.parametrize("operation", [
    "limit_scope", "suppress", "snooze", "revoke", "correct",
    "mark_not_useful", "mark_wrong_timing", "restore",
])
def test_closed_control_operation_vocabulary(operation: str) -> None:
    assert operation in ControlCommand.OPERATIONS


def test_user_owned_append_projection_and_exact_replay(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    command = _command(target, "suppress", "one")
    first = append_control(db, command, write=True)
    replay = append_control(db, command, write=True)
    assert first.written is True and replay.existing is True
    assert first.event == replay.event
    projected = project_controls(db, targets=(target,), as_of=AS_OF)
    assert projected.eligible is False
    assert projected.reason_codes == ("trust_veto", "suppressed_by_user")


def test_scope_precedence_denial_and_snooze_expiry(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    global_target = ControlTarget("a.proactive_intelligence", "global", "proactive", checksum({"global": "proactive"}))
    append_control(db, _command(global_target, "suppress", "g"), write=True)
    append_control(db, _command(target, "snooze", "e", expires_at="2026-07-18T13:00:00Z"), write=True)
    before = project_controls(db, targets=(global_target, target), as_of=AS_OF)
    after = project_controls(db, targets=(global_target, target), as_of="2026-07-18T14:00:00Z")
    assert before.eligible is False and before.winning_event_id is not None
    assert before.reason_codes == ("trust_veto", "snoozed_by_user")
    assert after.eligible is False and after.reason_codes == ("trust_veto", "suppressed_by_user")


def test_restore_is_compensating_and_double_restore_fails(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    suppression = append_control(db, _command(target, "suppress", "s"), write=True).event
    restored = append_control(
        db, _command(target, "restore", "r", expected=1, rollback_of=suppression.event_id), write=True,
    ).event
    assert restored.rollback_of_event_id == suppression.event_id
    assert restored.before_projected_checksum != restored.after_projected_checksum
    assert project_controls(db, targets=(target,), as_of=AS_OF).eligible is True
    with pytest.raises(ValueError, match="invalid_restore"):
        append_control(db, _command(target, "restore", "r2", expected=2, rollback_of=suppression.event_id), write=True)


def test_correction_is_request_only_and_limit_scope_is_fail_closed(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    correction = append_control(db, _command(target, "correct", "c", details={"interpretation_code": "user_correction"}), write=True)
    assert correction.event.outcome == "canonical_correction_requested"
    append_control(db, _command(target, "limit_scope", "l", expected=1, details={"allowed_scopes": ["project:alpha"]}), write=True)
    denied = project_controls(db, targets=(target,), as_of=AS_OF, scope="project:beta")
    allowed = project_controls(db, targets=(target,), as_of=AS_OF, scope="project:alpha")
    assert denied.eligible is False and "scope_limited" in denied.reason_codes
    assert allowed.eligible is True


def test_frontier_is_deterministic_and_changes_only_on_append(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    before = active_control_frontier(db)
    append_control(db, _command(target, "mark_not_useful", "f"), write=False)
    assert active_control_frontier(db) == before
    append_control(db, _command(target, "mark_not_useful", "f"), write=True)
    assert active_control_frontier(db) != before


def test_human_actor_and_timezone_are_mandatory(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    with pytest.raises(ValueError, match="human_actor_required"):
        append_control(db, replace(_command(target, "suppress", "bot"), actor_class="agent"), write=True)
    with pytest.raises(ValueError, match="timezone_required"):
        append_control(db, replace(_command(target, "suppress", "time"), created_at="2026-07-18T12:00:00"), write=True)


def test_restore_changes_only_projection_at_or_after_restore_time(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    suppression = append_control(
        db, replace(_command(target, "suppress", "s"), created_at="2026-07-18T12:00:00Z"), write=True,
    ).event
    append_control(
        db, replace(_command(target, "restore", "r", expected=1, rollback_of=suppression.event_id),
                    created_at="2026-07-18T13:00:00Z"), write=True,
    )
    assert project_controls(db, targets=(target,), as_of="2026-07-18T12:30:00Z").eligible is False
    assert project_controls(db, targets=(target,), as_of="2026-07-18T13:00:00Z").eligible is True
    assert project_controls(db, targets=(target,), as_of="2026-07-18T13:30:00Z").eligible is True


def test_target_specificity_precedes_scope_specificity(tmp_path) -> None:
    db, exact = _published_candidate(tmp_path)
    global_target = ControlTarget("a.proactive_intelligence", "global", "proactive", checksum({"global": "proactive"}))
    domain_target = ControlTarget("a.proactive_intelligence", "domain", "career", checksum({"domain": "career"}))
    policy_target = ControlTarget("a.proactive_intelligence", "policy", "importance-v1", checksum({"policy": "importance-v1"}))
    append_control(db, _command(global_target, "suppress", "g", scope="project:alpha"), write=True)
    append_control(db, _command(domain_target, "suppress", "d", scope="policy:importance-v1"), write=True)
    append_control(db, _command(policy_target, "suppress", "p", scope="domain:career"), write=True)
    append_control(db, _command(exact, "limit_scope", "e", scope="global", details={"allowed_scopes": ["project:alpha"]}), write=True)
    projection = project_controls(db, targets=(global_target, domain_target, policy_target, exact),
                                  as_of=AS_OF, scope="project:alpha", domains=("career",), policies=("importance-v1",))
    assert projection.eligible is True
    assert projection.winning_event_id is not None


def test_same_target_stream_sequence_overrides_created_at_order(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    append_control(
        db,
        replace(_command(target, "suppress", "first"), created_at="2026-07-18T12:00:00+08:00"),
        write=True,
    )
    latest = append_control(
        db,
        replace(
            _command(target, "snooze", "second", expected=1, expires_at="2026-07-18T08:00:00Z"),
            created_at="2026-07-18T03:00:00Z",
        ),
        write=True,
    ).event

    projection = project_controls(db, targets=(target,), as_of="2026-07-18T06:00:00Z")

    assert projection.winning_event_id == latest.event_id
    assert projection.reason_codes == ("trust_veto", "snoozed_by_user")


def test_cross_target_streams_compare_parsed_utc_instants(tmp_path) -> None:
    db, _ = _published_candidate(tmp_path)
    earlier_target = ControlTarget(
        "a.proactive_intelligence", "policy", "policy-a", checksum({"policy": "policy-a"}),
    )
    later_target = ControlTarget(
        "a.proactive_intelligence", "policy", "policy-b", checksum({"policy": "policy-b"}),
    )
    append_control(
        db,
        replace(_command(earlier_target, "suppress", "earlier"), created_at="2026-07-18T12:00:00+08:00"),
        write=True,
    )
    later = append_control(
        db,
        replace(
            _command(later_target, "snooze", "later", expires_at="2026-07-18T08:00:00Z"),
            created_at="2026-07-18T05:00:00Z",
        ),
        write=True,
    ).event

    projection = project_controls(
        db, targets=(earlier_target, later_target), as_of="2026-07-18T06:00:00Z",
    )

    assert projection.winning_event_id == later.event_id
    assert projection.reason_codes == ("trust_veto", "snoozed_by_user")


def test_cross_target_equal_instants_use_stable_target_tie_break(tmp_path) -> None:
    db, _ = _published_candidate(tmp_path)
    target_a = ControlTarget(
        "a.proactive_intelligence", "policy", "policy-a", checksum({"policy": "policy-a"}),
    )
    target_b = ControlTarget(
        "a.proactive_intelligence", "policy", "policy-b", checksum({"policy": "policy-b"}),
    )
    append_control(
        db,
        replace(_command(target_a, "suppress", "a"), created_at="2026-07-18T13:00:00+08:00"),
        write=True,
    )
    winner = append_control(
        db,
        replace(
            _command(target_b, "snooze", "b", expires_at="2026-07-18T08:00:00Z"),
            created_at="2026-07-18T05:00:00Z",
        ),
        write=True,
    ).event

    forward = project_controls(db, targets=(target_a, target_b), as_of="2026-07-18T06:00:00Z")
    reverse = project_controls(db, targets=(target_b, target_a), as_of="2026-07-18T06:00:00Z")

    assert forward.winning_event_id == winner.event_id
    assert reverse.winning_event_id == winner.event_id


def test_cross_target_tie_break_is_stable_after_stream_sequence_selection(tmp_path) -> None:
    db, _ = _published_candidate(tmp_path)
    target_a = ControlTarget(
        "a.proactive_intelligence", "policy", "policy-a", checksum({"policy": "policy-a"}),
    )
    target_b = ControlTarget(
        "a.proactive_intelligence", "policy", "policy-b", checksum({"policy": "policy-b"}),
    )
    append_control(
        db,
        replace(_command(target_a, "suppress", "a1"), created_at="2026-07-18T04:00:00Z"),
        write=True,
    )
    latest_a = append_control(
        db,
        replace(
            _command(target_a, "snooze", "a2", expected=1, expires_at="2026-07-18T08:00:00Z"),
            created_at="2026-07-18T05:00:00Z",
        ),
        write=True,
    ).event
    append_control(
        db,
        replace(_command(target_b, "suppress", "b1"), created_at="2026-07-18T03:00:00Z"),
        write=True,
    )

    projection = project_controls(db, targets=(target_a, target_b), as_of="2026-07-18T06:00:00Z")

    assert projection.winning_event_id == latest_a.event_id
    assert projection.reason_codes == ("trust_veto", "snoozed_by_user")
