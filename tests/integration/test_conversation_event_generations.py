"""Phase 62-04 Task 2: staging, validation, atomic activation and exact rollback.

RED/GREEN tests for :mod:`personal_knowledge.application.conversation.event_generations`
(the sole generation lifecycle owner):

  - prepare stages a complete generation
  - validate covers schema/FK/digests/provenance/fidelity/adapter coverage
  - activate commits the event authority + compatibility projection + version /
    watermark / fingerprint binding atomically
  - every injected failure (before commit, after authority commit in
    projection/pointer/version, checksum mismatch, stale manifest, unknown
    adapter, missing family coverage, consumer parity failure) restores the
    exact prior authority rows / compatibility tables / version / watermark /
    fingerprint
  - old generation rows and activation audit records are preserved, never
    deleted

All tests run against temporary SQLite files under tmp_path. No live database,
no var/, no network, no provider calls (D-31).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.core.conversation_repository import (
    ConversationRepository,
    SOURCE_CANONICAL,
)
from personal_knowledge.application.conversation.event_repository import (
    EventRepository,
    GenerationInput,
)
from personal_knowledge.application.conversation.event_generations import (
    ActivationHooks,
    GenerationActivationError,
    GenerationLifecycle,
)


def test_activation_persists_exact_changed_event_refs_for_restart(tmp_path, _activate, _generation):
    from personal_knowledge.application.conversation.generation_delta import GenerationDeltaRepository

    db = tmp_path / "delta.sqlite"
    life = GenerationLifecycle(db)
    first = _generation("d-1", "Use PowerShell")
    second = _generation("d-2", "Use Bash")
    life.prepare(first, generation_id="g-1")
    _activate(life, "g-1", digest="d-1")
    life.prepare(second, generation_id="g-2")
    _activate(life, "g-2", digest="d-2")
    before = db.read_bytes()
    delta = GenerationDeltaRepository(db).latest()
    assert delta["generation_id"] == "g-2"
    assert delta["prior_generation_id"] == "g-1"
    assert delta["source_manifest_id"] == "manifest-1"
    assert delta["dataset_digest"] == "d-2"
    assert delta["event_count"] == 1
    assert GenerationDeltaRepository(db).events(delta["delta_id"]) == [
        {"event_id": second.events[0].event_id, "change": "changed"}
    ]
    assert db.read_bytes() == before


def test_delta_failure_restores_authority_and_last_delta(tmp_path, _activate, _generation):
    from personal_knowledge.application.conversation.generation_delta import GenerationDeltaRepository

    db = tmp_path / "failure.sqlite"
    life = GenerationLifecycle(db)
    life.prepare(_generation("d-1", "first"), generation_id="g-1")
    _activate(life, "g-1", digest="d-1")
    previous = GenerationDeltaRepository(db).latest()
    life.prepare(_generation("d-2", "second"), generation_id="g-2")
    with sqlite3.connect(db) as con:
        con.execute("CREATE TRIGGER delta_abort BEFORE INSERT ON ce_generation_delta_events "
                    "BEGIN SELECT RAISE(ABORT, 'storage failed'); END")
    with pytest.raises(GenerationActivationError):
        _activate(life, "g-2", digest="d-2")
    assert life.authority_generation_id() == "g-1"
    assert GenerationDeltaRepository(db).latest() == previous


def test_delta_tracks_removed_events_and_rollback(tmp_path, _activate, _generation):
    from dataclasses import replace
    from personal_knowledge.application.conversation.generation_delta import GenerationDeltaRepository

    db = tmp_path / "removed.sqlite"
    life = GenerationLifecycle(db)
    first = _generation("d-1", "first")
    life.prepare(first, generation_id="g-1")
    _activate(life, "g-1", digest="d-1")
    life.prepare(replace(first, dataset_digest="d-2", events=first.events[:2]), generation_id="g-2")
    _activate(life, "g-2", digest="d-2")
    repo = GenerationDeltaRepository(db)
    assert repo.events(repo.latest()["delta_id"]) == [
        {"event_id": first.events[2].event_id, "change": "removed"}
    ]
    life.rollback_to("g-1")
    assert repo.events(repo.latest()["delta_id"]) == [
        {"event_id": first.events[2].event_id, "change": "changed"}
    ]
    history = repo.pending(after_delta=0, limit=2)
    assert [item["generation_id"] for item in history] == ["g-1", "g-2"]
    assert [item["generation_id"] for item in repo.pending(after_delta=history[-1]["delta_id"])] == ["g-1"]


@pytest.mark.parametrize("change", ["session", "relation"])
def test_context_changes_requeue_affected_events(tmp_path, change, _activate, _generation):
    from dataclasses import replace
    from personal_knowledge.core.conversation_events import EventRelation, RelationKind
    from personal_knowledge.application.conversation.generation_delta import GenerationDeltaRepository

    db = tmp_path / "context.sqlite"
    life = GenerationLifecycle(db)
    first = _generation("d-1", "first")
    life.prepare(first, generation_id="g-1")
    _activate(life, "g-1", digest="d-1")
    if change == "session":
        second = replace(first, dataset_digest="d-2", sessions=(replace(first.sessions[0], cwd="/project-b"),))
        expected = {event.event_id for event in first.events}
    else:
        second = replace(first, dataset_digest="d-2", relations=(EventRelation(
            "relation-new", first.events[2].event_id, first.events[0].event_id, RelationKind.COMPACTED_RANGE),))
        expected = {first.events[2].event_id, first.events[0].event_id}
    life.prepare(second, generation_id="g-2")
    _activate(life, "g-2", digest="d-2")
    repo = GenerationDeltaRepository(db)
    assert {row["event_id"] for row in repo.events(repo.latest()["delta_id"])} == expected


def test_delta_read_rejects_partial_authority_schema(tmp_path):
    from personal_knowledge.application.conversation.generation_delta import GenerationDeltaRepository

    db = tmp_path / "partial.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE ce_generation_deltas(delta_id INTEGER)")
    before = db.read_bytes()
    with pytest.raises(ValueError, match="schema is incomplete"):
        GenerationDeltaRepository(db).latest()
    assert db.read_bytes() == before


def test_delta_consumer_prepares_candidates_and_resumes_without_duplicate(tmp_path, capsys, _activate, _generation):
    import json
    from personal_knowledge.application.ku import main
    from personal_knowledge.application.knowledge.delta_candidate_consumer import consume_delta_candidates
    from personal_knowledge.application.knowledge.view_candidate_prepare import CandidateRunRepository

    db = tmp_path / "consumer.sqlite"
    life = GenerationLifecycle(db)
    life.prepare(_generation("d-1", "I prefer PowerShell for future commands."), generation_id="g-1")
    _activate(life, "g-1", digest="d-1")
    assert main(["view-consume", "--conversation-db", str(db)]) == 0
    batch = json.loads(capsys.readouterr().out)
    assert batch["status"] == "prepared"
    assert batch["run_id"].startswith("vc_")
    assert CandidateRunRepository(db).list_candidates(batch["run_id"])
    before = db.read_bytes()
    assert consume_delta_candidates(db)["status"] == "idle"
    assert db.read_bytes() == before


def test_consumer_failure_after_prepare_retries_without_losing_progress(tmp_path, _activate, _generation):
    from personal_knowledge.application.knowledge.delta_candidate_consumer import consume_delta_candidates

    db = tmp_path / "consumer-retry.sqlite"
    life = GenerationLifecycle(db)
    life.prepare(_generation("d-1", "I prefer PowerShell."), generation_id="g-1")
    _activate(life, "g-1", digest="d-1")
    consume_delta_candidates(db)
    life.prepare(_generation("d-2", "Use Bash in this project."), generation_id="g-2")
    _activate(life, "g-2", digest="d-2")
    with sqlite3.connect(db) as con:
        con.execute("CREATE TRIGGER batch_abort BEFORE INSERT ON ce_delta_prepare_batches "
                    "BEGIN SELECT RAISE(ABORT, 'checkpoint failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        consume_delta_candidates(db)
    with sqlite3.connect(db) as con:
        con.execute("DROP TRIGGER batch_abort")
    resumed = consume_delta_candidates(db)
    assert resumed["status"] == "prepared"
    assert resumed["generation_id"] == "g-2"
    assert consume_delta_candidates(db)["status"] == "idle"


def test_consumer_context_limit_does_not_advance_or_write(tmp_path, _activate, _generation):
    from personal_knowledge.application.knowledge.delta_candidate_consumer import consume_delta_candidates

    db = tmp_path / "consumer-limit.sqlite"
    life = GenerationLifecycle(db)
    life.prepare(_generation("d-1", "I prefer PowerShell."), generation_id="g-1")
    _activate(life, "g-1", digest="d-1")
    before = db.read_bytes()
    with pytest.raises(ValueError, match="max_events"):
        consume_delta_candidates(db, max_context_events=1)
    assert db.read_bytes() == before
    assert consume_delta_candidates(db)["status"] == "prepared"


def test_consumer_legacy_active_generation_without_delta_is_not_caught_up(tmp_path, _generation):
    from personal_knowledge.application.knowledge.delta_candidate_consumer import consume_delta_candidates

    db = tmp_path / "legacy-active.sqlite"
    life = GenerationLifecycle(db)
    life.prepare(_generation("d-1", "I prefer PowerShell."), generation_id="g-1")
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO ce_generation_authority VALUES ('g-1',1,'2026-09-06')")
    before = db.read_bytes()
    with pytest.raises(ValueError, match="generation delta history unavailable"):
        consume_delta_candidates(db)
    assert db.read_bytes() == before


def test_consumer_reconciles_offline_history_against_current_generation(tmp_path, _activate, _generation):
    from personal_knowledge.application.knowledge.delta_candidate_consumer import consume_delta_candidates
    from personal_knowledge.application.knowledge.view_candidate_prepare import CandidateRunRepository
    from personal_knowledge.application.conversation.bounded_event_context import load_affected_context

    db = tmp_path / "offline.sqlite"
    life = GenerationLifecycle(db)
    life.prepare(_generation("d-1", "Old shell preference"), generation_id="g-1")
    _activate(life, "g-1", digest="d-1")
    current = _generation("d-2", "New shell preference")
    life.prepare(current, generation_id="g-2")
    _activate(life, "g-2", digest="d-2")
    batch = consume_delta_candidates(db)
    assert batch["reconciled"] is True
    assert batch["source_generation_id"] == "g-1"
    assert batch["generation_id"] == "g-2"
    assert CandidateRunRepository(db).get_run(batch["run_id"]).key.active_generation_id == "g-2"
    context = load_affected_context(db, "g-2", [current.events[0].event_id])
    assert context.events[0].summary == "New shell preference"
    assert consume_delta_candidates(db)["source_generation_id"] == "g-2"
    assert consume_delta_candidates(db)["status"] == "idle"



def _snapshot(db: Path) -> dict:
    con = sqlite3.connect(str(db))
    try:
        return {
            "authority": sorted(
                (tuple(r) for r in con.execute(
                    "SELECT generation_id, active FROM ce_generation_authority"
                )),
            ),
            "sessions": con.execute(
                "SELECT canonical_session_id, primary_source, agent FROM "
                "canonical_sessions ORDER BY canonical_session_id"
            ).fetchall(),
            "messages": con.execute(
                "SELECT canonical_message_id, role, content FROM "
                "canonical_messages ORDER BY canonical_message_id"
            ).fetchall(),
            "tools": con.execute(
                "SELECT canonical_tool_id, source_kind FROM "
                "canonical_tool_events ORDER BY canonical_tool_id"
            ).fetchall(),
            "bindings": sorted(
                (tuple(r) for r in con.execute(
                    "SELECT kind, generation_id, value FROM ce_activation_bindings"
                )),
            ),
        }
    finally:
        con.close()


@pytest.fixture()
def live(tmp_path: Path, _generation, _activate) -> tuple[Path, GenerationLifecycle, GenerationInput, GenerationInput]:
    """Baseline: gen-1 active; gen-2 staged and ready to attempt activation."""
    db = tmp_path / "conversations.sqlite"
    life = GenerationLifecycle(db)
    gen_a = _generation("ds-a", "hello from generation one")
    life.prepare(gen_a, "gen-1")
    _activate(life, "gen-1", digest=gen_a.dataset_digest)
    assert life.authority_generation_id() == "gen-1"
    gen_b = _generation("ds-b", "hello from generation two")
    life.prepare(gen_b, "gen-2")
    return db, life, gen_a, gen_b


def _failing_hook(message: str):
    def _hook(*args, **kwargs):
        raise RuntimeError(message)
    return _hook


# ---------------------------------------------------------------- lifecycle

def test_prepare_stages_generation(tmp_path: Path, _generation) -> None:
    db = tmp_path / "conversations.sqlite"
    life = GenerationLifecycle(db)
    gen = _generation("ds-a", "staged text")
    life.prepare(gen, "gen-x")
    repo = EventRepository(db)
    result = repo.validate_generation("gen-x")
    assert result["ok"] is True
    assert result["events"] == 3
    assert life.authority_generation_id() is None  # staged != active


def test_validate_reports_checksum_mismatch(live) -> None:
    db, life, gen_a, gen_b = live
    report = life.validate(
        "gen-2", source_manifest_id="manifest-1",
        expected_dataset_digest="wrong-digest",
        expected_adapter_families=("codex",),
    )
    assert report["ok"] is False
    assert "checksum" in report["failure"]


def test_validate_reports_stale_manifest(live) -> None:
    db, life, gen_a, gen_b = live
    report = life.validate(
        "gen-2", source_manifest_id="stale-manifest",
        expected_dataset_digest=gen_b.dataset_digest,
        expected_adapter_families=("codex",),
    )
    assert report["ok"] is False
    assert "manifest" in report["failure"]


def test_validate_reports_unknown_adapter(live) -> None:
    db, life, gen_a, gen_b = live
    report = life.validate(
        "gen-2", source_manifest_id="manifest-1",
        expected_dataset_digest=gen_b.dataset_digest,
        expected_adapter_families=("codex", "ghost-family"),
    )
    assert report["ok"] is False
    assert "adapter" in report["failure"]


def test_validate_reports_missing_family_coverage(live) -> None:
    db, life, gen_a, gen_b = live
    report = life.validate(
        "gen-2", source_manifest_id="manifest-1",
        expected_dataset_digest=gen_b.dataset_digest,
        expected_adapter_families=("codex", "claude"),
    )
    assert report["ok"] is False
    assert "coverage" in report["failure"]


def test_validate_passes_healthy_generation(live) -> None:
    db, life, gen_a, gen_b = live
    report = life.validate(
        "gen-2", source_manifest_id="manifest-1",
        expected_dataset_digest=gen_b.dataset_digest,
        expected_adapter_families=("codex",),
    )
    assert report["ok"] is True


# ----------------------------------------------------------- successful flow

def test_activate_success_binds_authority_projection_version(live, _activate) -> None:
    db, life, gen_a, gen_b = live
    _activate(life, "gen-2", digest=gen_b.dataset_digest)
    assert life.authority_generation_id() == "gen-2"
    # projection rows now reflect generation two (one user message + one tool?)
    con = sqlite3.connect(str(db))
    contents = [r[0] for r in con.execute(
        "SELECT content FROM canonical_messages ORDER BY ordinal"
    )]
    con.close()
    assert "hello from generation two" in contents
    assert "hello from generation one" not in contents  # old projection replaced
    # version / watermark / fingerprint bind to generation two
    bindings = dict(
        (r[0], r[1]) for r in sqlite3.connect(str(db)).execute(
            "SELECT kind, generation_id FROM ce_activation_bindings"
        )
    )
    assert bindings["projection_version"] == "gen-2"
    assert bindings["projection_watermark"] == "gen-2"
    assert bindings["projection_fingerprint"] == "gen-2"
    # old generation rows are preserved, never deleted
    repo = EventRepository(db)
    assert repo.validate_generation("gen-1")["ok"] is True
    assert repo.validate_generation("gen-2")["ok"] is True


# ------------------------------------------------- fault injection: pre-commit

def test_checksum_mismatch_restores_exact_state(live, _activate) -> None:
    db, life, gen_a, gen_b = live
    before = _snapshot(db)
    with pytest.raises(GenerationActivationError):
        _activate(life, "gen-2", digest="wrong-digest")
    assert _snapshot(db) == before
    assert life.authority_generation_id() == "gen-1"


def test_stale_manifest_restores_exact_state(live) -> None:
    db, life, gen_a, gen_b = live
    before = _snapshot(db)
    with pytest.raises(GenerationActivationError):
        life.activate(
            "gen-2", source_manifest_id="stale-manifest",
            expected_dataset_digest=gen_b.dataset_digest,
            expected_adapter_families=("codex",),
        )
    assert _snapshot(db) == before


def test_unknown_adapter_restores_exact_state(live) -> None:
    db, life, gen_a, gen_b = live
    before = _snapshot(db)
    with pytest.raises(GenerationActivationError):
        life.activate(
            "gen-2", source_manifest_id="manifest-1",
            expected_dataset_digest=gen_b.dataset_digest,
            expected_adapter_families=("codex", "ghost-family"),
        )
    assert _snapshot(db) == before


def test_consumer_parity_failure_blocks_activation(live, _activate) -> None:
    db, life, gen_a, gen_b = live
    before = _snapshot(db)
    hooks = ActivationHooks(
        consumer_parity=lambda: {"ok": False, "reason": "user_turn_parity_mismatch"},
    )
    with pytest.raises(GenerationActivationError):
        _activate(life, "gen-2", digest=gen_b.dataset_digest, hooks=hooks)
    assert _snapshot(db) == before
    assert life.authority_generation_id() == "gen-1"


# ------------------------------------------- fault injection: post-authority

def test_projection_write_failure_restores_exact_state(live, _activate) -> None:
    db, life, gen_a, gen_b = live
    before = _snapshot(db)
    hooks = ActivationHooks(projection_writer=_failing_hook("projection write boom"))
    with pytest.raises(GenerationActivationError):
        _activate(life, "gen-2", digest=gen_b.dataset_digest, hooks=hooks)
    assert _snapshot(db) == before
    assert life.authority_generation_id() == "gen-1"


def test_authority_pointer_failure_restores_exact_state(live, _activate) -> None:
    db, life, gen_a, gen_b = live
    before = _snapshot(db)
    hooks = ActivationHooks(authority_writer=_failing_hook("pointer write boom"))
    with pytest.raises(GenerationActivationError):
        _activate(life, "gen-2", digest=gen_b.dataset_digest, hooks=hooks)
    assert _snapshot(db) == before


def test_version_binding_failure_restores_exact_state(live, _activate) -> None:
    db, life, gen_a, gen_b = live
    before = _snapshot(db)
    hooks = ActivationHooks(version_binder=_failing_hook("version bind boom"))
    with pytest.raises(GenerationActivationError):
        _activate(life, "gen-2", digest=gen_b.dataset_digest, hooks=hooks)
    assert _snapshot(db) == before
    assert life.authority_generation_id() == "gen-1"
    # exact fingerprint restoration
    con = sqlite3.connect(str(db))
    fp = con.execute(
        "SELECT generation_id, value FROM ce_activation_bindings "
        "WHERE kind='projection_fingerprint'"
    ).fetchone()
    con.close()
    assert fp[0] == "gen-1"


# ------------------------------------------------------ preservation + rollback

def test_old_generation_rows_and_audit_preserved_after_failure(live, _activate) -> None:
    db, life, gen_a, gen_b = live
    with pytest.raises(GenerationActivationError):
        _activate(life, "gen-2", digest="wrong-digest")
    repo = EventRepository(db)
    # failed generation stays staged/preserved
    assert repo.validate_generation("gen-2")["events"] == 3
    assert repo.validate_generation("gen-1")["events"] == 3
    # activation audit preserves both attempts
    con = sqlite3.connect(str(db))
    log = con.execute(
        "SELECT generation_id, outcome FROM ce_activation_log "
        "ORDER BY attempted_at"
    ).fetchall()
    con.close()
    outcomes = [o for _g, o in log]
    assert "success" in outcomes
    assert "failure" in outcomes


def test_rollback_to_previous_generation(live, _activate) -> None:
    db, life, gen_a, gen_b = live
    _activate(life, "gen-2", digest=gen_b.dataset_digest)
    assert life.authority_generation_id() == "gen-2"
    life.rollback_to("gen-1")
    assert life.authority_generation_id() == "gen-1"
    con = sqlite3.connect(str(db))
    contents = [r[0] for r in con.execute(
        "SELECT content FROM canonical_messages ORDER BY ordinal"
    )]
    bindings = dict((r[0], r[1]) for r in con.execute(
        "SELECT kind, generation_id FROM ce_activation_bindings"
    ))
    con.close()
    assert "hello from generation one" in contents
    assert bindings["projection_version"] == "gen-1"
    # generation two rows are still preserved, not deleted
    assert EventRepository(db).validate_generation("gen-2")["ok"] is True


def test_repository_has_no_activation_surface(live) -> None:
    db, life, gen_a, gen_b = live
    repo = EventRepository(db)
    public = [n for n in dir(repo) if not n.startswith("_")]
    assert not any("activate" in n.lower() for n in public)


def test_activation_preserves_legacy_rows(tmp_path: Path, _activate, _generation) -> None:
    """Activation must never discard pre-existing legacy-era canonical rows
    (D-18/D-19): only v2 projection rows are replaced. Regression for the
    62-08 incident where activation cleared the live legacy tables."""
    db = tmp_path / "conversations.sqlite"
    life = GenerationLifecycle(db)
    # Preload one legacy-era session + message with non-v2 ids.
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS canonical_sessions ("
            " canonical_session_id TEXT PRIMARY KEY, primary_source TEXT NOT NULL,"
            " agent TEXT, started_at TEXT, ended_at TEXT, message_count INTEGER,"
            " user_message_count INTEGER, file_hash TEXT, parent_canonical_id TEXT,"
            " relationship_type TEXT, cwd TEXT, git_branch TEXT, model TEXT,"
            " evidence_eligible INTEGER NOT NULL DEFAULT 1,"
            " evidence_scope TEXT NOT NULL DEFAULT 'user',"
            " merged INTEGER NOT NULL DEFAULT 0,"
            " lifecycle TEXT NOT NULL DEFAULT 'active',"
            " superseded_by_canonical_id TEXT)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS canonical_messages ("
            " canonical_message_id TEXT PRIMARY KEY, canonical_session_id TEXT,"
            " source TEXT NOT NULL, source_message_ref TEXT, ordinal INTEGER,"
            " role TEXT, content TEXT, content_length INTEGER, timestamp TEXT,"
            " model TEXT, is_system INTEGER, is_sidechain INTEGER,"
            " content_hash TEXT, evidence_scope TEXT)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS canonical_tool_events ("
            " canonical_tool_id TEXT PRIMARY KEY, canonical_session_id TEXT,"
            " source TEXT NOT NULL, source_kind TEXT, tool_name TEXT, category TEXT,"
            " status TEXT, input TEXT, output TEXT, tool_use_ordinal INTEGER,"
            " evidence_scope TEXT)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS session_source_links ("
            " link_id TEXT PRIMARY KEY, canonical_session_id TEXT NOT NULL,"
            " source TEXT NOT NULL, source_session_id TEXT NOT NULL,"
            " source_raw_file TEXT, match_method TEXT NOT NULL,"
            " match_confidence TEXT)"
        )
        con.execute(
            "INSERT INTO canonical_sessions (canonical_session_id, primary_source,"
            " message_count, user_message_count) VALUES ('legacy-session-1',"
            " 'legacy', 1, 1)"
        )
        con.execute(
            "INSERT INTO canonical_messages (canonical_message_id, canonical_session_id,"
            " source, role, content) VALUES ('legacy-msg-1', 'legacy-session-1',"
            " 'legacy', 'user', 'pre-existing legacy message')"
        )
        con.execute(
            "INSERT INTO session_source_links VALUES ("
            " 'legacy-link-1', 'legacy-session-1', 'legacy', 'source-legacy-1',"
            " 'legacy.jsonl', 'single_source', 'strong')"
        )
        con.commit()
    finally:
        con.close()

    gen_a = _generation("ds-a", "hello from generation one")
    life.prepare(gen_a, "gen-1")
    _activate(life, "gen-1", digest=gen_a.dataset_digest)

    con = sqlite3.connect(str(db))
    try:
        sessions = con.execute(
            "SELECT canonical_session_id, primary_source FROM canonical_sessions"
        ).fetchall()
        messages = con.execute(
            "SELECT canonical_message_id, content FROM canonical_messages"
        ).fetchall()
    finally:
        con.close()

    legacy_sessions = [r for r in sessions if not r[0].startswith("v2|")]
    legacy_msgs = [r for r in messages if not r[0].startswith("v2|")]
    v2_msgs = [r for r in messages if r[0].startswith("v2|")]
    assert legacy_sessions == [("legacy-session-1", "legacy")]
    assert legacy_msgs == [("legacy-msg-1", "pre-existing legacy message")]
    assert v2_msgs, "v2 projection rows must be written alongside legacy rows"

    consumer = ConversationRepository(
        source=SOURCE_CANONICAL, canonical_db=db, legacy_db=db,
    )
    visible_sessions = list(consumer.iter_sessions())
    assert len(visible_sessions) == 1
    assert visible_sessions[0]["canonical_session_id"].startswith("v2|")
    assert consumer.session_count() == 1
    assert consumer.user_turn_count() == 1
    assert consumer.session_source_refs("legacy-session-1") == []
    assert all(
        turn.content != "pre-existing legacy message"
        for session in visible_sessions
        for turn in consumer.iter_turns(session["canonical_session_id"])
    )
