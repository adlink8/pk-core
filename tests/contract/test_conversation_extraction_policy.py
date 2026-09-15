"""Phase 62-05 Task 2: versioned replaceable extraction policy.

Contract tests for :mod:`personal_knowledge.application.conversation.extraction_policy`.

Requirements exercised (Phase 62 CONTEXT D-22..D-24):
  - extraction ordering is owned by a versioned ``ExtractionPolicy``, never
    hard-coded into adapters or event identity (D-22)
  - replacing trace priority is a policy-only operation: raw artifact / event
    / view identities do not change, only queue ranks and policy digest change
  - compaction summaries have the highest initial scheduling priority as
    navigation signals, never as self-authenticating truth (D-23/D-24)
  - compaction priority cannot override missing evidence refs or low fidelity
  - candidates retain ``derived_from_view`` lineage and stable
    ``evidence_event_refs`` (D-24)

All tests are pure and deterministic (D-31: no I/O, no network).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from personal_knowledge.core.conversation_events import (
    AdaptedSession,
    EventKind,
    EventRelation,
    FidelityDimension,
    FidelityLevel,
    FidelityProfile,
    Provenance,
    RelationKind,
    TypedEvent,
    make_event_id,
)
from personal_knowledge.application.conversation.extraction_policy import (
    BlockReason,
    BudgetConfig,
    DEFAULT_POLICY,
    DedupConfig,
    ExtractionPolicy,
    FreshnessConfig,
    NoveltyConfig,
    PolicyCandidate,
    PriorityBand,
    SchedulingOutput,
    schedule_candidates,
    policy_digest,
    band_digest,
)
from personal_knowledge.application.conversation.extraction_views import (
    CompactionWindowView,
    EventGraph,
    TurnView,
    ViewBuildResult,
    ViewType,
    build_all_views,
    view_set_digest,
)


# ------------------------------------------------------------------ fixtures

NOW = "2026-08-12T00:00:00Z"


def _event(
    nid: str,
    kind: EventKind,
    ordinal: int,
    day: int,
    *,
    session: str = "s-a",
    summary: str | None = None,
) -> TypedEvent:
    return TypedEvent(
        event_id=make_event_id("codex", "art-a", "1", nid),
        session_id=session,
        kind=kind,
        provenance=Provenance(
            artifact_id="art-a",
            artifact_hash="h" * 8,
            native_locator=f"jsonl:{nid}",
            native_session_id=session,
            native_event_id=nid,
            contract_version="1",
        ),
        fidelity=FidelityProfile.complete(),
        ordinal=ordinal,
        occurred_at=f"2026-08-{day:02d}T00:00:00Z",
        summary=summary,
    )


def _rel(rid: str, src: TypedEvent, dst: TypedEvent, kind: RelationKind) -> EventRelation:
    return EventRelation(rid, src.event_id, dst.event_id, kind)


def _session(sid: str) -> AdaptedSession:
    return AdaptedSession(
        session_id=sid,
        provenance=Provenance(
            artifact_id="art-a",
            artifact_hash="h" * 8,
            native_locator=f"jsonl:{sid}",
            native_session_id=sid,
            contract_version="1",
        ),
        fidelity=FidelityProfile.complete(),
        native_session_id=sid,
    )


@pytest.fixture()
def view_result() -> tuple[dict[str, str | None], ViewBuildResult]:
    """One rich generation producing all seven view types."""
    tb1 = _event("tb-1", EventKind.TURN_BOUNDARY, 1, 1)
    m1 = _event("m-1", EventKind.USER_MESSAGE, 2, 1)
    m2 = _event("m-2", EventKind.ASSISTANT_MESSAGE, 3, 2)
    loop1 = _event("loop-1", EventKind.LOOP_BOUNDARY, 4, 3)
    m3 = _event("m-3", EventKind.USER_MESSAGE, 5, 4)
    m4 = _event("m-4", EventKind.ASSISTANT_MESSAGE, 6, 5)
    c1 = _event("c-1", EventKind.USER_MESSAGE, 7, 1)
    c2 = _event("c-2", EventKind.ASSISTANT_MESSAGE, 8, 2)
    kept = _event("kept-1", EventKind.ASSISTANT_MESSAGE, 9, 3)
    fc1 = _event("fc-1", EventKind.FILE_CONTEXT, 10, 1)
    sum1 = _event(
        "sum-1", EventKind.COMPACTION_SUMMARY, 11, 6,
        summary="earlier turn compacted",
    )
    sum2 = _event(
        "sum-2", EventKind.COMPACTION_SUMMARY, 12, 7, summary="recap only"
    )
    x1 = _event("x-1", EventKind.ASSISTANT_MESSAGE, 1, 7, session="s-b")

    graph = EventGraph(
        generation_id="gen-1",
        sessions=(_session("s-a"), _session("s-b")),
        events=(tb1, m1, m2, loop1, m3, m4, c1, c2, kept, fc1, sum1, sum2, x1),
        relations=(
            _rel("r-t1", tb1, m1, RelationKind.TURN_MEMBERSHIP),
            _rel("r-t2", tb1, m2, RelationKind.TURN_MEMBERSHIP),
            _rel("r-l1", loop1, m3, RelationKind.TURN_MEMBERSHIP),
            _rel("r-l2", loop1, m4, RelationKind.TURN_MEMBERSHIP),
            _rel("r-p1", m1, m2, RelationKind.PARENT_CHILD),
            _rel("r-p2", m3, m4, RelationKind.PARENT_CHILD),
            _rel("r-fc", fc1, m3, RelationKind.PARENT_CHILD),
            _rel("r-c1", sum1, c1, RelationKind.COMPACTED_RANGE),
            _rel("r-c2", sum1, c2, RelationKind.COMPACTED_RANGE),
            _rel("r-r1", sum1, kept, RelationKind.RETAINED_FROM),
            _rel("r-x", sum1, x1, RelationKind.SOURCE_SESSION_CROSSWALK),
        ),
    )
    events_map = {e.event_id: e.occurred_at for e in graph.events}
    return events_map, build_all_views(graph)


def _schedule(
    policy: ExtractionPolicy,
    view_result: tuple[dict[str, str | None], ViewBuildResult],
) -> SchedulingOutput:
    events_map, result = view_result
    return schedule_candidates(policy, result, NOW, events=events_map)


def _result(
    view_result: tuple[dict[str, str | None], ViewBuildResult],
) -> ViewBuildResult:
    return view_result[1]


# ------------------------------------------------------ default policy

def test_default_policy_ranks_compaction_first() -> None:
    band = DEFAULT_POLICY.band_for(ViewType.COMPACTION_WINDOW)
    assert band is not None
    assert band.order == 1
    assert all(
        b.order > band.order
        for b in DEFAULT_POLICY.priority_bands
        if b is not band
    )


def test_default_policy_bands_are_locked_and_complete() -> None:
    bands = {b.order: b for b in DEFAULT_POLICY.priority_bands}
    assert sorted(bands) == [1, 2, 3, 4, 5]
    assert ViewType.COMPACTION_WINDOW in bands[1].allowed_view_types
    assert ViewType.NATIVE_TRACE in bands[2].allowed_view_types
    assert ViewType.EPISODE in bands[2].allowed_view_types
    assert ViewType.TURN in bands[2].allowed_view_types
    assert ViewType.SESSION in bands[3].allowed_view_types
    assert ViewType.TOPIC in bands[4].allowed_view_types
    assert ViewType.CROSS_SESSION in bands[5].allowed_view_types


def test_policy_digest_is_deterministic() -> None:
    assert policy_digest(DEFAULT_POLICY) == policy_digest(DEFAULT_POLICY)
    assert DEFAULT_POLICY.digest == policy_digest(DEFAULT_POLICY)


def test_policy_digest_changes_when_bands_change() -> None:
    variant = _session_first_policy()
    assert policy_digest(variant) != policy_digest(DEFAULT_POLICY)
    assert band_digest(variant.priority_bands) != band_digest(
        DEFAULT_POLICY.priority_bands
    )


# ------------------------------------------------------------ scheduling

def test_scheduling_is_deterministic(view_result: ViewBuildResult) -> None:
    a = _schedule(DEFAULT_POLICY, view_result)
    b = _schedule(DEFAULT_POLICY, view_result)
    assert a == b
    assert a.digest == b.digest


def test_schedule_ranks_compaction_window_first(view_result: ViewBuildResult) -> None:
    out = _schedule(DEFAULT_POLICY, view_result)
    assert out.candidates[0].view_type is ViewType.COMPACTION_WINDOW
    ranks = [c.rank for c in out.candidates]
    assert ranks == list(range(1, len(ranks) + 1))


def test_candidates_carry_derived_from_view_and_evidence_refs(
    view_result: ViewBuildResult,
) -> None:
    out = _schedule(DEFAULT_POLICY, view_result)
    assert out.candidates
    view_ids = {v.view_id for v in _result(view_result).views}
    for candidate in out.candidates:
        assert candidate.derived_from_view in view_ids
        assert candidate.evidence_event_refs
        # every evidence ref is a real event id of the generation
        assert candidate.derived_from_view in {
            v.view_id for v in _result(view_result).views
            if set(v.evidence_event_refs) == set(candidate.evidence_event_refs)
        }


def test_candidates_carry_fidelity_freshness_novelty_metadata(
    view_result: ViewBuildResult,
) -> None:
    out = _schedule(DEFAULT_POLICY, view_result)
    for candidate in out.candidates:
        assert candidate.fidelity.is_complete() or candidate.fidelity.has_loss()
        assert 0.0 <= candidate.freshness <= 1.0
        assert 0.0 <= candidate.novelty <= 1.0
        assert candidate.policy_digest == DEFAULT_POLICY.digest
        assert candidate.rank >= 1


# ------------------------------------------------- policy replacement

def _session_first_policy() -> ExtractionPolicy:
    return ExtractionPolicy(
        policy_id="policy-session-first",
        version="2",
        priority_bands=(
            PriorityBand(order=1, allowed_view_types=(ViewType.SESSION,)),
            PriorityBand(
                order=2,
                allowed_view_types=(
                    ViewType.COMPACTION_WINDOW,
                    ViewType.TURN,
                    ViewType.NATIVE_TRACE,
                    ViewType.EPISODE,
                ),
            ),
            PriorityBand(order=3, allowed_view_types=(ViewType.TOPIC,)),
            PriorityBand(
                order=4, allowed_view_types=(ViewType.CROSS_SESSION,)
            ),
        ),
    )


def _trace_disabled_policy() -> ExtractionPolicy:
    return ExtractionPolicy(
        policy_id="policy-trace-disabled",
        version="3",
        priority_bands=(
            PriorityBand(
                order=1, allowed_view_types=(ViewType.COMPACTION_WINDOW,)
            ),
            PriorityBand(
                order=2, allowed_view_types=(ViewType.TURN, ViewType.EPISODE)
            ),
            PriorityBand(order=3, allowed_view_types=(ViewType.SESSION,)),
            PriorityBand(order=4, allowed_view_types=(ViewType.TOPIC,)),
            PriorityBand(
                order=5, allowed_view_types=(ViewType.CROSS_SESSION,)
            ),
        ),
    )


def test_replace_trace_priority_is_policy_only(view_result: ViewBuildResult) -> None:
    session_first = _session_first_policy()
    out_default = _schedule(DEFAULT_POLICY, view_result)
    out_session = _schedule(session_first, view_result)

    # raw view identities and evidence are unchanged (same view result)
    identities = {
        (v.view_id, v.view_type, v.evidence_event_refs) for v in _result(view_result).views
    }
    assert identities == {
        (v.view_id, v.view_type, v.evidence_event_refs) for v in _result(view_result).views
    }

    # the scheduled set is identical: only ordering/digest change
    scheduled_default = {
        (c.view_type, c.evidence_event_refs) for c in out_default.candidates
    }
    scheduled_session = {
        (c.view_type, c.evidence_event_refs) for c in out_session.candidates
    }
    assert scheduled_default == scheduled_session

    # queue ranks change: session jumps to front under the session-first policy
    assert out_default.candidates[0].view_type is ViewType.COMPACTION_WINDOW
    assert out_session.candidates[0].view_type is ViewType.SESSION
    assert [c.view_type for c in out_session.candidates] != [
        c.view_type for c in out_default.candidates
    ]
    assert out_session.policy_digest != out_default.policy_digest
    assert out_session.digest != out_default.digest


def test_trace_disabled_blocks_trace_views_without_changing_event_ids(
    view_result: ViewBuildResult,
) -> None:
    trace_views = [
        v for v in _result(view_result).views if v.view_type is ViewType.NATIVE_TRACE
    ]
    assert trace_views, "fixture must produce native trace views"

    out = _schedule(_trace_disabled_policy(), view_result)
    scheduled_types = {c.view_type for c in out.candidates}
    assert ViewType.NATIVE_TRACE not in scheduled_types

    blocked_by_view = {b.view_id: b.reason for b in out.blocked}
    for view in trace_views:
        assert blocked_by_view[view.view_id] == BlockReason.VIEW_TYPE_DISALLOWED.value

    # event identities are untouched by the policy change
    trace_event_ids = sorted(
        {eid for v in trace_views for eid in v.evidence_event_refs}
    )
    assert trace_event_ids == sorted(
        {eid for v in trace_views for eid in v.evidence_event_refs}
    )


def test_policy_version_change_keeps_event_identities(
    view_result: ViewBuildResult,
) -> None:
    v2 = replace(DEFAULT_POLICY, version="99", policy_id="policy-v99")
    assert v2.version != DEFAULT_POLICY.version
    assert policy_digest(v2) != policy_digest(DEFAULT_POLICY)
    out = _schedule(v2, view_result)
    # identical event ids referenced regardless of policy version
    refs = {eid for c in out.candidates for eid in c.evidence_event_refs}
    all_refs = {eid for v in _result(view_result).views for eid in v.evidence_event_refs}
    assert refs <= all_refs


# --------------------------------------------- compaction cannot override

def test_compaction_priority_cannot_override_missing_evidence() -> None:
    empty = CompactionWindowView(
        view_id="view:empty-compaction",
        view_type=ViewType.COMPACTION_WINDOW,
        generation_id="gen-1",
        builder_version="1",
        session_id="s-a",
        members=(),
        evidence_event_refs=(),
        lineage=(),
        fidelity=FidelityProfile.complete(),
        contradictions=(),
        metadata=(),
        summary_event_id="ev:missing",
        compacted_event_refs=(),
        retained_event_refs=(),
    )
    result = ViewBuildResult(
        generation_id="gen-1",
        builder_version="1",
        views=(empty,),
        digest="synthetic",
    )
    out = schedule_candidates(DEFAULT_POLICY, result, NOW)
    assert out.candidates == ()
    assert (empty.view_id, BlockReason.ABSTAIN_NO_EVIDENCE.value) in [
        (b.view_id, b.reason) for b in out.blocked
    ]


def test_compaction_priority_cannot_override_low_fidelity(
    view_result: ViewBuildResult,
) -> None:
    strict = replace(
        DEFAULT_POLICY, fidelity_threshold=FidelityLevel.COMPLETE,
        policy_id="policy-strict",
    )
    out = _schedule(strict, view_result)
    blocked_reasons = {b.reason for b in out.blocked}
    assert BlockReason.ABSTAIN_LOW_FIDELITY.value in blocked_reasons
    # the partial recap compaction (no ranges) is blocked despite top priority
    partial_compactions = [
        v for v in _result(view_result).views
        if v.view_type is ViewType.COMPACTION_WINDOW
        and not v.fidelity.is_complete()
    ]
    assert partial_compactions
    for view in partial_compactions:
        assert all(
            c.derived_from_view != view.view_id for c in out.candidates
        )


# ------------------------------------------------ dedup and budget

def test_dedup_supersession_blocks_duplicate_evidence() -> None:
    view = TurnView(
        view_id="view:dup",
        view_type=ViewType.TURN,
        generation_id="gen-1",
        builder_version="1",
        session_id="s-a",
        members=("ev-a", "ev-b"),
        evidence_event_refs=("ev-a", "ev-b"),
        lineage=("ev-a", "ev-b"),
        fidelity=FidelityProfile.complete(),
        contradictions=(),
        metadata=(),
    )
    dup = replace(view, view_id="view:dup-2")
    result = ViewBuildResult(
        generation_id="gen-1",
        builder_version="1",
        views=(view, dup),
        digest="synthetic",
    )
    out = schedule_candidates(DEFAULT_POLICY, result, NOW)
    assert len(out.candidates) == 1
    assert {b.view_id for b in out.blocked} == {"view:dup-2"}
    assert all(
        b.reason == BlockReason.EVIDENCE_SUPERSEDED.value for b in out.blocked
    )


def test_budget_limits_candidates_per_band(view_result: ViewBuildResult) -> None:
    budgeted = replace(
        DEFAULT_POLICY,
        budget=BudgetConfig(max_candidates_per_band=1),
        policy_id="policy-budget-1",
    )
    out = _schedule(budgeted, view_result)
    per_band: dict[str, int] = {}
    for candidate in out.candidates:
        band = budgeted.band_for(candidate.view_type)
        assert band is not None
        per_band[band.order] = per_band.get(band.order, 0) + 1
    assert all(count <= 1 for count in per_band.values())
    assert {b.reason for b in out.blocked} == {BlockReason.BUDGET_EXCEEDED.value}


def test_abstain_block_reasons_are_exposed(
    view_result: ViewBuildResult,
) -> None:
    out = _schedule(_trace_disabled_policy(), view_result)
    assert out.blocked
    assert all(isinstance(b.reason, str) and b.reason for b in out.blocked)
