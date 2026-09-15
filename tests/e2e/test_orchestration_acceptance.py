from __future__ import annotations

import hashlib
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import personal_knowledge.services.api_server as api_server
from personal_knowledge.intelligence.analysis.service import AnalysisReadService
from personal_knowledge.intelligence.orchestration import OrchestrationService, apply_schema
from personal_knowledge.services.api_server import Handler, SESSION_WRITE_ROUTES, orchestration_rest_contract
from personal_knowledge.services.mcp_server import orchestration_tool_contract
from personal_knowledge.services.orchestration_service import GuardedOrchestrationInterface
from tests.integration.test_project_pilot_authority import setup_authorities


ACTOR = "d" * 64
NOW = "2026-07-18T09:30:00Z"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup(tmp_path: Path):
    env = setup_authorities(tmp_path)
    orchestration_db = tmp_path / "orchestration.sqlite"
    apply_schema(orchestration_db)
    core = OrchestrationService(
        db_path=orchestration_db, personal_db=env["personal"], external_db=env["external"],
        confirmation_secret=b"phase-33-e2e-confirmation-secret-32-bytes",
    )
    calls = {"provider": 0, "network": 0, "external_actions": 0, "promotions": 0}
    detail = AnalysisReadService(env["analysis"]).get_run(env["run_id"])

    def runner(_manifest, _payload, _reservation_id, _now):
        calls["provider"] += 1
        return {
            "status": "success",
            "references": {
                "run_id": env["run_id"], "candidate_id": env["candidate_id"],
                "run_checksum": detail["run_checksum"],
                "candidate_checksum": detail["candidate_checksum"],
            },
        }

    interface = GuardedOrchestrationInterface(
        service=core, analysis_db=env["analysis"], pilot_db=env["pilot"],
        calibration_db=tmp_path / "calibration.sqlite", generation_runner=runner,
    )
    return env, orchestration_db, core, interface, calls


def test_real_transport_generation_replays_without_second_provider_call(tmp_path: Path) -> None:
    env, _, core, interface, calls = _setup(tmp_path)
    prepared = orchestration_rest_contract("session.prepare", {
        "goal": "Choose a compatible local runtime",
        "constraints": ["local validation only", "manual operation only"],
        "weights": {"safety": 0.7, "speed": 0.3},
        "actor_identity_hash": ACTOR,
        "max_external_age_seconds": 7200,
        "now": "2026-07-18T09:10:00Z",
    }, service=interface)
    assert prepared["ok"]
    confirmed = orchestration_tool_contract("agent_session_confirm", {
        "preview": prepared["data"], "confirmed": True,
        "idempotency_key": "confirm-e2e", "now": "2026-07-18T09:10:00Z",
    }, service=interface)
    assert confirmed["ok"] and confirmed["data"]["state"] == "confirmed"

    preview = orchestration_rest_contract("session.preview", {
        "session_id": confirmed["data"]["session_id"], "transition": "generate",
        "payload": {"personal_evidence": [], "external_evidence": []},
        "actor_identity_hash": ACTOR, "expected_sequence": 1, "now": NOW,
    }, service=interface)["data"]
    args = {"preview": preview, "confirmed": True, "idempotency_key": "generate-e2e", "now": NOW}
    first = orchestration_tool_contract("agent_session_generate", args, service=interface)
    replay = orchestration_tool_contract("agent_session_generate", args, service=interface)
    assert first["ok"], first
    assert replay["ok"], replay
    assert first["data"]["event_id"] == replay["data"]["event_id"]
    assert replay["data"]["replayed"] is True
    assert calls == {"provider": 1, "network": 0, "external_actions": 0, "promotions": 0}
    resumed = orchestration_rest_contract(
        "session.explain", {"session_id": confirmed["data"]["session_id"], "now": NOW}, service=interface,
    )
    assert resumed["data"]["state"] == "generated"
    assert resumed["data"]["next_operation"] == "publish"
    assert env["run_id"] == first["data"]["references"]["run_id"]


def test_rejected_inputs_leave_every_authority_unchanged(tmp_path: Path) -> None:
    env, orchestration_db, core, interface, calls = _setup(tmp_path)
    paths = [Path(env[key]) for key in ("personal", "external", "analysis", "pilot")] + [orchestration_db]
    before = {path: _sha(path) for path in paths}
    rejected = orchestration_rest_contract("session.prepare", {
        "goal": "Deploy investment automation", "constraints": ["send message"],
        "weights": {"speed": 1.0}, "actor_identity_hash": ACTOR, "now": NOW,
    }, service=interface)
    assert rejected["error"]["code"] == "high_risk_or_external_action_forbidden"
    assert {path: _sha(path) for path in paths} == before

    prepared = interface.invoke(
        "session.prepare", goal="Evaluate another local runtime", constraints=["manual only"],
        weights={"safety": 1.0}, actor_identity_hash=ACTOR,
        max_external_age_seconds=7200, now="2026-07-18T09:10:00Z",
    )["data"]
    token = core.issue_confirmation(prepared, expires_at="2026-07-18T09:11:00Z")
    before_expired = {path: _sha(path) for path in paths}
    expired = interface.invoke(
        "session.confirm", preview=prepared, confirmation_token=token,
        idempotency_key="expired-e2e", now="2026-07-18T09:11:01Z",
    )
    assert expired["error"]["code"] == "confirmation_expired"
    assert {path: _sha(path) for path in paths} == before_expired
    assert calls == {"provider": 0, "network": 0, "external_actions": 0, "promotions": 0}


