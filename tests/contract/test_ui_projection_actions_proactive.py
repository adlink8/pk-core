"""Phase 39:actions_recent.get / proactive_summary.get / calibration_overview.get 投影契约测试。"""
import base64
import json
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.intelligence.decision.service import (
    DecisionFeedbackService,
    DecisionServiceError,
)
from personal_knowledge.intelligence.proactive.ranking import DEFAULT_RANKING_POLICY
from personal_knowledge.intelligence.proactive.service import ProactiveIntelligenceService
from personal_knowledge.services import api_server
from personal_knowledge.services.api_server import ui_rest_contract
from personal_knowledge.services.decision_intelligence_reads import (
    DecisionIntelligenceReadService,
)
from personal_knowledge.services.ui_projection import (
    INTERFACE_SCHEMA_VERSION,
    CockpitProjectionService,
    _build_timeline,
)

ENVELOPE_KEYS = {
    "schema_version", "operation", "ok", "generated_at", "snapshot_bindings",
    "freshness", "authorities", "partial", "limitations", "data",
}
TIMELINE_STAGES = (
    "recommendation", "decision", "action_start",
    "action_complete", "outcome", "effectiveness",
)
ACTION_ITEM_KEYS = {
    "recommendation_id", "domain", "recommendation_kind", "confirmation_state",
    "action_state", "expires_at", "timeline", "outcomes", "effectiveness",
}
TIMELINE_ENTRY_KEYS = {"stage", "present", "event_id", "sequence", "checksum"}
PROACTIVE_CARD_KEYS = {
    "candidate_id", "domains", "candidate_class", "presentation_kind", "importance",
    "expires_at", "valid_from", "reason_codes",
    "current_control_eligible", "current_control_reason_codes",
}
PROTOCOL_ITEM_KEYS = {
    "protocol_id", "status", "verdict", "causal_claim",
    "inconclusive_reasons", "sample_size", "summary_limitations",
}

# 注入含路径/密钥/Bearer/provider JSON/confirmation-HMAC 字样的异常文本,
# 验证公开 envelope(D-36-06)绝不回显这些片段——只允许 allowlisted safe code/message。
_POISON_MESSAGE = (
    r'path=C:\secret\x key=sk-test-1234567890 auth=Bearer abcdef123 '
    r'provider_body={"provider": "openai", "choices": []} '
    r'confirmation_token=deadbeef1234 hmac=HMAC-SHA256:cafebabe'
)
_POISON_FRAGMENTS = (
    r"C:\secret\x",
    "sk-test-1234567890",
    "Bearer abcdef123",
    '"provider": "openai"',
    "confirmation_token=deadbeef1234",
    "HMAC-SHA256:cafebabe",
    "RuntimeError",
)


def _event(sequence, event_type, typed_record_id):
    return {
        "event_id": f"e{sequence}",
        "sequence": sequence,
        "event_type": event_type,
        "typed_record_id": typed_record_id,
        "previous_event_checksum": f"p{sequence}",
        "payload_checksum": f"c{sequence}",
    }


def _full_chain():
    """真实链形状(读自 state_machine):published → confirmation →
    action(planned→started→completed)→ outcome → assessment。"""
    return [
        _event(1, "recommendation_published", "r1"),
        _event(2, "confirmation", "cf1"),
        _event(3, "action", "a1"),
        _event(4, "action", "a2"),
        _event(5, "action", "a3"),
        _event(6, "outcome", "o1"),
        _event(7, "assessment", "ef1"),
    ], {"a1": "planned", "a2": "started", "a3": "completed"}


# --- _build_timeline 纯函数:六阶段恒在 + event_type→stage 映射 ----------------


def test_build_timeline_six_stages_always_present():
    timeline = _build_timeline([], {})
    assert tuple(entry["stage"] for entry in timeline) == TIMELINE_STAGES
    for entry in timeline:
        assert set(entry) == TIMELINE_ENTRY_KEYS
        assert entry["present"] is False
        assert entry["event_id"] is None
        assert entry["sequence"] is None
        assert entry["checksum"] is None


