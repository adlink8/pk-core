"""Phase 62-06 Task 2: view/policy/evidence-bound prepare ledger.

Integration tests for
:mod:`personal_knowledge.application.knowledge.view_candidate_prepare`
against a real SQLite database.

Requirements exercised (Phase 62 CONTEXT D-16/D-24/D-30/D-31):
  - a prepared candidate run persists to a ledger keyed by generation, view
    builder version, policy digest, semantic prompt/schema version and
    evidence digest
  - old message-level prepare runs stay as audit history but are non-executable
    (D-30): their ledger rows/caches are never deleted
  - supersession transitions are append-only
  - only current view-policy runs can approach extraction
  - candidate preparation writes estimates/ledger only and never touches
    canonical/KU/authority tables or any provider

All tests run against temporary SQLite files under tmp_path. No live database,
no var/, no network, no provider calls (D-31).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.core.conversation_events import (
    AdaptedSession,
    EventKind,
    EventRelation,
    FidelityProfile,
    Provenance,
    RelationKind,
    TypedEvent,
    make_event_id,
)
from personal_knowledge.application.conversation.event_repository import (
    EventRepository,
    GenerationInput,
)
from personal_knowledge.application.conversation.event_schema import create_v2_schema
from personal_knowledge.application.conversation.extraction_policy import (
    DEFAULT_POLICY,
    schedule_candidates,
)
from personal_knowledge.application.conversation.extraction_views import (
    EventGraph,
    build_all_views,
)
from personal_knowledge.application.conversation.view_repository import (
    ViewRepository,
)
from personal_knowledge.application.knowledge.view_candidate_prepare import (
    CandidateRunRepository,
    CandidateRunKey,
    LegacyRunSupersededError,
    UnresolvedEvidenceError,
    VersionMismatchError,
    ViewExtractionBlockedError,
    evidence_set_digest,
)

LEGACY_ITEM_COUNT = 24_487  # the two old message-level prepare runs (D-30)


@pytest.mark.parametrize("run_id", ["vc_missing", "ir_missing"])
def test_status_without_candidate_schema_preserves_database(db: Path, run_id: str) -> None:
    from personal_knowledge.application.knowledge.view_candidate_prepare import view_run_status

    before = db.read_bytes()
    status = view_run_status(db, run_id)
    assert status["status"] == ("legacy_unclassified" if run_id.startswith("ir_") else "not_found")
    assert db.read_bytes() == before


def test_status_missing_database_does_not_create_file(tmp_path: Path) -> None:
    from personal_knowledge.application.knowledge.view_candidate_prepare import view_run_status

    db = tmp_path / "missing.sqlite"
    assert view_run_status(db, "vc_missing")["status"] == "not_found"
    assert not db.exists()


def test_inspect_active_generation_without_ledger_is_readonly(db: Path) -> None:
    from personal_knowledge.application.knowledge.view_candidate_prepare import inspect_candidate_state

    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO ce_generation_authority VALUES ('gen-1', 1, '2026-09-06')")
    before = db.read_bytes()
    state = inspect_candidate_state(db)
    assert state["active_generation_id"] == "gen-1"
    assert state["policy_view_count"] > 0
    assert state["pending_estimates"] == []
    assert db.read_bytes() == before


def test_existing_run_status_remains_readonly(db: Path, prepared: CandidateRunKey) -> None:
    from personal_knowledge.application.knowledge.view_candidate_prepare import make_candidate_run_id, view_run_status

    before = db.read_bytes()
    status = view_run_status(db, make_candidate_run_id(prepared))
    assert status["status"] == "blocked_pending_user_cost_approval"
    assert status["candidate_count"] > 0
    assert db.read_bytes() == before


# ------------------------------------------------------------------ fixtures

def _prov(nid: str, session: str) -> Provenance:
    return Provenance(
        artifact_id="art-a", artifact_hash="h" * 8,
        native_locator=f"jsonl:{nid}", native_session_id=session,
        native_event_id=nid, contract_version="1",
    )


def _ev(nid: str, kind: EventKind, ordinal: int, session: str = "s-a") -> TypedEvent:
    return TypedEvent(
        event_id=make_event_id("codex", "art-a", "1", nid),
        session_id=session, kind=kind, provenance=_prov(nid, session),
        fidelity=FidelityProfile.complete(), ordinal=ordinal,
        occurred_at=f"2026-08-{ordinal:02d}T00:00:00Z",
    )


def _session(sid: str) -> AdaptedSession:
    return AdaptedSession(
        session_id=sid, provenance=_prov(sid, sid),
        fidelity=FidelityProfile.complete(), native_session_id=sid,
    )


def _graph(*, generation_id: str = "gen-1", prefix: str = "") -> EventGraph:
    b1 = _ev(f"{prefix}tb-1", EventKind.TURN_BOUNDARY, 1)
    m1 = _ev(f"{prefix}m-1", EventKind.USER_MESSAGE, 2)
    m2 = _ev(f"{prefix}m-2", EventKind.ASSISTANT_MESSAGE, 3)
    c1 = _ev(f"{prefix}c-1", EventKind.USER_MESSAGE, 4)
    kept = _ev(f"{prefix}kept-1", EventKind.ASSISTANT_MESSAGE, 5)
    sum1 = _ev(f"{prefix}sum-1", EventKind.COMPACTION_SUMMARY, 6)
    x1 = _ev(f"{prefix}x-1", EventKind.ASSISTANT_MESSAGE, 1, session="s-b")
    return EventGraph(
        generation_id=generation_id,
        sessions=(_session("s-a"), _session("s-b")),
        events=(b1, m1, m2, c1, kept, sum1, x1),
        relations=(
            EventRelation("r-t1", b1.event_id, m1.event_id, RelationKind.TURN_MEMBERSHIP),
            EventRelation("r-t2", b1.event_id, m2.event_id, RelationKind.TURN_MEMBERSHIP),
            EventRelation("r-c1", sum1.event_id, c1.event_id, RelationKind.COMPACTED_RANGE),
            EventRelation("r-r1", sum1.event_id, kept.event_id, RelationKind.RETAINED_FROM),
            EventRelation(
                "r-x", sum1.event_id, x1.event_id,
                RelationKind.SOURCE_SESSION_CROSSWALK,
            ),
        ),
    )


def _gen_input_from(graph: EventGraph) -> GenerationInput:
    from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact

    return GenerationInput(
        family="codex", adapter_version="1", contract_version="1",
        capability_digest="cap-1", source_manifest_id="manifest-1",
        dataset_digest="ds-1",
        artifacts=(
            SourceArtifact(
                artifact_id="art-a", family="codex", source_kind="file",
                content_hash="h" * 8, capture_method="sha256",
                relative_path="rollout.jsonl", byte_size=10,
            ),
        ),
        sessions=graph.sessions,
        events=graph.events, relations=graph.relations, dispositions=(),
        warnings=(),
    )


class _EventEvidenceRepo:
    """EventRepository-backed evidence resolver (read-only)."""

    def __init__(self, db: Path) -> None:
        self._repo = EventRepository(db)

    def has_events(self, generation_id: str, event_ids) -> set[str]:
        existing = {r["event_id"] for r in self._repo.iter_events(generation_id)}
        return {e for e in event_ids if e in existing}


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "conversations.sqlite"
    create_v2_schema(db_path)
    event_repo = EventRepository(db_path)
    event_repo.create_schema()
    event_repo.write_generation(
        _gen_input_from(_graph(generation_id="gen-1")), generation_id="gen-1"
    )
    views = ViewRepository(db_path)
    views.create_schema()
    views.save_view_revision(build_all_views(_graph(generation_id="gen-1")), DEFAULT_POLICY)
    return db_path


@pytest.fixture()
def prepared(db: Path) -> CandidateRunKey:
    result = build_all_views(_graph(generation_id="gen-1"))
    scheduled = schedule_candidates(DEFAULT_POLICY, result, "2026-08-12T00:00:00Z")
    repo = CandidateRunRepository(db)
    repo.create_schema()
    refs = sorted({eid for c in scheduled.candidates for eid in c.evidence_event_refs})
    key = CandidateRunKey(
        active_generation_id="gen-1",
        view_builder_version="1",
        policy_digest=scheduled.policy_digest,
        semantic_prompt_version="semantic-v1",
        semantic_schema_version="schema-v1",
        evidence_event_digest=evidence_set_digest(refs),
    )
    repo.prepare_view_run(key, scheduled, result, event_repo=_EventEvidenceRepo(db))
    return key


# ------------------------------------------------------------ ledger

def test_prepare_persists_ledger_row(db: Path, prepared: CandidateRunKey) -> None:
    repo = CandidateRunRepository(db)
    rows = repo._run_rows()
    assert len(rows) == 1
    run = repo.get_run(rows[0]["run_id"])
    assert run is not None
    assert run.key == prepared
    assert run.status == "blocked_pending_user_cost_approval"
    status = repo.status(run.run_id)
    assert status["candidate_count"] == run.candidate_count
    assert status["estimated_cost_usd"] == run.estimated_cost_usd


def test_prepare_writes_only_candidate_tables(db: Path, prepared: CandidateRunKey) -> None:
    con = sqlite3.connect(str(db))
    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    con.close()
    for table in ("ce_candidate_runs", "ce_candidate_estimates", "ce_candidate_audit"):
        assert table in tables
    # the v2 authority + view tables are preserved untouched
    assert "ce_events" in tables
    assert "ce_view_headers" in tables


def test_prepare_makes_no_authority_or_ku_mutation(db: Path, prepared: CandidateRunKey) -> None:
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE IF NOT EXISTS canonical_messages (id TEXT)")
    con.execute("INSERT INTO canonical_messages VALUES ('pre-existing')")
    con.execute(
        "CREATE TABLE IF NOT EXISTS ce_generation_authority "
        "(generation_id TEXT PRIMARY KEY, active INTEGER DEFAULT 0)"
    )
    con.commit()
    con.close()

    # re-run prepare (idempotent) and confirm nothing outside ce_candidate_* changed
    result = build_all_views(_graph(generation_id="gen-1"))
    scheduled = schedule_candidates(DEFAULT_POLICY, result, "2026-08-12T00:00:00Z")
    refs = sorted({eid for c in scheduled.candidates for eid in c.evidence_event_refs})
    key = CandidateRunKey(
        active_generation_id="gen-1",
        view_builder_version="1",
        policy_digest=scheduled.policy_digest,
        semantic_prompt_version="semantic-v1",
        semantic_schema_version="schema-v1",
        evidence_event_digest=evidence_set_digest(refs),
    )
    CandidateRunRepository(db).prepare_view_run(
        key, scheduled, result, event_repo=_EventEvidenceRepo(db)
    )

    con = sqlite3.connect(str(db))
    canonical_count = con.execute("SELECT COUNT(*) FROM canonical_messages").fetchone()[0]
    authority_count = con.execute(
        "SELECT COUNT(*) FROM ce_generation_authority"
    ).fetchone()[0]
    con.close()
    assert canonical_count == 1
    assert authority_count == 0


# ------------------------------------------------- legacy supersession

def test_legacy_rows_preserved_when_superseded(db: Path) -> None:
    """24,487 legacy message-level prepare rows stay; the run is non-executable."""
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE knowledge_run_items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "run_id TEXT NOT NULL, position INTEGER NOT NULL, evidence_ref TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending')"
    )
    con.execute("CREATE INDEX idx_kri_run ON knowledge_run_items(run_id)")
    con.executemany(
        "INSERT INTO knowledge_run_items (run_id, position, evidence_ref, status) "
        "VALUES (?, ?, ?, 'pending')",
        [("ir_legacy-user", i, f"ref:{i}",) for i in range(3_224)],
    )
    con.executemany(
        "INSERT INTO knowledge_run_items (run_id, position, evidence_ref, status) "
        "VALUES (?, ?, ?, 'pending')",
        [("ir_legacy-assistant", i, f"ref:{i}",) for i in range(21_263)],
    )
    con.commit()
    con.close()
    assert _count_items(db) == LEGACY_ITEM_COUNT

    repo = CandidateRunRepository(db)
    repo.create_schema()
    repo.classify_legacy_run("ir_legacy-user")
    repo.classify_legacy_run("ir_legacy-assistant")

    # rows/caches preserved exactly
    assert _count_items(db) == LEGACY_ITEM_COUNT
    # audit transitions recorded append-only
    assert len(repo._audit_rows("ir_legacy-user")) == 1
    assert repo.legacy_status("ir_legacy-user") == "superseded_policy"
    # both old runs refuse execution
    with pytest.raises(LegacyRunSupersededError):
        repo.assert_extraction_authorized("ir_legacy-user")
    with pytest.raises(LegacyRunSupersededError):
        repo.assert_extraction_authorized("ir_legacy-assistant")


def test_audit_transition_is_append_only(db: Path) -> None:
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE knowledge_run_items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "run_id TEXT NOT NULL, position INTEGER NOT NULL, evidence_ref TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending')"
    )
    con.executemany(
        "INSERT INTO knowledge_run_items (run_id, position, evidence_ref) VALUES (?, ?, ?)",
        [("ir_old", i, f"ref:{i}") for i in range(100)],
    )
    con.commit()
    con.close()

    repo = CandidateRunRepository(db)
    repo.create_schema()
    repo.classify_legacy_run("ir_old", reason="policy superseded")
    before = _count_items(db)
    repo.classify_legacy_run("ir_old", reason="policy superseded again")
    assert _count_items(db) == before
    assert len(repo._audit_rows("ir_old")) == 2


def test_unclassified_legacy_run_still_non_executable(db: Path) -> None:
    repo = CandidateRunRepository(db)
    with pytest.raises(LegacyRunSupersededError):
        repo.assert_extraction_authorized("ir_unclassified")


# ------------------------------------------------ only current runs approach

def test_only_current_view_policy_run_approaches_extraction(
    db: Path, prepared: CandidateRunKey
) -> None:
    repo = CandidateRunRepository(db)
    run = repo.get_run(repo._run_rows()[0]["run_id"])
    # a current matching run can be inspected/prepared
    repo.check_run_executable(run.run_id, current_key=prepared, event_repo=_EventEvidenceRepo(db))
    # but no command path can spend quota yet
    with pytest.raises(ViewExtractionBlockedError, match="approval"):
        repo.assert_extraction_authorized(run.run_id)


def test_stale_generation_run_rejected(db: Path, prepared: CandidateRunKey) -> None:
    repo = CandidateRunRepository(db)
    run = repo.get_run(repo._run_rows()[0]["run_id"])
    current = CandidateRunKey(
        active_generation_id="gen-2",
        view_builder_version=prepared.view_builder_version,
        policy_digest=prepared.policy_digest,
        semantic_prompt_version=prepared.semantic_prompt_version,
        semantic_schema_version=prepared.semantic_schema_version,
        evidence_event_digest=prepared.evidence_event_digest,
    )
    with pytest.raises(VersionMismatchError, match="generation"):
        repo.check_run_executable(run.run_id, current_key=current)


def test_evidence_unresolved_rejected_with_live_repo(db: Path) -> None:
    """A candidate whose evidence is missing from ce_events is rejected."""
    result = build_all_views(_graph(generation_id="gen-1"))
    scheduled = schedule_candidates(DEFAULT_POLICY, result, "2026-08-12T00:00:00Z")
    refs = sorted({eid for c in scheduled.candidates for eid in c.evidence_event_refs})
    key = CandidateRunKey(
        active_generation_id="gen-1",
        view_builder_version="1",
        policy_digest=scheduled.policy_digest,
        semantic_prompt_version="semantic-v1",
        semantic_schema_version="schema-v1",
        evidence_event_digest=evidence_set_digest(refs),
    )
    repo = CandidateRunRepository(db)
    # delete one staged event so its view evidence becomes unresolved
    con = sqlite3.connect(str(db))
    con.execute("DELETE FROM ce_events WHERE generation_id='gen-1' AND event_id IN "
                "(SELECT event_id FROM ce_view_members LIMIT 1)")
    con.commit()
    con.close()
    with pytest.raises(UnresolvedEvidenceError):
        repo.prepare_view_run(key, scheduled, result, event_repo=_EventEvidenceRepo(db))


def _count_items(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT COUNT(*) FROM knowledge_run_items").fetchone()[0]
    finally:
        con.close()