# --- Phase 38-03：真实 transport 下的跨源/篡改负向验收（D-38-07 / T-38-12） ---

FAKE_ORIGIN = "http://evil.example.com"
FAKE_TOKEN = "fake-confirmation-token-e2e-38-03"


@pytest.fixture()
def live_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _http(server, method: str, path: str, *, origin: str | None = None, body=None):
    host, port = server.server_address[0], server.server_address[1]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        return {"status": resp.status, "headers": dict(resp.getheaders()), "body": resp.read()}
    finally:
        conn.close()


def test_cross_origin_mutations_leave_all_authority_fingerprints_unchanged(
    tmp_path: Path, live_server, monkeypatch,
) -> None:
    """跨 origin POST/OPTIONS 打到真实 transport + 真实 authority 接线：
    全部 403/安全拒绝、零委派、orchestration/Analysis/Pilot/Calibration 指纹不变。"""
    env, orchestration_db, _, interface, calls = _setup(tmp_path)

    def bound(operation, params, *, service=None):
        return orchestration_rest_contract(operation, params, service=interface)

    monkeypatch.setattr(api_server, "orchestration_rest_contract", bound)
    paths = [Path(env[key]) for key in ("personal", "external", "analysis", "pilot")] + [orchestration_db]
    before = {path: _sha(path) for path in paths}

    for route in SESSION_WRITE_ROUTES:
        preflight = _http(live_server, "OPTIONS", route, origin=FAKE_ORIGIN)
        assert "Access-Control-Allow-Origin" not in preflight["headers"]
        resp = _http(
            live_server, "POST", route, origin=FAKE_ORIGIN,
            body={"confirmation_token": FAKE_TOKEN, "preview": {"operation": "confirm"}},
        )
        assert resp["status"] in (401, 403), f"{route} accepted cross-origin mutation"
        text = resp["body"].decode("utf-8")
        assert FAKE_TOKEN not in text and FAKE_ORIGIN not in text
        assert json.loads(resp["body"])["error"]["code"] == "origin_not_allowed"

    assert {path: _sha(path) for path in paths} == before
    assert calls == {"provider": 0, "network": 0, "external_actions": 0, "promotions": 0}


def test_same_origin_tampered_preview_returns_typed_error_without_side_effects(
    tmp_path: Path, live_server, monkeypatch,
) -> None:
    """同源路径仍到达既有 guarded contract（最小互操作冒烟）：
    篡改 Preview → 真实 transport 返回稳定 typed code；指纹不变、无敏感泄露。
    完整真机浏览器 UAT（响应式/无障碍/离线矩阵）留给 Phase 40。"""
    env, orchestration_db, _, interface, calls = _setup(tmp_path)

    def bound(operation, params, *, service=None):
        return orchestration_rest_contract(operation, params, service=interface)

    monkeypatch.setattr(api_server, "orchestration_rest_contract", bound)
    host, port = live_server.server_address[0], live_server.server_address[1]
    same_origin = f"http://{host}:{port}"

    prepared = _http(
        live_server, "POST", "/agent/session/prepare", origin=same_origin,
        body={
            "goal": "Choose a compatible local runtime",
            "constraints": ["local validation only"],
            "weights": {"safety": 1.0},
            "actor_identity_hash": ACTOR,
            "now": "2026-07-18T09:10:00Z",
        },
    )
    assert prepared["status"] == 200
    preview = json.loads(prepared["body"])["data"]

    paths = [Path(env[key]) for key in ("personal", "external", "analysis", "pilot")] + [orchestration_db]
    before = {path: _sha(path) for path in paths}
    tampered = dict(preview)
    tampered["payload"] = {**preview["payload"], "goal": "tampered-goal"}
    resp = _http(
        live_server, "POST", "/agent/session/confirm", origin=same_origin,
        body={"preview": tampered, "confirmed": True, "idempotency_key": "tamper-e2e",
              "now": "2026-07-18T09:10:00Z"},
    )
    assert resp["status"] == 400
    envelope = json.loads(resp["body"])
    error = envelope["error"]
    assert error["code"] == "preview_checksum_mismatch"
    assert set(error) == {"code", "category", "message", "retryable", "recovery_actions"}
    text = resp["body"].decode("utf-8")
    assert "tampered-goal" not in text  # 不回显请求 payload
    assert {path: _sha(path) for path in paths} == before
    assert calls == {"provider": 0, "network": 0, "external_actions": 0, "promotions": 0}
