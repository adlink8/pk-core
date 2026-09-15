"""Real request/proposal adapters with only the model boundary replayed."""
from dataclasses import replace
import json
import sqlite3
import pytest

from personal_knowledge.application.conversation.event_generations import GenerationLifecycle
from personal_knowledge.application.knowledge.delta_candidate_consumer import consume_delta_candidates
from personal_knowledge.application.knowledge.view_candidate_prepare import CandidateRunRepository
from personal_knowledge.core.providers import ReplayProvider


@pytest.fixture
def extraction_case(tmp_path, _generation, _activate):
    db = tmp_path / "events.sqlite"
    life = GenerationLifecycle(db)
    source = _generation("d-1", "Use PowerShell.")
    user = replace(source.events[0], content="Use PowerShell for this project.")
    assistant = replace(source.events[1], content="I will use PowerShell in this project.")
    source = replace(source, events=(user, assistant, *source.events[2:]))
    life.prepare(source, "g-1")
    _activate(life, "g-1", digest="d-1")
    run = consume_delta_candidates(db)
    queue = next(row for row in CandidateRunRepository(db).list_candidates(run["run_id"])
                 if user.event_id in row["evidence_event_refs"])
    return db, life, source, user, assistant, run, queue


def test_bounded_request_and_replayed_response_stage_a_real_candidate(tmp_path, extraction_case, _activate):
    from personal_knowledge.application.conversation.memory_extraction_request import prepare_memory_extraction, stage_memory_extraction_response
    from personal_knowledge.application.conversation.extracted_candidate_store import ExtractedCandidateStore

    db, life, source, user, assistant, run, queue = extraction_case
    before = db.read_bytes()
    request = prepare_memory_extraction(db, run["run_id"], queue["candidate_id"], scope="project:alpha")
    assert db.read_bytes() == before
    assert user.content in request.provider_request.prompt
    assert request.context_checksums
    assert request.scope == "project:alpha"
    response = ReplayProvider({"proposal": {
        "subject": "shell", "conclusion": "Use PowerShell for this project.", "confidence": 0.9,
        "evidence": [{"event_id": user.event_id, "quote": user.content}],
    }}).generate(request.provider_request)
    # Provider execution is explicitly external to request preparation.
    assert response.telemetry.cost_amount == 0
    ledger = tmp_path / "reflection.sqlite"
    result = stage_memory_extraction_response(db, ledger, request, response)
    assert result["status"] == "candidate"
    candidate = result["candidate"]
    assert candidate["scope"] == "project:alpha"
    assert candidate["conclusion"] == "Use PowerShell for this project."
    assert ExtractedCandidateStore(ledger).load_candidates(db)[candidate["candidate_id"]] == candidate

    # Same evidence quote remains, but other input context is corrected.
    corrected = replace(source, dataset_digest="d-2", events=(user, replace(assistant, content="Correction: this project uses Bash."), *source.events[2:]))
    life.prepare(corrected, "g-2")
    _activate(life, "g-2", digest="d-2")
    assert ExtractedCandidateStore(ledger).load_candidates(db) == {}


@pytest.mark.parametrize("payload", [
    {"proposal": {"subject": "x", "conclusion": "y", "confidence": .8, "evidence": [], "scope": "global"}},
    {"proposal": {"subject": "x", "conclusion": "y", "confidence": .8, "evidence": [{"event_id": "outside", "quote": "PowerShell"}]}},
    {"proposal": {"subject": "x", "conclusion": "y", "confidence": True, "evidence": []}},
    {"text": "not json"},
])
def test_invalid_response_cannot_create_review_ledger(tmp_path, extraction_case, payload):
    from personal_knowledge.application.conversation.memory_extraction_request import prepare_memory_extraction, stage_memory_extraction_response
    db, _, _, _, _, run, queue = extraction_case
    request = prepare_memory_extraction(db, run["run_id"], queue["candidate_id"], scope="project:alpha")
    before = db.read_bytes()
    ledger = tmp_path / "invalid.sqlite"
    response = ReplayProvider(payload).generate(request.provider_request)
    with pytest.raises(ValueError):
        stage_memory_extraction_response(db, ledger, request, response)
    assert not ledger.exists()
    assert db.read_bytes() == before


def test_abstain_has_no_candidate_side_effect(tmp_path, extraction_case):
    from personal_knowledge.application.conversation.memory_extraction_request import prepare_memory_extraction, stage_memory_extraction_response
    db, _, _, _, _, run, queue = extraction_case
    request = prepare_memory_extraction(db, run["run_id"], queue["candidate_id"], scope="project:alpha")
    ledger = tmp_path / "abstain.sqlite"
    response = ReplayProvider({"proposal": None}).generate(request.provider_request)
    assert stage_memory_extraction_response(db, ledger, request, response)["status"] == "abstained"
    assert not ledger.exists()


@pytest.mark.parametrize("content", [None, "x" * 32001, "api_key=fixture-do-not-use"])
def test_unsafe_unavailable_or_oversized_input_is_rejected(extraction_case, content):
    from personal_knowledge.application.conversation.memory_extraction_request import prepare_memory_extraction
    db, _, _, user, _, run, queue = extraction_case
    with sqlite3.connect(db) as con:
        con.execute("UPDATE ce_events SET content=? WHERE event_id=?", (content, user.event_id))
    before = db.read_bytes()
    with pytest.raises(ValueError):
        prepare_memory_extraction(db, run["run_id"], queue["candidate_id"], scope="project:alpha")
    assert db.read_bytes() == before


def test_source_change_while_model_runs_prevents_admission(tmp_path, extraction_case):
    from personal_knowledge.application.conversation.memory_extraction_request import prepare_memory_extraction, stage_memory_extraction_response
    db, _, _, _, assistant, run, queue = extraction_case
    request = prepare_memory_extraction(db, run["run_id"], queue["candidate_id"], scope="project:alpha")
    response = ReplayProvider({"proposal": None}).generate(request.provider_request)
    with sqlite3.connect(db) as con:
        con.execute("UPDATE ce_events SET content='Later correction' WHERE event_id=?", (assistant.event_id,))
    ledger = tmp_path / "stale.sqlite"
    with pytest.raises(ValueError, match="input changed"):
        stage_memory_extraction_response(db, ledger, request, response)
    assert not ledger.exists()
