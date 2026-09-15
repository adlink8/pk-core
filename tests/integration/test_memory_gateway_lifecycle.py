"""Memory lifecycle through the production gateway, without injected adapters.

Seam: PiDomainGateway reflection.stage -> candidate.review -> model_projection.
Real temporary stores must survive gateway reconstruction. No live data/provider.
"""
from __future__ import annotations

import hashlib
import json

from personal_knowledge.services.pi_domain_gateway import PiDomainGateway


def metadata(**overrides):
    digest = "a" * 64
    return {
        "event_id": "pi_evt_" + "b" * 64,
        "canonical_checksum": digest,
        "watermark": digest,
        "rule_version": "conversation-reflection-v1",
        "source": "pk-sync",
        "snapshot": "agentsview@" + "c" * 64,
        "scope": "agent.conversation",
        "publication_version": "2026-09-05T00:00:00.000Z#1",
        "occurred_at": "2026-09-05T00:00:00.000Z",
        "freshness": {
            leg: {"status": "current", "watermark": digest, "observed_at": "2026-09-05T00:00:00.000Z"}
            for leg in ("source_to_agentsview", "agentsview_to_canonical")
        },
        "task_id": "task-memory-stage",
        "idempotency_key": "memory-stage-1",
        "binding": {"scope": "agent.conversation", "role": "reflection-consumer"},
        **overrides,
    }


def gateway(tmp_path):
    return PiDomainGateway(
        capability="fixture-capability",
        reflection_db=tmp_path / "reflection.sqlite",
        review_db=tmp_path / "review.sqlite",
    )


def invoke(tmp_path, operation, request):
    result = gateway(tmp_path).invoke(operation, request, capability="fixture-capability")
    assert result["ok"], result
    return result["data"]


def review_request(candidate_id, **overrides):
    return {
        "candidate_id": candidate_id, "action": "accept", "expected_version": 1,
        "explicit_confirmation": True, "confirmation_token": "fixture-confirmed",
        "task_id": "task-review", "binding": {"role": "user-review", "source": "desktop"},
        "idempotency_key": "memory-accept-1", **overrides,
    }


def projection(tmp_path, scope="agent.conversation"):
    return invoke(tmp_path, "personal.model_projection.get", {
        "scope": scope, "task_id": "task-project", "idempotency_key": "projection-read",
        "binding": {"scope": scope},
    })


def test_default_gateway_review_and_projection_survive_restart(tmp_path):
    staged = invoke(tmp_path, "conversation.reflection.stage", metadata())
    assert staged["status"] == "staged"
    reviewed = invoke(tmp_path, "candidate.review", review_request(staged["candidate_id"]))
    assert reviewed["status"] == "reviewed", reviewed
    projected = projection(tmp_path)
    assert projected["version"] >= 1
    assert projected["support_count"] == 2
    assert projected["status"] == "uncertain"


def test_accepted_edit_is_used_after_restart_and_undo_removes_it(tmp_path):
    staged = invoke(tmp_path, "conversation.reflection.stage", metadata())
    payload = {"conclusion": "Use PowerShell for this project", "confidence": 0.9}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    reviewed = invoke(tmp_path, "candidate.review", review_request(
        staged["candidate_id"], action="edit", edited_payload=payload, edited_payload_checksum=digest,
    ))
    assert reviewed["status"] == "reviewed", reviewed
    projected = projection(tmp_path)
    assert projected["confidence"] == 0.9
    assert projected["assertions"][0]["conclusion"] == "Use PowerShell for this project"
    assert projected["assertions"][0]["support_refs"] == projected["support_refs"]
    undone = invoke(tmp_path, "candidate.review", review_request(
        staged["candidate_id"], action="undo", expected_version=2,
        feedback_id=reviewed["feedback_id"], idempotency_key="memory-undo-1",
    ))
    assert undone["status"] == "reviewed", undone
    assert projection(tmp_path)["status"] == "unknown"


def test_empty_projection_does_not_create_stores(tmp_path):
    assert projection(tmp_path)["status"] == "unknown"
    assert list(tmp_path.iterdir()) == []


def remember(tmp_path, *, sequence, scope, subject, conclusion):
    staged = invoke(tmp_path, "conversation.reflection.stage", metadata(
        event_id=f"pi_evt_{sequence:064x}", scope=scope,
        binding={"scope": scope, "role": "reflection-consumer"},
        idempotency_key=f"stage-{sequence}",
    ))
    payload = {"subject": subject, "conclusion": conclusion, "confidence": 0.9}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    reviewed = invoke(tmp_path, "candidate.review", review_request(
        staged["candidate_id"], action="edit", edited_payload=payload, edited_payload_checksum=digest,
        idempotency_key=f"review-{sequence}",
    ))
    assert reviewed["status"] == "reviewed", reviewed
    return staged, reviewed


