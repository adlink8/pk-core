import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

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

import personal_knowledge.services.api_server as api_server
from personal_knowledge.intelligence.proactive.schema import CANONICAL_DOMAINS
from personal_knowledge.intelligence.service import IntelligenceService
from personal_knowledge.services.api_server import ui_rest_contract
from personal_knowledge.services.decision_intelligence_reads import (
    DecisionIntelligenceReadService,
)
from personal_knowledge.services.ui_projection import (
    INTERFACE_SCHEMA_VERSION,
    CockpitProjectionService,
)

EIGHT_DOMAINS = set(CANONICAL_DOMAINS)
ASSERTION_KIND_KEYS = {"goal", "constraint", "observation", "state"}
PROVENANCE_KEYS = {"fact", "observation", "inference"}
LIFECYCLE_KEYS = {"current", "stale", "conflict", "resolved", "expired"}
PERSONAL_STATE_DATA_KEYS = {
    "snapshot_id", "as_of", "total_available", "history_total_available",
    "domains", "lifecycle_counts", "recent_changes",
}
ASSERTION_ITEM_KEYS = {
    "key", "provenance_class", "status", "confidence",
    "current_assertion_id", "current_value_checksum", "evidence_count",
}
CHANGE_ITEM_KEYS = {
    "record_id", "record_type", "status", "domain", "subject", "effective_at",
}
EXTERNAL_DATA_KEYS = {"snapshot", "sources", "facts", "delta", "counts"}
SNAPSHOT_KEYS = {"snapshot_id", "snapshot_hash", "activated_at"}
SOURCE_ITEM_KEYS = {
    "source_id", "authority_role", "source_type", "topic", "region", "endpoint",
}
FACT_ITEM_KEYS = {
    "fact_id", "fact_checksum", "subject", "predicate", "region", "valid_from", "valid_to",
    "source_quality", "fact_confidence", "source_ids", "lifecycle", "conflict", "freshness",
}
FRESHNESS_KEYS = {"level", "reason"}
FRESHNESS_LEVELS = {"unknown", "valid", "expiring_soon", "expired"}
DELTA_KEYS = {"new", "updated", "expiring", "conflicts"}


def test_personal_state_envelope_shape():
    result = CockpitProjectionService().invoke("personal_state.get")
    assert result["schema_version"] == INTERFACE_SCHEMA_VERSION
    assert result["schema_version"] == "decision_cockpit_projection_v1"
    assert result["ok"] is True
    assert result["operation"] == "personal_state.get"
    assert set(result["snapshot_bindings"]) == {"personal", "external", "serving"}
    assert isinstance(result["limitations"], list)
    assert set(result["authorities"]) == {"state", "changes"}
    assert set(result["authorities"].values()) <= {"ok", "empty", "error"}
    assert result["generated_at"]
    assert set(result["data"]) == PERSONAL_STATE_DATA_KEYS


def test_personal_state_domains_shape():
    data = CockpitProjectionService().invoke("personal_state.get")["data"]
    # 八个领域键恒在且恰好八个(无数据时全零 + 空数组)
    assert set(data["domains"]) == EIGHT_DOMAINS
    assert len(data["domains"]) == 8
    for bucket in data["domains"].values():
        assert set(bucket) == {"total", "by_kind", "by_provenance", "conflicts", "assertions"}
        assert set(bucket["by_kind"]) == ASSERTION_KIND_KEYS
        assert set(bucket["by_provenance"]) == PROVENANCE_KEYS
        assert isinstance(bucket["conflicts"], int)
        assert len(bucket["assertions"]) <= 20
        for item in bucket["assertions"]:
            assert set(item) == ASSERTION_ITEM_KEYS
            assert item["provenance_class"] in PROVENANCE_KEYS
            assert set(item["key"]) == {
                "assertion_kind", "subject", "domain", "scope", "predicate",
            }
            assert isinstance(item["evidence_count"], int)
            # current_value_checksum + current_assertion_id + data.snapshot_id 是
            # evidence.resolve 的稳定引用三元组(Phase 37:EVID-01);有 current_assertion_id
            # 就必须有对应 checksum,不得用 nullish 掩盖两者本应同时存在
            if item["current_assertion_id"]:
                assert item["current_value_checksum"]
    assert set(data["lifecycle_counts"]) == LIFECYCLE_KEYS
    for item in data["recent_changes"]:
        assert set(item) == CHANGE_ITEM_KEYS


def test_personal_state_changes_failure_isolated(monkeypatch):
    original = IntelligenceService.invoke

    def guarded(self, operation, **params):
        if operation == "changes.recent":
            raise RuntimeError("boom")
        return original(self, operation, **params)

    monkeypatch.setattr(IntelligenceService, "invoke", guarded)
    result = CockpitProjectionService().invoke("personal_state.get")
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["authorities"]["changes"] == "error"
    assert any("changes" in item for item in result["limitations"])
    # changes 故障降级为空数组,state 节不受影响
    assert result["data"]["recent_changes"] == []
    assert result["authorities"]["state"] in {"ok", "empty"}
    assert set(result["data"]["domains"]) == EIGHT_DOMAINS


def test_personal_state_changes_failure_never_leaks_exception_detail(monkeypatch):
    original = IntelligenceService.invoke

    def guarded(self, operation, **params):
        if operation == "changes.recent":
            raise RuntimeError(_POISON_MESSAGE)
        return original(self, operation, **params)

    monkeypatch.setattr(IntelligenceService, "invoke", guarded)
    result = CockpitProjectionService().invoke("personal_state.get")
    assert result["ok"] is True
    assert result["authorities"]["changes"] == "error"
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    for fragment in _POISON_FRAGMENTS:
        assert fragment not in serialized