def test_build_timeline_full_chain_maps_all_stages():
    history, action_states = _full_chain()
    timeline = {entry["stage"]: entry for entry in _build_timeline(history, action_states)}
    assert all(entry["present"] for entry in timeline.values())
    assert timeline["recommendation"]["event_id"] == "e1"
    assert timeline["decision"]["event_id"] == "e2"
    # action 事件按 typed record 的 action_state 细分
    assert timeline["action_start"]["event_id"] == "e4"
    assert timeline["action_complete"]["event_id"] == "e5"
    assert timeline["outcome"]["event_id"] == "e6"
    assert timeline["effectiveness"]["event_id"] == "e7"
    # checksum 取事件的 payload_checksum
    assert timeline["outcome"]["checksum"] == "c6"
    assert timeline["effectiveness"]["sequence"] == 7


def test_build_timeline_non_started_completed_actions_occupy_no_stage():
    history = [
        _event(1, "recommendation_published", "r1"),
        _event(2, "confirmation", "cf1"),
        _event(3, "action", "a1"),
    ]
    for state in ("planned", "abandoned", "not_taken"):
        timeline = {entry["stage"]: entry for entry in _build_timeline(history, {"a1": state})}
        assert timeline["action_start"]["present"] is False
        assert timeline["action_complete"]["present"] is False
        assert timeline["recommendation"]["present"] is True
        assert timeline["decision"]["present"] is True


def test_build_timeline_unknown_event_type_ignored():
    history = [_event(1, "recommendation_published", "r1"), _event(2, "mystery", "x1")]
    timeline = {entry["stage"]: entry for entry in _build_timeline(history, {})}
    assert timeline["recommendation"]["present"] is True
    assert all(
        entry["present"] is False
        for stage, entry in timeline.items() if stage != "recommendation"
    )


# --- actions_recent.get -----------------------------------------------------


def test_actions_recent_envelope_and_shape():
    result = CockpitProjectionService().invoke("actions_recent.get")
    assert set(result) == ENVELOPE_KEYS
    assert result["schema_version"] == INTERFACE_SCHEMA_VERSION
    assert result["ok"] is True
    assert result["operation"] == "actions_recent.get"
    assert set(result["authorities"]) == {"decision"}
    data = result["data"]
    assert set(data) == {
        "total_available", "shown", "with_outcome", "awaiting_outcome", "items", "next_cursor",
    }
    assert data["shown"] == len(data["items"])
    assert data["shown"] <= 10
    assert data["shown"] <= data["total_available"]
    assert data["with_outcome"] + data["awaiting_outcome"] <= data["shown"]
    for item in data["items"]:
        assert ACTION_ITEM_KEYS <= set(item)
        assert tuple(entry["stage"] for entry in item["timeline"]) == TIMELINE_STAGES
        for entry in item["timeline"]:
            assert set(entry) == TIMELINE_ENTRY_KEYS
            if entry["present"]:
                assert entry["event_id"] is not None
                assert entry["sequence"] is not None
                assert entry["checksum"] is not None
            else:
                assert entry["event_id"] is None
                assert entry["sequence"] is None
                assert entry["checksum"] is None
        assert isinstance(item["outcomes"], list)
        assert isinstance(item["effectiveness"], list)


def test_actions_recent_real_chain_genesis_always_present():
    result = CockpitProjectionService().invoke("actions_recent.get")
    items = [item for item in result["data"]["items"] if "error" not in item]
    if not items:
        pytest.skip("库中无真实 recommendation")
    for item in items:
        # genesis(recommendation_published)由状态机强制存在
        timeline = {entry["stage"]: entry for entry in item["timeline"]}
        assert timeline["recommendation"]["present"] is True
        assert timeline["recommendation"]["sequence"] == 1
    # with_outcome 计数与 outcomes 明细一致
    data = result["data"]
    assert data["with_outcome"] == sum(1 for item in items if item["outcomes"])