def test_project_exception_preserves_other_preferences_and_undo_restores_default(tmp_path):
    remember(tmp_path, sequence=1, scope="global", subject="shell", conclusion="Use PowerShell")
    remember(tmp_path, sequence=2, scope="global", subject="language", conclusion="Answer in Chinese")
    staged, reviewed = remember(tmp_path, sequence=3, scope="project:alpha", subject="shell", conclusion="Use Bash")
    local = projection(tmp_path, "project:alpha")
    assert {row["conclusion"] for row in local["assertions"]} == {"Use Bash", "Answer in Chinese"}
    assert {row["conclusion"] for row in projection(tmp_path, "global")["assertions"]} == {"Use PowerShell", "Answer in Chinese"}
    invoke(tmp_path, "candidate.review", review_request(
        staged["candidate_id"], action="undo", expected_version=2,
        feedback_id=reviewed["feedback_id"], idempotency_key="undo-project-exception",
    ))
    assert {row["conclusion"] for row in projection(tmp_path, "project:alpha")["assertions"]} == {"Use PowerShell", "Answer in Chinese"}


def test_expired_project_exception_does_not_hide_current_default(tmp_path):
    remember(tmp_path, sequence=1, scope="global", subject="shell", conclusion="Use PowerShell")
    staged, _ = remember(tmp_path, sequence=2, scope="project:alpha", subject="shell", conclusion="Use Bash")
    payload = {"valid_from": "2000-01-01T00:00:00Z", "observed_at": "2000-01-01T00:00:00Z", "valid_to": "2001-01-01T00:00:00Z"}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    edited = invoke(tmp_path, "candidate.review", review_request(staged["candidate_id"], action="edit", expected_version=2,
        edited_payload=payload, edited_payload_checksum=digest, idempotency_key="expire-project"))
    assert edited["status"] == "reviewed"
    result = projection(tmp_path, "project:alpha")
    assert {row["conclusion"] for row in result["assertions"]} == {"Use PowerShell"}
    assert result["assertions"][0]["source_scope"] == "global"


def test_coexist_by_context_preserves_project_exception(tmp_path):
    remember(tmp_path, sequence=1, scope="global", subject="shell", conclusion="Use PowerShell")
    staged, _ = remember(tmp_path, sequence=2, scope="project:alpha", subject="shell", conclusion="Use Bash")
    payload = {"conclusion": "Use Bash"}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    edited = invoke(tmp_path, "candidate.review", review_request(staged["candidate_id"], action="edit", expected_version=2,
        edited_payload=payload, edited_payload_checksum=digest, conflict_disposition="coexist_by_context", idempotency_key="coexist-project"))
    assert edited["status"] == "reviewed"
    assert {row["conclusion"] for row in projection(tmp_path, "project:alpha")["assertions"]} == {"Use Bash"}
    assert {row["conclusion"] for row in projection(tmp_path, "global")["assertions"]} == {"Use PowerShell"}


def test_undo_latest_edit_restores_previous_accepted_value(tmp_path):
    staged, _ = remember(tmp_path, sequence=1, scope="global", subject="shell", conclusion="Use PowerShell")
    payload = {"conclusion": "Use Bash"}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    edited = invoke(tmp_path, "candidate.review", review_request(
        staged["candidate_id"], action="edit", expected_version=2, edited_payload=payload,
        edited_payload_checksum=digest, idempotency_key="edit-shell-again",
    ))
    assert edited["status"] == "reviewed"
    before_undo = projection(tmp_path, "global")
    assert before_undo["assertions"][0]["conclusion"] == "Use Bash"
    invoke(tmp_path, "candidate.review", review_request(
        staged["candidate_id"], action="undo", expected_version=3,
        feedback_id=edited["feedback_id"], idempotency_key="undo-second-edit",
    ))
    after_undo = projection(tmp_path, "global")
    assert after_undo["assertions"][0]["conclusion"] == "Use PowerShell"
    assert after_undo["version"] > before_undo["version"]


def test_candidate_edit_rejects_nested_payload_before_persistence(tmp_path):
    staged = invoke(tmp_path, "conversation.reflection.stage", metadata())
    payload = {"conclusion": {"secret": "fixture-must-not-be-stored"}}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = invoke(tmp_path, "candidate.review", review_request(
        staged["candidate_id"], action="edit", edited_payload=payload, edited_payload_checksum=digest,
    ))
    assert result["status"] == "rejected"
    assert b"fixture-must-not-be-stored" not in (tmp_path / "review.sqlite").read_bytes()


def test_undo_ignore_restores_memory_and_undo_of_undo_hides_it_again(tmp_path):
    staged, _ = remember(tmp_path, sequence=1, scope="global", subject="shell", conclusion="Use PowerShell")
    ignored = invoke(tmp_path, "candidate.review", review_request(
        staged["candidate_id"], action="ignore", expected_version=2,
        idempotency_key="ignore-shell",
    ))
    assert ignored["status"] == "reviewed", ignored
    assert projection(tmp_path, "global")["status"] == "unknown"
    restored = invoke(tmp_path, "candidate.review", review_request(
        staged["candidate_id"], action="undo", expected_version=3,
        feedback_id=ignored["feedback_id"], idempotency_key="undo-ignore-shell",
    ))
    assert restored["status"] == "reviewed", restored
    assert projection(tmp_path, "global")["assertions"][0]["conclusion"] == "Use PowerShell"
    revoked = invoke(tmp_path, "candidate.review", review_request(
        staged["candidate_id"], action="undo", expected_version=4,
        feedback_id=restored["feedback_id"], idempotency_key="undo-restoration",
    ))
    assert revoked["status"] == "reviewed", revoked
    assert projection(tmp_path, "global")["status"] == "unknown"


