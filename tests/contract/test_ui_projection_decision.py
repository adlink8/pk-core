"""Phase 38:decision_queue.get / decision_workspace.get 投影契约测试。"""
from datetime import datetime, timedelta, timezone

import pytest

from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.intelligence.decision.service import DecisionFeedbackService
from personal_knowledge.services.api_server import ui_rest_contract
from personal_knowledge.services.ui_projection import (
    INTERFACE_SCHEMA_VERSION,
    CockpitProjectionService,
    _classify_stage,
)

STAGE_KEYS = (
    "needs_attention", "awaiting_confirmation", "in_progress",
    "awaiting_outcome", "completed", "closed",
)
QUEUE_CARD_KEYS = {
    "recommendation_id", "domain", "recommendation_kind", "horizon", "confidence",
    "confirmation_state", "action_state", "expires_at", "current_sequence", "snapshot_id",
}
WORKSPACE_AUTHORITIES = {"recommendation", "history", "outcomes", "effectiveness"}

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
FAR_FUTURE = (NOW + timedelta(days=30)).isoformat().replace("+00:00", "Z")
SOON = (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
PAST = (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z")


def _item(**overrides):
    base = {
        "confirmation_state": "proposed",
        "action_state": None,
        "expires_at": FAR_FUTURE,
        "has_outcome": False,
    }
    base.update(overrides)
    return base


def _first_recommendation_id():
    result = DecisionFeedbackService(UNIFIED_DB).invoke("recommendations.list", limit=1)
    if not result.get("ok"):
        return None
    items = result["data"].get("items") or []
    return items[0]["recommendation_id"] if items else None


# --- _classify_stage 纯函数:六分支覆盖 -------------------------------------


def test_classify_stage_closed_by_confirmation():
    for state in ("rejected", "deferred", "revoked"):
        assert _classify_stage(_item(confirmation_state=state), NOW) == "closed"
    # confirmation 终态优先于 outcome / action
    assert _classify_stage(
        _item(confirmation_state="rejected", has_outcome=True), NOW,
    ) == "closed"


def test_classify_stage_awaiting_confirmation():
    assert _classify_stage(_item(), NOW) == "awaiting_confirmation"


def test_classify_stage_needs_attention():
    # 已过期 / 72h 窗口内到期 / expires_at 无法解析 / 未知状态词
    assert _classify_stage(_item(expires_at=PAST), NOW) == "needs_attention"
    assert _classify_stage(_item(expires_at=SOON), NOW) == "needs_attention"
    assert _classify_stage(_item(expires_at="not-a-date"), NOW) == "needs_attention"
    assert _classify_stage(_item(confirmation_state="mystery"), NOW) == "needs_attention"


def test_classify_stage_in_progress():
    # accepted 但 action 未开始;planned / started
    assert _classify_stage(_item(confirmation_state="accepted"), NOW) == "in_progress"
    for action in ("planned", "started"):
        assert _classify_stage(
            _item(confirmation_state="accepted", action_state=action), NOW,
        ) == "in_progress"


def test_classify_stage_awaiting_outcome():
    # action 完成但尚无 outcome
    assert _classify_stage(
        _item(confirmation_state="accepted", action_state="completed"), NOW,
    ) == "awaiting_outcome"


def test_classify_stage_completed():
    # 有 outcome 即闭环,不看 action 细值
    assert _classify_stage(
        _item(confirmation_state="accepted", action_state="completed", has_outcome=True),
        NOW,
    ) == "completed"


def test_classify_stage_closed_by_terminal_action_without_outcome():
    for action in ("abandoned", "not_taken"):
        assert _classify_stage(
            _item(confirmation_state="accepted", action_state=action), NOW,
        ) == "closed"


def test_classify_stage_unknown_confirmation_never_promoted_via_action_state(): # Phase 36 task 3
    # 词表外 confirmation_state(理论上不该在真实状态机中与非空 action_state 共存,
    # 但 Projection 必须对此保守处理)不得因 action_state 而被提升为
    # in_progress/awaiting_outcome/closed 等 action 驱动 stage,一律 needs_attention。
    for action in ("planned", "started", "completed", "abandoned", "not_taken"):
        assert _classify_stage(
            _item(confirmation_state="mystery", action_state=action), NOW,
        ) == "needs_attention"
    # has_outcome 仍是独立、更强的信号,不受词表锁定影响
    assert _classify_stage(
        _item(confirmation_state="mystery", action_state="completed", has_outcome=True), NOW,
    ) == "completed"


# --- decision_queue.get ----------------------------------------------------


def test_decision_queue_envelope_and_stage_shape():
    result = CockpitProjectionService().invoke("decision_queue.get")
    assert result["schema_version"] == INTERFACE_SCHEMA_VERSION
    assert result["ok"] is True
    assert set(result["authorities"]) == {"decision"}
    data = result["data"]
    assert set(data) == {"total_available", "stage_counts", "stages"}
    # 六个 stage 键恒在
    assert tuple(data["stage_counts"]) == STAGE_KEYS
    assert tuple(data["stages"]) == STAGE_KEYS
    # stage_counts 与 stages 数组长度一致
    for key in STAGE_KEYS:
        assert data["stage_counts"][key] == len(data["stages"][key])
    assert sum(data["stage_counts"].values()) <= data["total_available"]
    for key in STAGE_KEYS:
        for card in data["stages"][key]:
            assert QUEUE_CARD_KEYS <= set(card)


def test_decision_queue_failure_degrades_to_zero_shape(monkeypatch):
    def boom(self, operation, **params):
        raise RuntimeError("boom")

    monkeypatch.setattr(DecisionFeedbackService, "invoke", boom)
    result = CockpitProjectionService().invoke("decision_queue.get")
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["authorities"]["decision"] == "error"
    assert any("decision" in item for item in result["limitations"])
    # data 仍保持完整看板零形状,前端无需特判 None
    data = result["data"]
    assert data["total_available"] == 0
    assert tuple(data["stage_counts"]) == STAGE_KEYS
    assert tuple(data["stages"]) == STAGE_KEYS
    assert all(data["stage_counts"][key] == 0 for key in STAGE_KEYS)
    assert all(data["stages"][key] == [] for key in STAGE_KEYS)


def test_decision_queue_locks_confirmation_vocabulary_end_to_end(monkeypatch):
    """Phase 36 task 3:通过完整 decision_queue.get 管道(而非只测纯函数)证明
    真实词表外的 confirmation_state 不会被 action_state 提升为可执行 stage。"""
    synthetic_items = [
        {
            "recommendation_id": "synthetic-known-accepted",
            "domain": "project", "recommendation_kind": "task",
            "horizon": "short", "confidence": 0.5,
            "confirmation_state": "accepted", "action_state": "started",
            "expires_at": None, "current_sequence": 1, "snapshot_id": "s1",
        },
        {
            "recommendation_id": "synthetic-unknown-confirmation",
            "domain": "project", "recommendation_kind": "task",
            "horizon": "short", "confidence": 0.5,
            "confirmation_state": "mystery", "action_state": "started",
            "expires_at": None, "current_sequence": 1, "snapshot_id": "s1",
        },
    ]

    def fake_invoke(self, operation, **params):
        if operation == "recommendations.list":
            return {
                "ok": True,
                "data": {"items": synthetic_items, "total_available": len(synthetic_items)},
            }
        raise AssertionError(f"unexpected operation {operation}")

    monkeypatch.setattr(DecisionFeedbackService, "invoke", fake_invoke)
    result = CockpitProjectionService().invoke("decision_queue.get")
    assert result["ok"] is True
    stages = result["data"]["stages"]
    known_ids = {card["recommendation_id"] for card in stages["in_progress"]}
    assert "synthetic-known-accepted" in known_ids
    unknown_ids = {card["recommendation_id"] for card in stages["needs_attention"]}
    assert "synthetic-unknown-confirmation" in unknown_ids
    for actionable_stage in ("in_progress", "awaiting_outcome", "completed"):
        assert "synthetic-unknown-confirmation" not in {
            card["recommendation_id"] for card in stages[actionable_stage]
        }


# --- decision_workspace.get ------------------------------------------------


def test_decision_workspace_requires_recommendation_id():
    service = CockpitProjectionService()
    for missing in (None, "", "   "):
        result = service.invoke("decision_workspace.get", recommendation_id=missing)
        assert result["ok"] is False
        assert result["schema_version"] == INTERFACE_SCHEMA_VERSION
        assert result["error"]["code"] == "invalid_input"
    result = service.invoke("decision_workspace.get")
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_input"


def test_decision_workspace_real_recommendation():
    rid = _first_recommendation_id()
    if rid is None:
        pytest.skip("库中无真实 recommendation")
    result = CockpitProjectionService().invoke(
        "decision_workspace.get", recommendation_id=rid,
    )
    assert result["ok"] is True
    assert set(result["authorities"]) == WORKSPACE_AUTHORITIES
    data = result["data"]
    assert set(data) == {
        "recommendation", "history", "outcomes", "effectiveness",
        "linked_analysis_run_id",
    }
    recommendation = data["recommendation"]
    assert recommendation["recommendation_id"] == rid
    for field in (
        "recommendation_checksum", "run_id", "snapshot_id", "policy_id",
        "rationale_codes", "support", "uncertainty",
        "confirmation_state", "action_state", "current_sequence",
    ):
        assert field in recommendation
    assert isinstance(recommendation["support"], list)
    assert isinstance(data["history"], list)
    for event in data["history"]:
        assert {
            "event_id", "sequence", "event_type", "typed_record_id",
            "previous_event_checksum", "payload_checksum",
        } <= set(event)
    assert isinstance(data["outcomes"], list)
    assert isinstance(data["effectiveness"], list)
    # linked_analysis_run_id 取自 support 条目的 source_run_id(无 support 回退 source_run_id)
    if recommendation["support"]:
        assert data["linked_analysis_run_id"] == recommendation["support"][0]["source_run_id"]
    else:
        assert data["linked_analysis_run_id"] in {None, recommendation.get("source_run_id")}


def test_decision_workspace_history_failure_partial(monkeypatch):
    rid = _first_recommendation_id()
    if rid is None:
        pytest.skip("库中无真实 recommendation")
    original = DecisionFeedbackService.invoke

    def boom(self, operation, **params):
        if operation == "recommendations.history":
            raise RuntimeError("boom")
        return original(self, operation, **params)

    monkeypatch.setattr(DecisionFeedbackService, "invoke", boom)
    result = CockpitProjectionService().invoke(
        "decision_workspace.get", recommendation_id=rid,
    )
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["authorities"]["history"] == "error"
    assert any("history" in item for item in result["limitations"])
    assert result["data"]["history"] == []
    # 其余节不受 history 故障影响
    assert result["authorities"]["recommendation"] == "ok"
    assert result["data"]["recommendation"]["recommendation_id"] == rid


# --- REST parity ------------------------------------------------------------


def _pop_generated_at(envelope):
    envelope.pop("generated_at")
    envelope["freshness"].pop("generated_at")


def test_rest_adapter_parity_decision_queue():
    service = CockpitProjectionService()
    rest = ui_rest_contract("decision_queue.get", {}, service=service)
    direct = service.invoke("decision_queue.get")
    for envelope in (rest, direct):
        _pop_generated_at(envelope)
    assert rest == direct


def test_rest_adapter_parity_decision_workspace():
    rid = _first_recommendation_id()
    if rid is None:
        pytest.skip("库中无真实 recommendation")
    service = CockpitProjectionService()
    params = {"recommendation_id": rid}
    rest = ui_rest_contract("decision_workspace.get", params, service=service)
    direct = service.invoke("decision_workspace.get", **params)
    for envelope in (rest, direct):
        _pop_generated_at(envelope)
    assert rest == direct


def test_rest_adapter_parity_decision_workspace_invalid_input():
    rest = ui_rest_contract("decision_workspace.get", {}, service=CockpitProjectionService())
    assert rest["ok"] is False
    assert rest["error"]["code"] == "invalid_input"