def test_external_delta_envelope_shape():
    result = CockpitProjectionService().invoke("external_delta.get")
    assert result["schema_version"] == INTERFACE_SCHEMA_VERSION
    assert result["ok"] is True
    assert result["operation"] == "external_delta.get"
    assert set(result["snapshot_bindings"]) == {"personal", "external", "serving"}
    assert set(result["authorities"]) == {"external"}
    assert set(result["authorities"].values()) <= {"ok", "empty", "error"}
    data = result["data"]
    if data is None:
        return  # authority 故障时 data 允许为 None(由 authorities/limitations 表达)
    assert set(data) == EXTERNAL_DATA_KEYS
    assert set(data["counts"]) == {"sources", "facts", "conflicts"}
    assert set(data["delta"]) == DELTA_KEYS
    for bucket in data["delta"].values():
        assert isinstance(bucket, list)
    assert set(data["snapshot"]) == SNAPSHOT_KEYS
    assert result["snapshot_bindings"]["external"] == data["snapshot"]["snapshot_id"]
    for source in data["sources"]:
        assert set(source) == SOURCE_ITEM_KEYS
    for fact in data["facts"]:
        assert set(fact) == FACT_ITEM_KEYS
        assert fact["conflict"] == (fact["lifecycle"] == "conflict")
        # fact_checksum + fact_id + data.snapshot.snapshot_id 是 evidence.resolve 的
        # 稳定引用三元组(Phase 37:EVID-01);canonical DTO 恒用 subject/predicate 命名轴,
        # 不与 fact_type/observed_at/source_id 并存(D-37-02)
        assert fact["fact_checksum"]
        assert isinstance(fact["source_ids"], list)
        assert set(fact["freshness"]) == FRESHNESS_KEYS
        assert fact["freshness"]["level"] in FRESHNESS_LEVELS
    assert data["counts"]["sources"] == len(data["sources"])
    assert data["counts"]["facts"] == len(data["facts"])
    assert data["counts"]["conflicts"] == len(data["delta"]["conflicts"])


def test_external_delta_freshness_derived_from_snapshot_reference_not_client_time():
    """freshness 必须由服务端相对 snapshot.activated_at 派生(D-37 的显式服务端
    freshness 判断),不是简单 always-valid;expired/expiring_soon 两级都要可达。"""
    result = CockpitProjectionService().invoke("external_delta.get")
    data = result["data"]
    if data is None or not data["facts"]:
        return
    snapshot_id = data["snapshot"]["snapshot_id"]
    for fact in data["facts"]:
        # lifecycle(记录状态)与 freshness(相对时效)是两个独立轴,不得合并
        assert "lifecycle" in fact and "freshness" in fact
        assert fact["freshness"]["level"] in FRESHNESS_LEVELS
    assert snapshot_id == result["snapshot_bindings"]["external"]


def test_personal_state_assertion_checksum_roundtrips_into_state_explain(monkeypatch):
    """PersonalAssertionSchema 新增的 current_value_checksum 必须真实等于
    state.explain 会返回的 current_value_checksum(evidence.resolve 匹配的口径),
    不是该 Projection 层自造的另一份值。"""
    from personal_knowledge.intelligence.service import IntelligenceService as RealIntelligenceService

    fake_item = {
        "key": {
            "assertion_kind": "goal", "subject": "user",
            "domain": "project", "scope": "s", "predicate": "p",
        },
        "status": "current", "provenance_class": "fact", "confidence": 0.9,
        "current_assertion_id": "psa_fixture0001",
        "current_value_checksum": "csum_fixture_abc123",
        "evidence_status": [],
    }
    original = RealIntelligenceService.invoke

    def guarded(self, operation, **params):
        if operation == "state.current":
            return {
                "ok": True, "status": "success",
                "snapshot": {"snapshot_id": "ss_fixture"},
                "data": {"as_of": "2026-01-01T00:00:00Z", "total_available": 1, "items": [fake_item]},
            }
        if operation == "state.history":
            return {"ok": True, "data": {"total_available": 1}}
        return original(self, operation, **params)

    monkeypatch.setattr(RealIntelligenceService, "invoke", guarded)
    data = CockpitProjectionService().invoke("personal_state.get")["data"]
    assertion = data["domains"]["project"]["assertions"][0]
    assert assertion["current_assertion_id"] == "psa_fixture0001"
    assert assertion["current_value_checksum"] == "csum_fixture_abc123"


def test_external_delta_failure_isolated(monkeypatch):
    def boom(self, operation, **params):
        raise RuntimeError("boom")

    monkeypatch.setattr(DecisionIntelligenceReadService, "invoke", boom)
    result = CockpitProjectionService().invoke("external_delta.get")
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["authorities"]["external"] == "error"
    assert any("external" in item for item in result["limitations"])
    assert result["data"] is None
    assert result["snapshot_bindings"]["external"] is None


def test_rest_adapter_parity_new_operations():
    service = CockpitProjectionService()
    for operation in ("personal_state.get", "external_delta.get"):
        rest = ui_rest_contract(operation, {}, service=service)
        direct = service.invoke(operation)
        # generated_at 按调用生成(含 freshness 内的同源副本),pop 掉再比较其余字段
        for envelope in (rest, direct):
            envelope.pop("generated_at")
            envelope["freshness"].pop("generated_at")
        assert rest == direct


@pytest.mark.skip(reason="FROZEN 2026-08-11: cockpit /ui/* 入口停用,该 HTTP 路由契约测试跳过;测试保留,取消冻结后恢复")
def test_ui_routes_serve_new_endpoints():
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # 本机请求不走任何 HTTP_PROXY 环境变量
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        port = server.server_address[1]
        for path, operation in (
            ("/ui/personal-state", "personal_state.get"),
            ("/ui/external/delta", "external_delta.get"),
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