def test_actions_recent_failure_degrades_to_zero_shape(monkeypatch):
    def boom(self, operation, **params):
        raise RuntimeError("boom")

    monkeypatch.setattr(DecisionFeedbackService, "invoke", boom)
    result = CockpitProjectionService().invoke("actions_recent.get")
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["authorities"]["decision"] == "error"
    assert any("decision" in item for item in result["limitations"])
    data = result["data"]
    assert data == {
        "total_available": 0, "shown": 0,
        "with_outcome": 0, "awaiting_outcome": 0, "items": [],
        "next_cursor": None,
    }


def test_actions_recent_single_item_failure_isolated(monkeypatch):
    original = DecisionFeedbackService.invoke

    def boom(self, operation, **params):
        if operation == "recommendations.history":
            raise RuntimeError("boom")
        return original(self, operation, **params)

    monkeypatch.setattr(DecisionFeedbackService, "invoke", boom)
    result = CockpitProjectionService().invoke("actions_recent.get")
    if not result["data"]["total_available"]:
        pytest.skip("库中无真实 recommendation")
    # 节本身成功(list 未失败),不标 partial
    assert result["ok"] is True
    assert result["partial"] is False
    assert result["authorities"]["decision"] == "ok"
    assert any("全链组装失败" in item for item in result["limitations"])
    for item in result["data"]["items"]:
        assert "error" in item
        # 失败条目仍保持完整形状,六阶段键恒在
        assert tuple(entry["stage"] for entry in item["timeline"]) == TIMELINE_STAGES
        assert all(entry["present"] is False for entry in item["timeline"])
        assert item["outcomes"] == []
        assert item["effectiveness"] == []


def test_actions_recent_single_item_failure_never_leaks_exception_detail(monkeypatch):
    original = DecisionFeedbackService.invoke

    def boom(self, operation, **params):
        if operation == "recommendations.history":
            raise RuntimeError(_POISON_MESSAGE)
        return original(self, operation, **params)

    monkeypatch.setattr(DecisionFeedbackService, "invoke", boom)
    result = CockpitProjectionService().invoke("actions_recent.get")
    if not result["data"]["total_available"]:
        pytest.skip("库中无真实 recommendation")
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    for fragment in _POISON_FRAGMENTS:
        assert fragment not in serialized


# --- proactive_summary.get ---------------------------------------------------


def test_proactive_summary_envelope_and_shape():
    result = CockpitProjectionService().invoke("proactive_summary.get")
    assert set(result) == ENVELOPE_KEYS
    assert result["schema_version"] == INTERFACE_SCHEMA_VERSION
    assert result["ok"] is True
    assert result["operation"] == "proactive_summary.get"
    assert set(result["authorities"]) == {"inbox", "metrics"}
    data = result["data"]
    assert set(data) == {"total_available", "groups", "metrics", "notes"}
    assert set(data["groups"]) == {"now", "deferrable"}
    assert isinstance(data["notes"], list) and data["notes"]
    assert any("controls/status" in note for note in data["notes"])
    if result["authorities"]["metrics"] == "ok":
        metrics = data["metrics"]
        # metrics.get 真实字段(读自 proactive/service.py metrics_get)
        for key in (
            "candidate_counts", "domain_counts", "evaluation_counts",
            "suppression_reason_counts", "feedback_counts",
            "control_frontier_checksum", "external_actions",
            "network_calls", "paid_calls",
        ):
            assert key in metrics