def test_reviewed_memory_crosses_rest_bridge_to_real_sdk_and_honors_undo(tmp_path, monkeypatch):
    from http.server import ThreadingHTTPServer
    from pathlib import Path
    import subprocess
    import threading
    from personal_knowledge.services import api_server

    remember(tmp_path, sequence=1, scope="global", subject="shell", conclusion="Use PowerShell")
    remember(tmp_path, sequence=2, scope="global", subject="language", conclusion="Answer in Chinese")
    staged, edited = remember(tmp_path, sequence=3, scope="project:alpha", subject="shell", conclusion="Use Bash")
    monkeypatch.setattr(api_server, "PI_DOMAIN_GATEWAY", gateway(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = Path(__file__).resolve().parents[2]
    probe = root / "apps/personal_intelligence_kernel/test/fixtures/memory-sdk-probe.mjs"
    def run(scope, present, absent, number):
        directory = tmp_path / f"sdk-{number}"
        directory.mkdir()
        result = subprocess.run(["node", str(probe)], input=json.dumps({
            "port": server.server_port, "dir": str(directory), "scope": scope,
            "present": present, "absent": absent,
        }), text=True, capture_output=True, cwd=root, timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["transport_calls"] == 1
    try:
        run("project:alpha", ["Use Bash", "Answer in Chinese"], ["Use PowerShell"], 1)
        run("global", ["Use PowerShell", "Answer in Chinese"], ["Use Bash"], 2)
        expired_payload = {
            "valid_from": "2000-01-01T00:00:00Z",
            "observed_at": "2000-01-01T00:00:00Z",
            "valid_to": "2001-01-01T00:00:00Z",
        }
        expired_digest = hashlib.sha256(
            json.dumps(expired_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        expired = invoke(tmp_path, "candidate.review", review_request(
            staged["candidate_id"], action="edit", expected_version=2,
            edited_payload=expired_payload, edited_payload_checksum=expired_digest,
            idempotency_key="expire-live-project",
        ))
        assert expired["status"] == "reviewed"
        run("project:alpha", ["Use PowerShell", "Answer in Chinese"], ["Use Bash"], 4)
        invoke(tmp_path, "candidate.review", review_request(
            staged["candidate_id"], action="undo", expected_version=2,
            feedback_id=edited["feedback_id"], idempotency_key="undo-live-project",
        ))
        run("project:alpha", ["Use PowerShell", "Answer in Chinese"], ["Use Bash"], 3)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_candidate_list_shares_persistent_review_state_without_creating_stores(tmp_path):
    request = {"scope": "global", "limit": 10, "task_id": "list-task", "idempotency_key": "list-1", "binding": "user-review"}
    assert invoke(tmp_path, "candidate.list", request)["candidates"] == []
    assert list(tmp_path.iterdir()) == []
    staged, edited = remember(tmp_path, sequence=1, scope="global", subject="shell", conclusion="Use PowerShell")
    remember(tmp_path, sequence=2, scope="project:other", subject="shell", conclusion="Use Bash")
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}
    rows = invoke(tmp_path, "candidate.list", request)["candidates"]
    assert len(rows) == 1
    assert rows[0]["conclusion"] == "Use PowerShell"
    assert rows[0]["review_status"] == "accepted"
    assert rows[0]["current_version"] == 2
    assert rows[0]["candidate_id"] == staged["candidate_id"]
    assert all(path.read_bytes() == content for path, content in before.items())
    invoke(tmp_path, "candidate.review", review_request(
        staged["candidate_id"], action="undo", expected_version=2,
        feedback_id=edited["feedback_id"], idempotency_key="undo-listed",
    ))
    item = invoke(tmp_path, "candidate.list", request)["candidates"][0]
    assert item["current_version"] == 3
    assert item["review_status"] == "pending"
    assert item["conclusion"] == ""


def test_desktop_preload_and_kernel_review_routes_reach_persistent_gateway(tmp_path, monkeypatch):
    from http.server import ThreadingHTTPServer
    from pathlib import Path
    import subprocess
    import threading
    from personal_knowledge.services import api_server

    invoke(tmp_path, "conversation.reflection.stage", metadata(scope="global"))
    monkeypatch.setattr(api_server, "PI_DOMAIN_GATEWAY", gateway(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = Path(__file__).resolve().parents[2]
    directory = tmp_path / "desktop-runtime"
    directory.mkdir()
    try:
        result = subprocess.run(["node", str(root / "apps/personal_intelligence_desktop/test/fixtures/memory-review-runtime.mjs")],
            input=json.dumps({"port": server.server_port, "dir": str(directory)}), text=True, capture_output=True, cwd=root, timeout=30)
        assert result.returncode == 0, result.stderr
        assert '"undone_version":3' in result.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
