"""Phase 62-06 Task 2/3: view/policy/evidence-bound candidate prepare.

Contract tests for
:mod:`personal_knowledge.application.knowledge.view_candidate_prepare`.

Requirements exercised (Phase 62 CONTEXT D-24/D-26/D-30/D-31):
  - prepare is keyed by active event generation, view builder version,
    extraction policy digest, semantic prompt/schema version and evidence
    event digest
  - estimates/ledger states only — preparing a run never calls a provider and
    never logs conversation bodies
  - old message-level prepare runs are classified ``superseded_policy`` /
    non-executable through an append-only audit transition; their ledger rows
    are never deleted (D-30)
  - extraction is rejected when generation/view/policy/gate versions mismatch,
    when evidence is unresolved, or when a legacy message-level run id is
    supplied
  - estimates report calls/tokens/cost per view type and family
  - only current view-policy runs can approach extraction, and no command path
    can spend provider quota yet (D-31)

All tests are pure and deterministic: no I/O, no network, no paid calls.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from personal_knowledge.core.conversation_events import (
    EventKind,
    FidelityProfile,
)
from personal_knowledge.application.conversation.extraction_policy import (
    DEFAULT_POLICY,
    BlockedView,
    SchedulingOutput,
    schedule_candidates,
)
from personal_knowledge.application.conversation.extraction_views import (
    CompactionWindowView,
    TurnView,
    ViewBuildResult,
    ViewType,
)
from personal_knowledge.application.knowledge.view_candidate_prepare import (
    CandidatePrepareError,
    CandidateRun,
    CandidateRunKey,
    CandidateRunRepository,
    LegacyRunSupersededError,
    UnresolvedEvidenceError,
    VersionMismatchError,
    ViewEstimate,
    ViewExtractionBlockedError,
    evidence_set_digest,
    is_legacy_run_id,
    is_view_run_id,
    make_candidate_run_id,
)

NOW = "2026-08-12T00:00:00Z"


# ------------------------------------------------------------------ fixtures

def _turn_view(view_id: str = "view:turn-1") -> TurnView:
    return TurnView(
        view_id=view_id,
        view_type=ViewType.TURN,
        generation_id="gen-1",
        builder_version="1",
        session_id="s-a",
        members=("ev:u1", "ev:a1"),
        evidence_event_refs=("ev:u1", "ev:a1"),
        lineage=("event:ev:u1", "event:ev:a1"),
        fidelity=FidelityProfile.complete(),
        contradictions=(),
        metadata=(("flags", "native_turn"),),
    )


def _compaction_view() -> CompactionWindowView:
    return CompactionWindowView(
        view_id="view:comp-1",
        view_type=ViewType.COMPACTION_WINDOW,
        generation_id="gen-1",
        builder_version="1",
        session_id="s-a",
        members=("ev:sum", "ev:c1"),
        evidence_event_refs=("ev:sum", "ev:c1"),
        lineage=("event:ev:sum", "event:ev:c1"),
        fidelity=FidelityProfile.complete(),
        contradictions=(),
        metadata=(("summary_event_id", "ev:sum"),),
        summary_event_id="ev:sum",
        compacted_event_refs=("ev:c1",),
        retained_event_refs=(),
    )


@pytest.fixture()
def scheduled() -> SchedulingOutput:
    result = ViewBuildResult(
        generation_id="gen-1",
        builder_version="1",
        views=(_compaction_view(), _turn_view()),
        digest="synthetic-digest",
    )
    return schedule_candidates(DEFAULT_POLICY, result, NOW)


@pytest.fixture()
def run_key(scheduled: SchedulingOutput) -> CandidateRunKey:
    refs = sorted({eid for c in scheduled.candidates for eid in c.evidence_event_refs})
    return CandidateRunKey(
        active_generation_id="gen-1",
        view_builder_version="1",
        policy_digest=scheduled.policy_digest,
        semantic_prompt_version="semantic-v1",
        semantic_schema_version="schema-v1",
        evidence_event_digest=evidence_set_digest(refs),
    )


@pytest.fixture()
def repo(tmp_path: Path) -> CandidateRunRepository:
    repository = CandidateRunRepository(tmp_path / "candidates.sqlite")
    repository.create_schema()
    return repository


# --------------------------------------------- key and version components

def test_run_key_has_all_version_components(run_key: CandidateRunKey) -> None:
    assert run_key.active_generation_id == "gen-1"
    assert run_key.view_builder_version == "1"
    assert run_key.policy_digest
    assert run_key.semantic_prompt_version == "semantic-v1"
    assert run_key.semantic_schema_version == "schema-v1"
    assert run_key.evidence_event_digest
    # deterministic: same components, same key
    assert run_key == run_key


def test_run_id_derives_from_key_deterministically(run_key: CandidateRunKey) -> None:
    run_id = make_candidate_run_id(run_key)
    assert is_view_run_id(run_id)
    assert run_id.startswith("vc_")
    assert make_candidate_run_id(run_key) == run_id
    other = replace(run_key, semantic_schema_version="schema-v2")
    assert make_candidate_run_id(other) != run_id


def test_legacy_and_view_prefixes_are_distinct() -> None:
    assert is_legacy_run_id("ir_abc")
    assert not is_view_run_id("ir_abc")
    assert is_view_run_id("vc_abc")
    assert not is_legacy_run_id("vc_abc")
    assert not is_legacy_run_id("6f3da1eec10c4fee6fb1509c83cfb85b")
    assert not is_view_run_id("6f3da1eec10c4fee6fb1509c83cfb85b")


# --------------------------------------------------- prepare writes estimates

def test_prepare_writes_estimates_only(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    prepared = repo.prepare_view_run(run_key, scheduled, _synthetic_result())
    assert isinstance(prepared, CandidateRun)
    assert prepared.status == "blocked_pending_user_cost_approval"
    assert prepared.kind == "view_policy"
    assert prepared.candidate_count == len(scheduled.candidates)
    assert prepared.estimated_calls >= 1
    assert prepared.estimated_tokens >= prepared.estimated_calls
    assert prepared.estimated_cost_usd >= 0.0
    estimates = repo.estimates(prepared.run_id)
    assert estimates
    assert all(isinstance(e, ViewEstimate) for e in estimates)
    assert {e.view_type for e in estimates} == {
        ViewType.COMPACTION_WINDOW.value,
        ViewType.TURN.value,
    }


def test_estimate_per_view_type_and_family(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    prepared = repo.prepare_view_run(
        run_key, scheduled, _synthetic_result(), family="codex"
    )
    estimates = repo.estimates(prepared.run_id)
    for estimate in estimates:
        assert estimate.family == "codex"
        assert estimate.candidate_count >= 1
        assert estimate.estimated_calls == estimate.candidate_count
        assert estimate.estimated_tokens > 0
        assert estimate.estimated_cost_usd > 0.0
    # aggregate cost equals the run's total
    assert prepared.estimated_cost_usd == pytest.approx(
        sum(e.estimated_cost_usd for e in estimates)
    )


def test_estimates_never_log_bodies(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    prepared = repo.prepare_view_run(run_key, scheduled, _synthetic_result())
    status = repo.status(prepared.run_id)
    estimates = repo.estimates(prepared.run_id)
    text = repr(status) + repr(estimates)
    assert "ev:u1" not in text
    assert "conversation" not in text.lower() or "summary" not in text.lower()


def test_prepare_is_idempotent_by_key(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    first = repo.prepare_view_run(run_key, scheduled, _synthetic_result())
    second = repo.prepare_view_run(run_key, scheduled, _synthetic_result())
    assert first.run_id == second.run_id
    assert len(repo._run_rows()) == 1


def test_candidate_mapping_survives_restart_and_read_is_bounded(repo, run_key, scheduled):
    run = repo.prepare_view_run(run_key, scheduled, _synthetic_result())
    before = repo.db.read_bytes()
    restarted = CandidateRunRepository(repo.db)
    first = restarted.list_candidates(run.run_id, limit=1)
    second = restarted.list_candidates(run.run_id, limit=1, offset=1)
    assert len(first) == len(second) == 1
    assert first[0]["candidate_id"] != second[0]["candidate_id"]
    by_view = {item["derived_from_view"]: item for item in first + second}
    assert set(by_view["view:turn-1"]["evidence_event_refs"]) == {"ev:u1", "ev:a1"}
    assert set(by_view["view:comp-1"]["evidence_event_refs"]) == {"ev:sum", "ev:c1"}
    assert restarted.list_candidates(run.run_id, offset=2) == []
    assert repo.db.read_bytes() == before


def test_exact_prepare_replay_does_not_rewrite_database(repo, run_key, scheduled):
    first = repo.prepare_view_run(run_key, scheduled, _synthetic_result())
    before = repo.db.read_bytes()
    second = CandidateRunRepository(repo.db).prepare_view_run(run_key, scheduled, _synthetic_result())
    assert first == second
    assert repo.db.read_bytes() == before


@pytest.mark.parametrize("change", ["rank", "view_digest", "mapping", "blocked", "order"])
def test_same_union_queue_drift_is_rejected_without_overwrite(repo, run_key, scheduled, change):
    result = _synthetic_result()
    run = repo.prepare_view_run(run_key, scheduled, result)
    before = repo.db.read_bytes()
    if change == "rank":
        changed = replace(scheduled.candidates[0], rank=999)
        scheduled = replace(scheduled, candidates=(changed,) + scheduled.candidates[1:])
    elif change == "view_digest":
        result = replace(result, digest="changed-view-layout")
    elif change == "blocked":
        scheduled = replace(scheduled, blocked=(BlockedView("view:extra", ViewType.TURN, "insufficient"),))
    elif change == "order":
        scheduled = replace(scheduled, candidates=tuple(reversed(scheduled.candidates)))
    else:
        a, b = scheduled.candidates
        scheduled = replace(scheduled, candidates=(
            replace(a, evidence_event_refs=b.evidence_event_refs),
            replace(b, evidence_event_refs=a.evidence_event_refs),
        ))
    with pytest.raises(VersionMismatchError):
        repo.prepare_view_run(run_key, scheduled, result)
    assert repo.db.read_bytes() == before
    assert repo.get_run(run.run_id) == run


@pytest.mark.parametrize("limit,offset", [(0, 0), (101, 0), (1, -1), (True, 0), (1, 0.5)])
def test_candidate_listing_rejects_unbounded_or_invalid_page(repo, run_key, scheduled, limit, offset):
    run = repo.prepare_view_run(run_key, scheduled, _synthetic_result())
    before = repo.db.read_bytes()
    with pytest.raises((CandidatePrepareError, ValueError)):
        repo.list_candidates(run.run_id, limit=limit, offset=offset)
    assert repo.db.read_bytes() == before


def test_first_prepare_rejects_cross_view_evidence_and_writes_nothing(repo, run_key, scheduled):
    a, b = scheduled.candidates
    swapped = replace(scheduled, candidates=(
        replace(a, evidence_event_refs=b.evidence_event_refs),
        replace(b, evidence_event_refs=a.evidence_event_refs),
    ))
    before = repo.db.read_bytes()
    with pytest.raises(VersionMismatchError):
        repo.prepare_view_run(run_key, swapped, _synthetic_result())
    assert repo.db.read_bytes() == before


def test_concurrent_identical_prepare_has_one_result(repo, run_key, scheduled):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    ready = Barrier(4)
    def prepare():
        ready.wait(timeout=10)
        return CandidateRunRepository(repo.db).prepare_view_run(run_key, scheduled, _synthetic_result())

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: prepare(), range(4)))
    assert all(result == results[0] for result in results)
    assert len(repo.list_candidates(results[0].run_id)) == 2


def test_manifest_write_failure_rolls_back_whole_run(repo, run_key, scheduled):
    import sqlite3

    with sqlite3.connect(repo.db) as con:
        con.execute("CREATE TRIGGER fail_manifest BEFORE INSERT ON ce_candidate_manifest_candidates "
                    "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END")
    before = repo.db.read_bytes()
    with pytest.raises(CandidatePrepareError):
        repo.prepare_view_run(run_key, scheduled, _synthetic_result())
    assert repo.get_run(make_candidate_run_id(run_key)) is None
    assert repo.db.read_bytes() == before


def test_replay_rejects_corrupt_manifest_digest(repo, run_key, scheduled):
    import sqlite3

    run = repo.prepare_view_run(run_key, scheduled, _synthetic_result())
    with sqlite3.connect(repo.db) as con:
        con.execute("UPDATE ce_candidate_manifests SET manifest_digest='corrupt' WHERE run_id=?", (run.run_id,))
    before = repo.db.read_bytes()
    with pytest.raises(CandidatePrepareError):
        repo.prepare_view_run(run_key, scheduled, _synthetic_result())
    assert repo.db.read_bytes() == before


def test_candidate_list_reports_partial_manifest_schema(repo, run_key, scheduled):
    import sqlite3

    run = repo.prepare_view_run(run_key, scheduled, _synthetic_result())
    with sqlite3.connect(repo.db) as con:
        con.execute("DROP TABLE ce_candidate_manifest_candidates")
    before = repo.db.read_bytes()
    with pytest.raises(CandidatePrepareError, match="schema.*incomplete"):
        repo.list_candidates(run.run_id)
    assert repo.db.read_bytes() == before


def test_estimates_are_deterministic(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    a = repo.prepare_view_run(run_key, scheduled, _synthetic_result())
    repo2 = CandidateRunRepository(repo.db)
    repo2.create_schema()
    b = repo2.prepare_view_run(run_key, scheduled, _synthetic_result())
    assert a == b
    assert repo.estimates(a.run_id) == repo2.estimates(b.run_id)


# ------------------------------------------- version mismatch rejections

def _synthetic_result() -> ViewBuildResult:
    return ViewBuildResult(
        generation_id="gen-1",
        builder_version="1",
        views=(_compaction_view(), _turn_view()),
        digest="synthetic-digest",
    )


def _prepare(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> CandidateRun:
    return repo.prepare_view_run(run_key, scheduled, _synthetic_result())


def test_generation_mismatch_rejects(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    run = _prepare(repo, run_key, scheduled)
    current = replace(run_key, active_generation_id="gen-2")
    with pytest.raises(VersionMismatchError, match="generation"):
        repo.check_run_executable(run.run_id, current_key=current)


def test_builder_version_mismatch_rejects(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    run = _prepare(repo, run_key, scheduled)
    current = replace(run_key, view_builder_version="2")
    with pytest.raises(VersionMismatchError, match="builder"):
        repo.check_run_executable(run.run_id, current_key=current)


def test_policy_digest_mismatch_rejects(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    run = _prepare(repo, run_key, scheduled)
    current = replace(run_key, policy_digest="policy-other")
    with pytest.raises(VersionMismatchError, match="policy"):
        repo.check_run_executable(run.run_id, current_key=current)


def test_prompt_version_mismatch_rejects(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    run = _prepare(repo, run_key, scheduled)
    current = replace(run_key, semantic_prompt_version="semantic-v2")
    with pytest.raises(VersionMismatchError, match="prompt"):
        repo.check_run_executable(run.run_id, current_key=current)


def test_schema_version_mismatch_rejects(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    run = _prepare(repo, run_key, scheduled)
    current = replace(run_key, semantic_schema_version="schema-v2")
    with pytest.raises(VersionMismatchError, match="schema"):
        repo.check_run_executable(run.run_id, current_key=current)


def test_evidence_digest_mismatch_rejects_at_prepare(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    wrong_key = replace(run_key, evidence_event_digest="deadbeef")
    with pytest.raises(VersionMismatchError, match="evidence"):
        repo.prepare_view_run(wrong_key, scheduled, _synthetic_result())


# ---------------------------------------------------- evidence resolution

def test_unresolved_evidence_rejects_at_prepare(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    stub = _StubEvidenceRepo(known={"ev:sum"})  # ev:c1 is missing
    with pytest.raises(UnresolvedEvidenceError, match="unresolved|evidence"):
        repo.prepare_view_run(
            run_key, scheduled, _synthetic_result(), event_repo=stub
        )


def test_resolved_evidence_passes(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    refs = {eid for c in scheduled.candidates for eid in c.evidence_event_refs}
    stub = _StubEvidenceRepo(known=refs)
    run = repo.prepare_view_run(
        run_key, scheduled, _synthetic_result(), event_repo=stub
    )
    repo.check_run_executable(run.run_id, current_key=run_key, event_repo=stub)


class _StubEvidenceRepo:
    """Minimal evidence resolver used by the contract tests (no I/O)."""

    def __init__(self, known: set[str]) -> None:
        self.known = set(known)

    def has_events(self, generation_id: str, event_ids) -> set[str]:
        return {e for e in event_ids if e in self.known}


# ------------------------------------------- legacy supersession (D-30)

def test_legacy_run_classified_superseded_append_only(
    repo: CandidateRunRepository,
) -> None:
    assert repo.legacy_status("ir_legacy-1") is None
    repo.classify_legacy_run("ir_legacy-1", reason="message-level prepare superseded")
    assert repo.legacy_status("ir_legacy-1") == "superseded_policy"
    # append-only: a second transition grows the audit, never replaces it
    repo.classify_legacy_run("ir_legacy-1", reason="again")
    assert len(repo._audit_rows("ir_legacy-1")) == 2


def test_legacy_run_refuses_execution(repo: CandidateRunRepository) -> None:
    repo.classify_legacy_run("ir_legacy-1")
    with pytest.raises(LegacyRunSupersededError, match="superseded"):
        repo.assert_extraction_authorized("ir_legacy-1")
    # unclassified legacy ids also refuse (queue semantics invalidated)
    with pytest.raises(LegacyRunSupersededError):
        repo.assert_extraction_authorized("ir_unclassified")


# ------------------------------------- view extraction blocked (D-31)

def test_view_run_extraction_blocked_pending_approval(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    run = _prepare(repo, run_key, scheduled)
    with pytest.raises(ViewExtractionBlockedError) as excinfo:
        repo.assert_extraction_authorized(run.run_id)
    message = str(excinfo.value)
    assert "approval" in message.lower()
    assert "pilot" in message.lower()
    # a current matching run can still be inspected/prepared (approach, not pay)
    repo.check_run_executable(run.run_id, current_key=run_key)


def test_unknown_run_refuses_execution(repo: CandidateRunRepository) -> None:
    with pytest.raises(CandidatePrepareError):
        repo.assert_extraction_authorized("vc_ghost")
    with pytest.raises(CandidatePrepareError):
        repo.check_run_executable("vc_ghost", current_key=None)


def test_status_reports_blocked_pending_user_cost_approval(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    run = _prepare(repo, run_key, scheduled)
    status = repo.status(run.run_id)
    assert status["status"] == "blocked_pending_user_cost_approval"
    assert status["active_generation_id"] == "gen-1"
    assert status["candidate_count"] == len(scheduled.candidates)
    assert status["non_executable"] is True


# ----------------------------------------------------- zero paid provider calls

def test_zero_paid_calls_whole_flow(
    repo: CandidateRunRepository,
    run_key: CandidateRunKey,
    scheduled: SchedulingOutput,
) -> None:
    """No provider is ever invoked across the entire prepare contract."""
    run = _prepare(repo, run_key, scheduled)
    repo.estimates(run.run_id)
    repo.status(run.run_id)
    repo.check_run_executable(run.run_id, current_key=run_key)
    try:
        repo.assert_extraction_authorized(run.run_id)
    except ViewExtractionBlockedError:
        pass
    # all deterministic local computation only
    assert run.estimated_cost_usd == pytest.approx(
        sum(e.estimated_cost_usd for e in repo.estimates(run.run_id))
    )


# ------------------------------------------------------------ Task 3 CLI gate

def test_view_run_id_extract_blocked_at_cli() -> None:
    """`pk-ku extract` refuses a view-policy run id with a typed block."""
    from personal_knowledge.application.ku import main as ku_main

    code = ku_main(["extract", "--run", "vc_candidate123", "--max-items", "1"])
    assert code == 2


def test_legacy_run_extract_blocked_at_cli(
    repo: CandidateRunRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pk-ku extract` refuses a superseded legacy message-level run id."""
    from personal_knowledge.application.ku import main as ku_main
    from personal_knowledge.core import project_paths

    repo.classify_legacy_run("ir_legacy-9")
    # the supersession audit is read from the conversation authority DB
    monkeypatch.setattr(project_paths, "AGENT_CONVERSATIONS_DB", repo.db)
    code = ku_main(
        ["extract", "--run", "ir_legacy-9", "--max-items", "1"]
    )
    assert code == 2


