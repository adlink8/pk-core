from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from personal_knowledge.intelligence.analysis.schema import checksum
from personal_knowledge.intelligence.decision.context_binding import DecisionContextBinding, DecisionContextPolicy
from personal_knowledge.intelligence.orchestration import OrchestrationService, apply_schema
from personal_knowledge.services.api_server import orchestration_rest_contract
from personal_knowledge.services.mcp_server import active_tools, orchestration_tool_contract
from personal_knowledge.services.orchestration_service import GuardedOrchestrationInterface


NOW = "2026-07-19T02:00:00Z"
ACTOR = "c" * 64


def _binding(*_args, **_kwargs):
    draft = DecisionContextBinding(
        personal_snapshot_id="ss_fixture", personal_snapshot_hash="a" * 64,
        external_snapshot_id="exs_fixture", external_snapshot_hash="b" * 64,
        policy=DecisionContextPolicy("global", 86400), bound_at=NOW, binding_hash="",
    )
    return replace(draft, binding_hash=checksum(draft.core()))


def _validate(value, *_args, **_kwargs):
    typed = DecisionContextBinding.from_dict(value)
    if checksum(typed.core()) != typed.binding_hash:
        raise ValueError("binding_hash_mismatch")
    return {"ok": True}


def _interface(tmp_path: Path):
    db = tmp_path / "orchestration.sqlite"
    apply_schema(db)
    core = OrchestrationService(
        db_path=db, personal_db=tmp_path / "personal.sqlite", external_db=tmp_path / "external.sqlite",
        confirmation_secret=b"contract-confirmation-secret-at-least-32-bytes",
        binding_factory=_binding, binding_validator=_validate,
    )
    return GuardedOrchestrationInterface(service=core), core


def test_rest_and_stdio_delegate_to_identical_shared_contract(tmp_path: Path) -> None:
    interface, _ = _interface(tmp_path)
    params = {
        "goal": "Choose local validation",
        "constraints": ["manual operation only"],
        "weights": {"safety": 1.0},
        "actor_identity_hash": ACTOR,
        "now": NOW,
    }
    rest = orchestration_rest_contract("session.prepare", params, service=interface)
    mcp = orchestration_tool_contract("agent_session_prepare", params, service=interface)
    assert rest == mcp and rest["ok"]
    assert rest["data"]["operation"] == "confirm"


def test_default_generation_runner_is_pi_kernel_owned(tmp_path: Path) -> None:
    interface, _ = _interface(tmp_path)
    runner = interface.generation_runner
    assert runner.__class__.__name__ == "ExistingAnalysisAdapter"
    assert runner.provider.__class__.__name__ == "PiKernelProvider"
    assert runner.provider.purpose == "guarded_generation"


def test_confirmation_preview_resume_and_stable_errors(tmp_path: Path) -> None:
    interface, core = _interface(tmp_path)
    prepared = interface.invoke(
        "session.prepare", goal="Choose local validation", constraints=["manual only"],
        weights={"safety": 1.0}, actor_identity_hash=ACTOR, now=NOW,
    )["data"]
    refused = interface.invoke(
        "session.confirm", preview=prepared, confirmed=False,
        idempotency_key="refused", now=NOW,
    )
    assert refused["error"]["code"] == "explicit_confirmation_required"
    confirmed = orchestration_tool_contract("agent_session_confirm", {
        "preview": prepared, "confirmed": True,
        "idempotency_key": "confirm-contract", "now": NOW,
    }, service=interface)
    assert confirmed["ok"] and confirmed["data"]["state"] == "confirmed"
    resumed = orchestration_rest_contract(
        "session.resume", {"session_id": confirmed["data"]["session_id"], "now": NOW}, service=interface,
    )
    assert resumed["data"]["sequence"] == 1

    missing = interface.invoke("session.confirm", preview=prepared, idempotency_key="missing", now=NOW)
    assert missing["error"]["code"] == "explicit_confirmation_required"
    stale = interface.invoke(
        "session.preview", session_id=confirmed["data"]["session_id"], transition="generate",
        payload={}, actor_identity_hash=ACTOR, expected_sequence=0, now=NOW,
    )
    assert stale["error"]["code"] == "stale_expected_sequence"
    undeclared = interface.invoke("session.resume", session_id="x", surprise=True)
    assert undeclared["error"]["code"] == "undeclared_input"


