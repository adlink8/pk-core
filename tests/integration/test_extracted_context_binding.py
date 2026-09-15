from __future__ import annotations

import sqlite3
import hashlib
from pathlib import Path

import pytest

from personal_knowledge.application.conversation.extracted_candidate_store import ExtractedCandidateStore
from personal_knowledge.application.conversation.event_repository import EventRepository, GenerationInput
from personal_knowledge.application.conversation.event_schema import create_v2_schema
from personal_knowledge.application.knowledge.view_candidate_prepare import CandidateRunRepository
from personal_knowledge.adapters.conversation_sources.contracts import SourceArtifact
from personal_knowledge.core.conversation_events import AdaptedSession, EventKind, FidelityProfile, Provenance, TypedEvent


def _setup(db: Path) -> tuple[str, str]:
    create_v2_schema(db)
    p = lambda eid, content: TypedEvent(eid, "s", EventKind.USER_MESSAGE, Provenance("a", "h", f"x:{eid}", "s", eid), FidelityProfile.complete(), content=content)
    events = (p("e1", "I prefer PowerShell."), p("e2", "Do not use Bash."))
    session = AdaptedSession("s", Provenance("a", "h", "x:s", "s"), FidelityProfile.complete(), native_session_id="s")
    EventRepository(db).write_generation(GenerationInput("test", "1", "1", "cap", "manifest", "digest", (SourceArtifact("a", "test", "file", "h", "test", "x", 1),), (session,), events, ()), "g")
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO ce_generation_authority VALUES ('g',1,'now')")
    from personal_knowledge.application.knowledge.view_candidate_prepare import prepare_view_candidates
    run = prepare_view_candidates(db)
    queue = next(item for item in CandidateRunRepository(db).list_candidates(run["run_id"])
                 if {"e1", "e2"}.issubset(item["evidence_event_refs"]))
    return run["run_id"], queue["candidate_id"]



def test_context_binding_survives_quote_but_withdraws_on_other_event_change(tmp_path: Path) -> None:
    db = tmp_path / "source.sqlite"
    run, queue = _setup(db)
    store = ExtractedCandidateStore(db)
    candidate = store.stage(db, run, queue, subject="shell", conclusion="Use PowerShell", scope="s", confidence=.8,
                             evidence=[{"event_id": "e1", "quote": "PowerShell"}],
                             expected_context_checksums={
                                 "e1": hashlib.sha256(b"I prefer PowerShell.").hexdigest(),
                                 "e2": hashlib.sha256(b"Do not use Bash.").hexdigest(),
                             })
    assert candidate["candidate_id"]
    with sqlite3.connect(db) as con:
        con.execute("UPDATE ce_events SET content='Do not use Bash or cmd.' WHERE event_id='e2'")
        con.commit()
    assert store.load_candidates(db) == {}


def test_context_mapping_must_be_queue_bounded_and_sha256(tmp_path: Path) -> None:
    db = tmp_path / "source.sqlite"
    run, queue = _setup(db)
    with pytest.raises(ValueError, match="context"):
        ExtractedCandidateStore(db).stage(db, run, queue, subject="s", conclusion="c", scope="s", confidence=.5,
                                          evidence=[{"event_id": "e1", "quote": "PowerShell"}],
                                          expected_context_checksums={"outside": "0" * 64})


def test_context_mapping_must_cover_all_evidence_refs(tmp_path: Path) -> None:
    db = tmp_path / "source.sqlite"
    run, queue = _setup(db)
    with pytest.raises(ValueError, match="context mapping must cover evidence"):
        ExtractedCandidateStore(db).stage(
            db, run, queue, subject="s", conclusion="c", scope="s", confidence=.5,
            evidence=[
                {"event_id": "e1", "quote": "PowerShell"},
                {"event_id": "e2", "quote": "Do not use Bash."},
            ],
            expected_context_checksums={
                "e1": hashlib.sha256(b"I prefer PowerShell.").hexdigest(),
            },
        )


def test_legacy_no_context_payload_keeps_proof_bytes(tmp_path: Path) -> None:
    db = tmp_path / "source.sqlite"
    run, queue = _setup(db)
    store = ExtractedCandidateStore(db)
    candidate = store.stage(
        db, run, queue, subject="s", conclusion="c", scope="s", confidence=.5,
        evidence=[{"event_id": "e1", "quote": "PowerShell"}],
    )
    with sqlite3.connect(db) as con:
        proof_json, = con.execute(
            "SELECT proof_json FROM ce_extracted_candidates WHERE candidate_id=?",
            (candidate["candidate_id"],),
        ).fetchone()
    assert '"context"' not in proof_json


def test_context_order_change_with_same_words_withdraws_candidate(tmp_path):
    db = tmp_path / "source.sqlite"
    run, queue = _setup(db)
    store = ExtractedCandidateStore(db)
    candidate = store.stage(db, run, queue, subject="shell", conclusion="Prefer PowerShell.", scope="project", confidence=.8,
        evidence=[{"event_id": "e1", "quote": "PowerShell"}],
        expected_context_checksums={"e1": hashlib.sha256(b"I prefer PowerShell.").hexdigest(),
                                    "e2": hashlib.sha256(b"Do not use Bash.").hexdigest()})
    with sqlite3.connect(db) as con:
        con.execute("UPDATE ce_events SET ordinal=99 WHERE event_id='e1'")
    assert candidate["candidate_id"] not in store.load_candidates(db)