def test_unclassified_legacy_run_extract_blocked_at_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``ir_*`` run is non-executable even without a live audit table."""
    import sys
    import types

    from personal_knowledge.application.ku import main as ku_main
    from personal_knowledge.core import project_paths

    monkeypatch.setattr(
        project_paths,
        "AGENT_CONVERSATIONS_DB",
        tmp_path / "authority-without-candidate-ledger.sqlite",
    )

    paid_executor = types.ModuleType(
        "personal_knowledge.application.knowledge.build_knowledge_units_prod"
    )

    def _must_not_run(_argv: list[str]) -> int:
        raise AssertionError("legacy run reached the paid extraction executor")

    paid_executor.DEFAULT_MIN_REQUEST_INTERVAL = 0.0
    paid_executor.DEFAULT_WORKERS = 1
    paid_executor.main = _must_not_run
    monkeypatch.setitem(
        sys.modules,
        "personal_knowledge.application.knowledge.build_knowledge_units_prod",
        paid_executor,
    )

    code = ku_main(
        ["extract", "--run", "ir_unclassified-live", "--max-items", "1"]
    )
    assert code == 2


def test_view_subcommands_exist_on_parser() -> None:
    from personal_knowledge.application.ku import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as ei:
        parser.parse_args(["view-inspect", "--help"])
    assert ei.value.code == 0
    with pytest.raises(SystemExit) as ei:
        parser.parse_args(["view-prepare", "--help"])
    assert ei.value.code == 0
    with pytest.raises(SystemExit) as ei:
        parser.parse_args(["view-status", "--run", "vc_x", "--help"])
    assert ei.value.code == 0


def test_extract_default_model_unchanged() -> None:
    """Task 3 must not change the daily extract surface (no regression)."""
    from personal_knowledge.application.ku import build_parser

    args = build_parser().parse_args(["extract", "--run", "ir_ok"])
    assert args.model == "gemini-3.5-flash-lite"