def test_proactive_summary_grouping_is_conservative():
    result = CockpitProjectionService().invoke("proactive_summary.get")
    if result["authorities"]["inbox"] != "ok":
        pytest.skip("inbox 节不可用")
    groups = result["data"]["groups"]
    total = len(groups["now"]) + len(groups["deferrable"])
    assert total <= result["data"]["total_available"]
    for card in groups["now"] + groups["deferrable"]:
        assert PROACTIVE_CARD_KEYS <= set(card)
    # now 组每条都必须有达阈值的真实 final_score;不达标/缺分一律 deferrable
    threshold = DEFAULT_RANKING_POLICY.threshold
    for card in groups["now"]:
        score = (card.get("importance") or {}).get("final_score")
        assert isinstance(score, (int, float)) and not isinstance(score, bool)
        assert score >= threshold
    for card in groups["deferrable"]:
        score = (card.get("importance") or {}).get("final_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            assert score < threshold


def test_proactive_summary_locks_final_score_vocabulary_end_to_end(monkeypatch):
    """Phase 36 task 3:通过完整 proactive_summary.get 管道(而非只测原始数据)证明
    缺失/非数值/bool 型 importance.final_score 一律保守归 deferrable,真实达标分
    才进入 now,并且降级会产生可枚举 limitation。"""
    threshold = DEFAULT_RANKING_POLICY.threshold
    synthetic_items = [
        {  # 缺失 importance 整体
            "candidate_id": "synthetic-missing-importance",
            "domains": ["project"], "candidate_class": "task", "presentation_kind": "card",
            "expires_at": None, "valid_from": None, "reason_codes": [],
            "current_control_eligible": True, "current_control_reason_codes": [],
        },
        {  # final_score 非数值
            "candidate_id": "synthetic-nonnumeric-score",
            "domains": ["project"], "candidate_class": "task", "presentation_kind": "card",
            "importance": {"final_score": "high"},
            "expires_at": None, "valid_from": None, "reason_codes": [],
            "current_control_eligible": True, "current_control_reason_codes": [],
        },
        {  # final_score 是 bool(isinstance(bool,int) 陷阱)
            "candidate_id": "synthetic-bool-score",
            "domains": ["project"], "candidate_class": "task", "presentation_kind": "card",
            "importance": {"final_score": True},
            "expires_at": None, "valid_from": None, "reason_codes": [],
            "current_control_eligible": True, "current_control_reason_codes": [],
        },
        {  # 真实达标分
            "candidate_id": "synthetic-real-now",
            "domains": ["project"], "candidate_class": "task", "presentation_kind": "card",
            "importance": {"final_score": threshold + 10.0},
            "expires_at": None, "valid_from": None, "reason_codes": [],
            "current_control_eligible": True, "current_control_reason_codes": [],
        },
    ]

    def fake_invoke(self, operation, **params):
        if operation == "inbox.list":
            return {
                "ok": True,
                "data": {"items": synthetic_items, "total_available": len(synthetic_items)},
            }
        if operation == "metrics.get":
            return {"ok": True, "data": {}}
        raise AssertionError(f"unexpected operation {operation}")

    monkeypatch.setattr(ProactiveIntelligenceService, "invoke", fake_invoke)
    result = CockpitProjectionService().invoke("proactive_summary.get")
    assert result["ok"] is True
    groups = result["data"]["groups"]
    now_ids = {card["candidate_id"] for card in groups["now"]}
    deferrable_ids = {card["candidate_id"] for card in groups["deferrable"]}
    assert now_ids == {"synthetic-real-now"}
    assert deferrable_ids == {
        "synthetic-missing-importance", "synthetic-nonnumeric-score", "synthetic-bool-score",
    }
    assert any("final_score" in item for item in result["limitations"])


def test_proactive_summary_inbox_failure_partial(monkeypatch):
    original = ProactiveIntelligenceService.invoke

    def boom(self, operation, **params):
        if operation == "inbox.list":
            raise RuntimeError("boom")
        return original(self, operation, **params)

    monkeypatch.setattr(ProactiveIntelligenceService, "invoke", boom)
    result = CockpitProjectionService().invoke("proactive_summary.get")
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["authorities"]["inbox"] == "error"
    assert any("inbox" in item for item in result["limitations"])
    # inbox 失败退化为空分组,metrics 节不受影响
    assert result["data"]["total_available"] == 0
    assert result["data"]["groups"] == {"now": [], "deferrable": []}
    assert result["authorities"]["metrics"] == "ok"
    assert isinstance(result["data"]["metrics"], dict)


def test_proactive_summary_projects_authority_control_history_and_as_of(monkeypatch):
    item = {
        "candidate_id": "candidate-with-history",
        "domains": ["project"],
        "candidate_class": "task",
        "presentation_kind": "card",
        "importance": {"final_score": 0.9},
        "expires_at": None,
        "valid_from": None,
        "reason_codes": ["deadline_approaching"],
        "current_control_eligible": True,
        "current_control_reason_codes": [],
    }

    def fake_invoke(self, operation, **params):
        if operation == "inbox.list":
            return {"ok": True, "data": {"items": [item], "total_available": 1}}
        if operation == "controls.status":
            return {"ok": True, "data": {
                "as_of": "9999-12-31T23:59:59Z",
                "frontier_checksum": "f" * 64,
                "history": [{"event_id": "evt-1", "sequence": 1, "operation": "suppress", "reason_code": "manual_review"}],
            }}
        if operation == "metrics.get":
            return {"ok": True, "data": {}}
        raise AssertionError(f"unexpected operation {operation}")

    monkeypatch.setattr(ProactiveIntelligenceService, "invoke", fake_invoke)
    result = CockpitProjectionService().invoke("proactive_summary.get")
    card = result["data"]["groups"]["now"][0]
    assert card["importance"]["final_score"] == 0.9
    assert card["control_as_of"] == "9999-12-31T23:59:59Z"
    assert card["control_history"][0]["operation"] == "suppress"
    assert card["control_frontier_checksum"] == "f" * 64


def test_proactive_summary_metrics_failure_partial(monkeypatch):
    original = ProactiveIntelligenceService.invoke

    def boom(self, operation, **params):
        if operation == "metrics.get":
            raise RuntimeError("boom")
        return original(self, operation, **params)

    monkeypatch.setattr(ProactiveIntelligenceService, "invoke", boom)
    result = CockpitProjectionService().invoke("proactive_summary.get")
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["authorities"]["metrics"] == "error"
    assert any("metrics" in item for item in result["limitations"])
    assert result["data"]["metrics"] is None
    assert result["authorities"]["inbox"] == "ok"
    assert set(result["data"]["groups"]) == {"now", "deferrable"}


# --- calibration_overview.get -------------------------------------------------


def test_calibration_overview_envelope_and_shape():
    result = CockpitProjectionService().invoke("calibration_overview.get")
    assert set(result) == ENVELOPE_KEYS
    assert result["schema_version"] == INTERFACE_SCHEMA_VERSION
    assert result["ok"] is True
    assert result["operation"] == "calibration_overview.get"
    assert set(result["authorities"]) == {"calibration"}
    data = result["data"]
    assert set(data) == {"total", "shown", "protocols"}
    assert data["shown"] == len(data["protocols"])
    assert data["shown"] <= 10
    assert data["shown"] <= data["total"]
    for item in data["protocols"]:
        assert PROTOCOL_ITEM_KEYS <= set(item)
        assert isinstance(item["inconclusive_reasons"], list)
        assert isinstance(item["summary_limitations"], list)
        assert isinstance(item["sample_size"], int)


def test_calibration_overview_real_protocol_fields():
    result = CockpitProjectionService().invoke("calibration_overview.get")
    items = [item for item in result["data"]["protocols"] if "error" not in item]
    if not items:
        pytest.skip("库中无真实 calibration protocol")
    for item in items:
        assert item["protocol_id"]
        assert item["status"] == "frozen"
        assert item["verdict"] in {"PASS", "FAIL", "INCONCLUSIVE", None}
        # explain 视图恒 causal_claim=False(读自 calibration/service.py)
        assert item["causal_claim"] is False
        assert item["sample_size"] >= 0
        assert item["summary_limitations"]


def test_calibration_overview_failure_degrades_to_zero_shape(monkeypatch):
    def boom(self, operation, **params):
        raise RuntimeError("boom")

    monkeypatch.setattr(DecisionIntelligenceReadService, "invoke", boom)
    result = CockpitProjectionService().invoke("calibration_overview.get")
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["authorities"]["calibration"] == "error"
    assert any("calibration" in item for item in result["limitations"])
    assert result["data"] == {"total": 0, "shown": 0, "protocols": []}


def test_calibration_overview_single_protocol_failure_isolated(monkeypatch):
    original = DecisionIntelligenceReadService.invoke

    def boom(self, operation, **params):
        if operation == "calibration.explain":
            raise RuntimeError("boom")
        return original(self, operation, **params)

    monkeypatch.setattr(DecisionIntelligenceReadService, "invoke", boom)
    result = CockpitProjectionService().invoke("calibration_overview.get")
    if not result["data"]["total"]:
        pytest.skip("库中无真实 calibration protocol")
    # 节本身成功(list 未失败),不标 partial
    assert result["ok"] is True
    assert result["partial"] is False
    assert result["authorities"]["calibration"] == "ok"
    assert any("explain 失败" in item for item in result["limitations"])
    for item in result["data"]["protocols"]:
        assert "error" in item
        assert PROTOCOL_ITEM_KEYS <= set(item)


def test_calibration_overview_single_protocol_failure_never_leaks_exception_detail(monkeypatch):
    original = DecisionIntelligenceReadService.invoke

    def boom(self, operation, **params):
        if operation == "calibration.explain":
            raise RuntimeError(_POISON_MESSAGE)
        return original(self, operation, **params)

    monkeypatch.setattr(DecisionIntelligenceReadService, "invoke", boom)
    result = CockpitProjectionService().invoke("calibration_overview.get")
    if not result["data"]["total"]:
        pytest.skip("库中无真实 calibration protocol")
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    for fragment in _POISON_FRAGMENTS:
        assert fragment not in serialized


# --- REST parity / 路由 --------------------------------------------------------


def _pop_generated_at(envelope):
    envelope.pop("generated_at")
    envelope["freshness"].pop("generated_at")


def test_rest_adapter_parity_phase39_operations():
    service = CockpitProjectionService()
    for operation in (
        "actions_recent.get", "proactive_summary.get", "calibration_overview.get",
    ):
        rest = ui_rest_contract(operation, {}, service=service)
        direct = service.invoke(operation)
        for envelope in (rest, direct):
            _pop_generated_at(envelope)
        assert rest == direct


@pytest.mark.skip(reason="FROZEN 2026-08-11: cockpit /ui/* 入口停用,该 HTTP 路由契约测试跳过;测试保留,取消冻结后恢复")
def test_ui_routes_serve_phase39_endpoints():
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # 本机请求不走任何 HTTP_PROXY 环境变量
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        port = server.server_address[1]
        for path, operation in (
            ("/ui/actions/recent", "actions_recent.get"),
            ("/ui/proactive/summary", "proactive_summary.get"),
            ("/ui/calibration/overview", "calibration_overview.get"),
        ):
            with opener.open(f"http://127.0.0.1:{port}{path}") as resp:
                body = json.loads(resp.read())
            assert resp.status == 200
            assert body["ok"] is True
            assert body["operation"] == operation
            assert body["schema_version"] == INTERFACE_SCHEMA_VERSION
    finally:
        server.shutdown()
        server.server_close()


# --- 39-01 稳定游标分页(newest-first + 不透明 cursor + fail-closed) ----------


def test_cursor_encode_decode_round_trip():
    enc = DecisionFeedbackService._encode_cursor("2026-01-02T03:04:05", "r-9")
    assert isinstance(enc, str) and enc
    assert DecisionFeedbackService._decode_cursor(enc) == ("2026-01-02T03:04:05", "r-9")


def test_cursor_decode_rejects_garbage():
    with pytest.raises(DecisionServiceError):
        DecisionFeedbackService._decode_cursor("!!!not-base64~~~")


def test_cursor_decode_rejects_valid_base64_wrong_format():
    # base64 可解码但字段数错误 → cursor_invalid,不得静默接受
    bad = base64.urlsafe_b64encode(b"onlyonefield").decode("ascii")
    with pytest.raises(DecisionServiceError):
        DecisionFeedbackService._decode_cursor(bad)


def test_recommendations_list_rejects_invalid_cursor_fail_closed():
    # 解码发生在打开 DB 之前;非法 cursor 必须 fail-closed,不得静默丢弃或降级
    svc = DecisionFeedbackService(UNIFIED_DB)
    result = svc.invoke("recommendations.list", cursor="@@@invalid@@@")
    assert result["ok"] is False
    assert result["error"]["code"] == "cursor_invalid"


def _synthetic_recommendations_db(path: Path, n: int) -> None:
    import sqlite3 as _sq

    con = _sq.connect(path)
    con.execute("CREATE TABLE decision_recommendations (recommendation_id TEXT, created_at TEXT)")
    for i in range(n):
        rid = f"r{i:02d}"
        ts = f"2026-01-01T00:{i:02d}:00"  # i 越大越新
        con.execute(
            "INSERT INTO decision_recommendations (recommendation_id, created_at) VALUES (?, ?)",
            (rid, ts),
        )
    con.commit()
    con.close()


class _FakeState:
    confirmation_state = "confirmed"
    action_state = "completed"
    events = [type("E", (), {"sequence": 5})()]


def _fake_context(self, con, recommendation_id):
    rec = {
        "recommendation_id": recommendation_id, "run_id": "run1", "subject": "s",
        "domain": "d", "scope": "sc", "recommendation_kind": "k", "horizon": "h",
        "confidence": 0.5, "uncertainty": "low", "expires_at": "2099-01-01T00:00:00",
        "payload_json": "{}", "payload_checksum": "x", "recommendation_checksum": "x",
        "snapshot_id": "snap1", "snapshot_hash": "h1", "policy_id": "p1", "policy_version": "v1",
        "source_run_id": "run1", "source_run_checksum": "c1", "source_publication_sequence": 1,
    }
    run = {
        "run_id": "run1", "run_checksum": "rc", "source_run_id": "run1",
        "source_run_checksum": "c1", "source_publication_sequence": 1, "snapshot_id": "snap1",
        "snapshot_hash": "h1", "policy_id": "p1", "policy_version": "v1",
        "input_manifest_checksum": "imc",
    }
    return rec, run, {"payload": {}, "state": _FakeState()}


def test_recommendations_list_newest_first_with_cursor_pagination(monkeypatch):
    monkeypatch.setattr(DecisionFeedbackService, "_context", _fake_context)
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "rec.sqlite"
        _synthetic_recommendations_db(db, 12)
        svc = DecisionFeedbackService(db)
        page1 = svc.invoke("recommendations.list", limit=10)
        assert page1["ok"] is True
        items1 = page1["data"]["items"]
        assert len(items1) == 10
        # newest-first:首项最新(r11),末项最旧窗口边界(r02)
        assert [it["recommendation_id"] for it in items1][0] == "r11"
        assert [it["recommendation_id"] for it in items1][-1] == "r02"
        assert page1["data"]["total_available"] == 12
        assert page1["data"]["next_cursor"] is not None
        # 翻页:严格更旧,且与第一页无重叠、并集为全集
        page2 = svc.invoke("recommendations.list", limit=10, cursor=page1["data"]["next_cursor"])
        assert page2["ok"] is True
        items2 = page2["data"]["items"]
        assert len(items2) == 2
        assert [it["recommendation_id"] for it in items2] == ["r01", "r00"]
        assert page2["data"]["next_cursor"] is None
        assert page2["data"]["total_available"] == 12
        page1_ids = {it["recommendation_id"] for it in items1}
        page2_ids = {it["recommendation_id"] for it in items2}
        assert page1_ids.isdisjoint(page2_ids)
        assert page1_ids | page2_ids == {f"r{i:02d}" for i in range(12)}


def test_actions_recent_forwards_cursor_to_service(monkeypatch):
    # 验证 REST → projection → service 的 cursor 透传链路
    captured: dict = {}
    original = DecisionFeedbackService.invoke

    def spy(self, operation, **params):
        if operation == "recommendations.list":
            captured.update(params)
            return {
                "schema_version": INTERFACE_SCHEMA_VERSION, "operation": operation,
                "ok": True, "status": "success",
                "data": {"items": [], "next_cursor": None},
                "total_available": 0, "limit": 10,
                "privacy": {"metadata_only": True, "private_bodies": 0},
            }
        return original(self, operation, **params)

    monkeypatch.setattr(DecisionFeedbackService, "invoke", spy)
    CockpitProjectionService().invoke("actions_recent.get", cursor="CUR123", limit=5)
    assert captured.get("cursor") == "CUR123"
    assert captured.get("limit") == 5
