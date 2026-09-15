from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact
from personal_knowledge.application.conversation.event_repository import EventRepository, GenerationInput
from personal_knowledge.application.conversation.event_schema import create_v2_schema
from personal_knowledge.application.conversation.extracted_candidate_store import ExtractedCandidateStore
from personal_knowledge.application.knowledge.view_candidate_prepare import CandidateRunRepository
from personal_knowledge.core.conversation_events import EventKind, FidelityProfile, Provenance, TypedEvent


def _setup(db: Path) -> tuple[str, str]:
    create_v2_schema(db)
    event = TypedEvent(
        event_id="event-1", session_id="session-1", kind=EventKind.USER_MESSAGE,
        provenance=Provenance("artifact-1", "hash-1", "test:event-1", "session-1", "event-1"),
        fidelity=FidelityProfile.complete(), content="The project needs a weekly report.",
    )
    session = __import__("personal_knowledge.core.conversation_events", fromlist=["AdaptedSession"]).AdaptedSession(
        session_id="session-1", provenance=Provenance("artifact-1", "hash-1", "test:session-1", "session-1"),
        fidelity=FidelityProfile.complete(), native_session_id="session-1",
    )
    EventRepository(db).write_generation(GenerationInput(
        family="test", adapter_version="1", contract_version="1", capability_digest="cap",
        source_manifest_id="manifest", dataset_digest="digest",
        artifacts=(SourceArtifact("artifact-1", "test", "file", "hash-1", "test", "fixture", 1),),
        sessions=(session,), events=(event,), relations=(),
    ), generation_id="gen-1")
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO ce_generation_authority(generation_id, active) VALUES ('gen-1', 1)")
    from personal_knowledge.application.knowledge.view_candidate_prepare import prepare_view_candidates
    run = prepare_view_candidates(db)
    queue = CandidateRunRepository(db).list_candidates(run["run_id"])[0]
    return run["run_id"], queue["candidate_id"]


def test_stage_and_load_candidate_is_body_free_and_replayable(tmp_path: Path) -> None:
    db = tmp_path / "conversation.sqlite"
    run_id, queue_id = _setup(db)
    store = ExtractedCandidateStore(db)
    kwargs = dict(subject="project", conclusion="Prepare a weekly report.", scope="conversation", confidence=0.8,
                  evidence=[{"event_id": "event-1", "quote": "weekly report"}])

    first = store.stage(db, run_id, queue_id, **kwargs)
    second = store.stage(db, run_id, queue_id, **kwargs)

    assert first == second
    assert first["candidate_id"].startswith("cand_")
    assert "quote" not in json.dumps(first)
    assert store.load_candidates(db)[first["candidate_id"]] == first
    assert first["evidence"][0]["ref"] == "ce.event@gen-1#event-1"


def test_rejects_unqueued_or_unsafe_evidence_and_fails_closed_on_drift(tmp_path: Path) -> None:
    db = tmp_path / "conversation.sqlite"
    run_id, queue_id = _setup(db)
    store = ExtractedCandidateStore(db)
    with pytest.raises(ValueError, match="queue"):
        store.stage(db, run_id, queue_id, subject="p", conclusion="c", scope="s", confidence=0.5,
                    evidence=[{"event_id": "other", "quote": "weekly"}])
    with pytest.raises(ValueError, match="secret|injection"):
        store.stage(db, run_id, queue_id, subject="p", conclusion="use api_key=secret-value", scope="s", confidence=0.5,
                    evidence=[{"event_id": "event-1", "quote": "weekly"}])
    candidate = store.stage(db, run_id, queue_id, subject="p", conclusion="c", scope="s", confidence=0.5,
                            evidence=[{"event_id": "event-1", "quote": "weekly"}])
    with sqlite3.connect(db) as con:
        con.execute("UPDATE ce_events SET content='changed' WHERE event_id='event-1'")
        con.commit()
    assert store.load_candidates(db) == {}


def test_invalid_quote_does_not_create_target_ledger(tmp_path):
    db = tmp_path / "source.sqlite"
    run_id, queue_id = _setup(db)
    target = tmp_path / "review.sqlite"
    with pytest.raises(ValueError):
        ExtractedCandidateStore(target).stage(db, run_id, queue_id, subject="p", conclusion="c", scope="s",
            confidence=0.8, evidence=[{"event_id": "event-1", "quote": "not in source"}])
    assert not target.exists()


def test_modified_proof_is_rejected(tmp_path):
    db = tmp_path / "source.sqlite"
    run_id, queue_id = _setup(db)
    store = ExtractedCandidateStore(tmp_path / "review.sqlite")
    candidate = store.stage(db, run_id, queue_id, subject="p", conclusion="c", scope="s",
        confidence=0.8, evidence=[{"event_id": "event-1", "quote": "weekly report"}])
    with sqlite3.connect(store.db_path) as con:
        proof = json.loads(con.execute("SELECT proof_json FROM ce_extracted_candidates").fetchone()[0])
        proof["events"][0]["quote"] = "project"
        con.execute("UPDATE ce_extracted_candidates SET proof_json=?", (json.dumps(proof),))
    assert candidate["candidate_id"] not in store.load_candidates(db)
