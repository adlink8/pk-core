from __future__ import annotations

from personal_knowledge.intelligence.proactive.service import ProactiveIntelligenceService
from personal_knowledge.services.api_server import proactive_rest_contract
from personal_knowledge.services.mcp_server import proactive_tool_contract
from tests.unit.test_proactive_controls import _published_candidate
from personal_knowledge.intelligence.proactive.cli import _invoke, build_parser
from personal_knowledge.intelligence.proactive.controls import append_control
from tests.unit.test_proactive_controls import _command


def test_shared_inbox_candidate_explain_and_metrics_are_metadata_only(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    service = ProactiveIntelligenceService(db)
    inbox = service.invoke("inbox.list", limit=10)
    assert inbox["ok"] and inbox["privacy"] == {"metadata_only": True, "private_bodies": 0}
    item = inbox["data"]["items"][0]
    assert item["candidate_id"] == target.record_id
    assert service.invoke("candidates.get", candidate_id=target.record_id)["ok"]
    assert service.invoke("candidates.explain", candidate_id=target.record_id)["ok"]
    assert service.invoke("controls.status", candidate_id=target.record_id)["ok"]
    assert service.invoke("metrics.get")["ok"]


def test_rest_and_mcp_delegate_to_one_read_contract(tmp_path) -> None:
    db, _ = _published_candidate(tmp_path)
    rest = proactive_rest_contract("inbox.list", {"limit": "10"}, db_path=db)
    mcp = proactive_tool_contract("proactive_inbox", {"limit": 10}, db_path=db)
    assert rest == mcp
    assert proactive_tool_contract("proactive_control_write", {}, db_path=db)["error"]["code"] == "unknown_operation"


def test_limits_and_reads_are_stable_and_side_effect_free(tmp_path) -> None:
    db, _ = _published_candidate(tmp_path)
    service = ProactiveIntelligenceService(db)
    assert service.invoke("inbox.list", limit=0)["error"]["code"] == "invalid_limit"
    assert service.invoke("inbox.list", limit=101)["error"]["code"] == "invalid_limit"
    before = db.stat().st_size
    first = service.invoke("digest.get", limit=10)
    second = service.invoke("digest.get", limit=10)
    assert first == second and db.stat().st_size == before


def test_guarded_local_surface_append_is_explicit_and_idempotent(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    argv = ["--db", str(db), "surface", "--candidate-id", target.record_id,
            "--candidate-checksum", target.record_checksum, "--event-type", "presented",
            "--occurred-at", "2026-07-18T12:00:00Z", "--actor-class", "user",
            "--actor-identity-hash", "4"*64, "--expected-sequence", "0",
            "--idempotency-key", "present-one", "--write", "--i-confirm", target.record_id]
    first = _invoke(build_parser().parse_args(argv))
    second = _invoke(build_parser().parse_args(argv))
    assert first["ok"] and first["status"] == "written"
    assert second["ok"] and second["status"] == "existing"
    assert first["external_actions"] == second["external_actions"] == 0


def test_controls_status_cli_preserves_default_as_of(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    result = _invoke(build_parser().parse_args([
        "--db", str(db), "controls-status", "--candidate-id", target.record_id,
    ]))
    assert result["ok"] and result["data"]["eligible"] is True


def test_reads_validate_historical_run_frontier_then_apply_current_overlay(tmp_path) -> None:
    db, target = _published_candidate(tmp_path)
    service = ProactiveIntelligenceService(db)
    before = service.invoke("candidates.get", candidate_id=target.record_id)
    assert before["ok"]
    original_lineage = before["data"]["control_frontier_checksum"]

    suppression = append_control(db, _command(target, "suppress", "read-suppress"), write=True).event
    after = service.invoke("candidates.get", candidate_id=target.record_id)
    status = service.invoke("controls.status", candidate_id=target.record_id,
                            as_of="2026-07-18T12:00:00Z")
    explain = service.invoke("candidates.explain", candidate_id=target.record_id)
    assert after["ok"] and status["ok"] and explain["ok"]
    assert after["data"]["control_frontier_checksum"] == original_lineage
    assert after["data"]["current_control_frontier_checksum"] != original_lineage
    assert status["data"]["eligible"] is False

    append_control(db, _command(target, "restore", "read-restore", expected=1,
                                rollback_of=suppression.event_id), write=True)
    restored = service.invoke("controls.status", candidate_id=target.record_id,
                              as_of="2026-07-18T12:00:00Z")
    assert restored["ok"] and restored["data"]["eligible"] is True
    assert service.invoke("candidates.get", candidate_id=target.record_id)["data"]["control_frontier_checksum"] == original_lineage
