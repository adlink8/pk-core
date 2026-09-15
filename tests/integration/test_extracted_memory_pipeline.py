"""Extracted evidence through review, REST and the real Pi SDK transport."""
from pathlib import Path
from personal_knowledge.application.conversation.event_generations import GenerationLifecycle


def _assert_extracted_at_model(db, reflection, review, tmp_path, monkeypatch, present, absent, number):
    import json
    import subprocess
    import threading
    from http.server import ThreadingHTTPServer
    from personal_knowledge.services import api_server
    from personal_knowledge.services.pi_domain_gateway import PiDomainGateway

    monkeypatch.setattr(api_server, "PI_DOMAIN_GATEWAY", PiDomainGateway(
        capability="fixture-capability", conversation_db=db, reflection_db=reflection, review_db=review))
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    directory = tmp_path / f"model-{number}"
    directory.mkdir()
    root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(["node", str(root / "apps/personal_intelligence_kernel/test/fixtures/memory-sdk-probe.mjs")],
            input=json.dumps({"port": server.server_port, "dir": str(directory), "scope": "project:alpha",
                              "present": present, "absent": absent}), text=True, capture_output=True, cwd=root, timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["transport_calls"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_extracted_evidence_reaches_review_and_projection_then_withdraws(tmp_path, monkeypatch, _activate, _generation):
    from dataclasses import replace
    from personal_knowledge.application.knowledge.delta_candidate_consumer import consume_delta_candidates
    from personal_knowledge.application.knowledge.view_candidate_prepare import CandidateRunRepository
    from personal_knowledge.services.pi_domain_gateway import PiDomainGateway

    db = tmp_path / "source.sqlite"
    reflection = tmp_path / "reflection.sqlite"
    review = tmp_path / "review.sqlite"
    first = _generation("d-1", "shell preference")
    user = replace(first.events[0], content="Use PowerShell for commands in this project.")
    assistant = replace(first.events[1], content="I will use PowerShell for this project.")
    first = replace(first, events=(user, assistant, *first.events[2:]))
    life = GenerationLifecycle(db)
    life.prepare(first, generation_id="g-1")
    _activate(life, "g-1", digest="d-1")
    batch = consume_delta_candidates(db)
    item = next(item for item in CandidateRunRepository(db).list_candidates(batch["run_id"])
                if user.event_id in item["evidence_event_refs"])
    from personal_knowledge.application.conversation.memory_extraction_request import prepare_memory_extraction, stage_memory_extraction_response
    from personal_knowledge.core.providers import ReplayProvider
    extraction = prepare_memory_extraction(db, batch["run_id"], item["candidate_id"], scope="project:alpha")
    response = ReplayProvider({"proposal": {
        "subject": "shell", "conclusion": "Use PowerShell for this project", "confidence": 0.9,
        "evidence": [{"event_id": user.event_id, "quote": user.content}],
    }}).generate(extraction.provider_request)
    candidate = stage_memory_extraction_response(db, reflection, extraction, response)["candidate"]
    def invoke(operation, params):
        gateway = PiDomainGateway(capability="fixture", conversation_db=db, reflection_db=reflection, review_db=review)
        result = gateway.invoke(operation, params, capability="fixture")
        assert result["ok"], result
        return result["data"]
    request = {"scope": "project:alpha", "task_id": "fixture", "idempotency_key": "projection",
               "binding": {"scope": "project:alpha"}}
    assert invoke("personal.model_projection.get", request)["status"] == "unknown"
    listed = invoke("candidate.list", {"scope": "project:alpha", "task_id": "fixture",
                                       "idempotency_key": "list", "binding": {"scope": "project:alpha"}})
    assert listed["candidates"][0]["conclusion"] == "Use PowerShell for this project"
    reviewed = invoke("candidate.review", {
        "candidate_id": candidate["candidate_id"], "action": "accept", "expected_version": 1,
        "explicit_confirmation": True, "confirmation_token": "fixture",
        "task_id": "fixture", "binding": {"role": "user-review", "source": "desktop"},
        "idempotency_key": "accept-extracted",
    })
    assert reviewed["status"] == "reviewed"
    projected = invoke("personal.model_projection.get", request)
    assert projected["assertions"][0]["conclusion"] == "Use PowerShell for this project"
    assert projected["support_refs"] == [f"ce.event@g-1#{user.event_id}"]
    _assert_extracted_at_model(db, reflection, review, tmp_path, monkeypatch,
                              ["Use PowerShell for this project"], [], 1)
    stable = replace(first, dataset_digest="d-stable")
    life.prepare(stable, generation_id="g-stable")
    _activate(life, "g-stable", digest="d-stable")
    assert invoke("personal.model_projection.get", request)["assertions"][0]["conclusion"] == "Use PowerShell for this project"
    # The cited user text is unchanged; a different part of model input changed.
    changed = replace(first, dataset_digest="d-2", events=(user, replace(assistant, content="Correction: use Bash."), *first.events[2:]))
    life.prepare(changed, generation_id="g-2")
    _activate(life, "g-2", digest="d-2")
    assert invoke("personal.model_projection.get", request)["status"] == "unknown"
    _assert_extracted_at_model(db, reflection, review, tmp_path, monkeypatch,
                              [], ["Use PowerShell for this project"], 2)