def test_negative_paths_return_stable_typed_codes_and_fail_closed(tmp_path: Path) -> None:
    """Phase 38-03（D-38-05/D-38-06/DEC-03）：篡改/重放/actor drift/非法 transition/同键异 payload
    都返回稳定 typed code；重复同 payload confirm 返回同一 event 且 replayed=True。"""
    interface, _ = _interface(tmp_path)
    prepared = interface.invoke(
        "session.prepare", goal="Choose local validation", constraints=["manual only"],
        weights={"safety": 1.0}, actor_identity_hash=ACTOR, now=NOW,
    )["data"]

    # Preview 篡改：payload 变但 checksum 保留 → 稳定 typed code，不产生 session
    tampered = dict(prepared)
    tampered["payload"] = {**prepared["payload"], "goal": "tampered-goal"}
    result = interface.invoke(
        "session.confirm", preview=tampered, confirmed=True, idempotency_key="tamper", now=NOW,
    )
    assert result["error"]["code"] == "preview_checksum_mismatch"
    missing = interface.invoke("session.resume", session_id=prepared["session_id"], now=NOW)
    assert missing["error"]["code"] == "session_missing"  # 篡改路径零写入

    # D-38-05：重复同 payload confirm → 同一 event + replayed=True（不是第二次写入）
    first = interface.invoke(
        "session.confirm", preview=prepared, confirmed=True, idempotency_key="dup-confirm", now=NOW,
    )
    replay = interface.invoke(
        "session.confirm", preview=prepared, confirmed=True, idempotency_key="dup-confirm", now=NOW,
    )
    assert first["ok"] and replay["ok"]
    assert replay["data"]["event_id"] == first["data"]["event_id"]
    assert replay["data"]["event_checksum"] == first["data"]["event_checksum"]
    assert replay["data"]["sequence"] == first["data"]["sequence"]
    assert first["data"]["replayed"] is False and replay["data"]["replayed"] is True
    session_id = first["data"]["session_id"]

    # D-38-06：actor identity drift → 只读 resume 仍可用，写路径稳定拒绝
    mismatch = interface.invoke(
        "session.preview", session_id=session_id, transition="generate",
        payload={"personal_evidence": [], "external_evidence": []},
        actor_identity_hash="e" * 64, expected_sequence=1, now=NOW,
    )
    assert mismatch["error"]["code"] == "actor_identity_mismatch"
    readonly = interface.invoke("session.resume", session_id=session_id, now=NOW)
    assert readonly["ok"] and readonly["data"]["state"] == "confirmed"

    # 非法 transition（跳过 generate 直接 publish）→ 稳定 typed code
    illegal = interface.invoke(
        "session.preview", session_id=session_id, transition="publish",
        payload={}, actor_identity_hash=ACTOR, expected_sequence=1, now=NOW,
    )
    assert illegal["error"]["code"] == "illegal_transition"

    # 同键异 payload（同 session、不同 issued_at → 不同 preview_checksum）→ idempotency_conflict
    prepared_later = interface.invoke(
        "session.prepare", goal="Choose local validation", constraints=["manual only"],
        weights={"safety": 1.0}, actor_identity_hash=ACTOR, now="2026-07-19T03:00:00Z",
    )["data"]
    assert prepared_later["session_id"] == session_id
    assert prepared_later["preview_checksum"] != prepared["preview_checksum"]
    conflict = interface.invoke(
        "session.confirm", preview=prepared_later, confirmed=True, idempotency_key="dup-confirm", now=NOW,
    )
    assert conflict["error"]["code"] == "idempotency_conflict"
    # 冲突后事件链不变：仍只有 1 个事件，状态不动
    after = interface.invoke("session.resume", session_id=session_id, now=NOW)
    assert after["data"]["sequence"] == 1 and after["data"]["state"] == "confirmed"


def test_error_envelopes_expose_only_sanitized_recovery_fields(tmp_path: Path) -> None:
    """Phase 38-03（T-38-10）：REST 错误信封只含脱敏 code/category/message/retryable/
    recovery_actions；不出现 token/HMAC/secret/payload/原始异常。"""
    import json as json_module

    interface, core = _interface(tmp_path)
    prepared = interface.invoke(
        "session.prepare", goal="Choose local validation", constraints=["manual only"],
        weights={"safety": 1.0}, actor_identity_hash=ACTOR, now=NOW,
    )["data"]
    token = core.issue_confirmation(prepared, expires_at="2026-07-19T02:01:00Z")
    expired = orchestration_rest_contract("session.confirm", {
        "preview": prepared, "confirmation_token": token,
        "idempotency_key": "expired-envelope", "now": "2026-07-19T02:01:01Z",
    }, service=interface)
    assert expired["ok"] is False
    error = expired["error"]
    assert set(error) == {"code", "category", "message", "retryable", "recovery_actions"}
    assert error["code"] == "confirmation_expired"
    assert error["category"] == "confirmation"
    text = json_module.dumps(expired, ensure_ascii=False)
    assert token not in text  # 确认凭据绝不回显
    assert "hmac" not in text.lower()
    assert prepared["preview_checksum"] not in text or expired["data"] is None  # 不回传 payload


def test_tool_names_are_additive_and_mutations_are_strict() -> None:
    tools = {tool.name: tool for tool in active_tools()}
    assert "search_semantic" in tools
    expected = {
        "agent_session_prepare", "agent_session_confirm", "agent_session_preview",
        "agent_session_generate", "agent_session_publish", "agent_session_decide",
        "agent_session_observe", "agent_session_calibrate", "agent_session_resume", "agent_session_explain",
    }
    assert expected <= set(tools)
    for name in expected - {"agent_session_prepare", "agent_session_preview", "agent_session_resume", "agent_session_explain"}:
        schema = tools[name].inputSchema
        assert schema["additionalProperties"] is False
        assert {"preview", "confirmed", "idempotency_key"} <= set(schema["required"])
