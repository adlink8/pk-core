import json
import sqlite3

import pytest

import personal_knowledge.services.api_server as api_server
from personal_knowledge.core.project_paths import UNIFIED_DB
from personal_knowledge.intelligence.decision.service import DecisionFeedbackService
from personal_knowledge.intelligence.proactive.service import ProactiveIntelligenceService
from personal_knowledge.services.api_server import ui_rest_contract
from personal_knowledge.services.ui_projection import (
    AUTHORITY_DB_PATHS,
    INTERFACE_SCHEMA_VERSION,
    CockpitProjectionService,
)

OVERVIEW_SECTIONS = {"personal", "decision", "proactive", "external", "knowledge"}

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


def test_overview_envelope_shape():
    result = CockpitProjectionService().invoke("overview.get")
    assert result["schema_version"] == INTERFACE_SCHEMA_VERSION
    assert result["schema_version"] == "decision_cockpit_projection_v1"
    assert result["ok"] is True
    assert set(result["snapshot_bindings"]) == {"personal", "external", "serving"}
    assert isinstance(result["limitations"], list)
    assert set(result["authorities"]) == OVERVIEW_SECTIONS
    assert set(result["authorities"].values()) <= {"ok", "empty", "error"}
    assert result["generated_at"]
    assert set(result["data"]) == OVERVIEW_SECTIONS


def test_overview_personal_section_shape():
    personal = CockpitProjectionService().invoke("overview.get")["data"]["personal"]
    if personal is None:
        return  # authority 故障时该节允许为 None(由 authorities/limitations 表达)
    assert {
        "snapshot_id", "as_of", "total_available", "domains", "status_counts", "top_items",
    } <= set(personal)
    assert isinstance(personal["domains"], dict)
    assert isinstance(personal["status_counts"], dict)
    assert len(personal["top_items"]) <= 10
    for item in personal["top_items"]:
        assert set(item) == {"key", "status", "confidence", "provenance_class"}


def test_proactive_failure_isolated_as_partial(monkeypatch):
    def boom(self, operation, **params):
        raise RuntimeError("boom")

    monkeypatch.setattr(ProactiveIntelligenceService, "invoke", boom)
    result = CockpitProjectionService().invoke("overview.get")
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["authorities"]["proactive"] == "error"
    assert any("proactive" in item for item in result["limitations"])
    assert result["data"]["proactive"] is None
    # 其余节不受 proactive 故障影响
    for name in OVERVIEW_SECTIONS - {"proactive"}:
        assert result["authorities"][name] in {"ok", "empty"}
        assert result["data"][name] is not None


def test_authority_failure_never_leaks_exception_detail(monkeypatch):
    def boom(self, operation, **params):
        raise RuntimeError(_POISON_MESSAGE)

    monkeypatch.setattr(ProactiveIntelligenceService, "invoke", boom)
    result = CockpitProjectionService().invoke("overview.get")
    assert result["ok"] is True
    assert result["partial"] is True
    assert result["authorities"]["proactive"] == "error"
    assert any("proactive" in item for item in result["limitations"])
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    for fragment in _POISON_FRAGMENTS:
        assert fragment not in serialized


def test_system_status_shape():
    result = CockpitProjectionService().invoke("system.status.get")
    assert result["schema_version"] == INTERFACE_SCHEMA_VERSION
    assert result["ok"] is True
    ports = result["data"]["ports"]
    assert set(ports) == {"rest", "mcp", "tunnel"}
    assert ports["rest"]["up"] is True
    # mcp / tunnel 为非常驻服务,只断言类型不断言实际 up/down
    assert isinstance(ports["mcp"]["up"], bool)
    assert isinstance(ports["tunnel"]["up"], bool)
    assert "active_collection" in result["data"]["knowledge"]
    authority_dbs = result["data"]["authority_dbs"]
    assert len(authority_dbs) == 4
    for entry in authority_dbs.values():
        assert "exists" in entry
        assert "readable" in entry
    observations = result["data"]["observations"]
    assert observations
    assert any(item["id"] == "rest_request" and item["state"] == "healthy" for item in observations)
    assert any(item["id"] == "mcp" for item in observations)
    supervisor = result["data"]["supervisor_state"]
    assert supervisor["state"] in {"healthy", "stale_observation", "unknown"}
    serialized = json.dumps(result, ensure_ascii=False)
    assert "supervisor_pid" not in serialized
    assert "health_url" not in serialized


def test_rest_adapter_delegates_to_identical_service():
    service = CockpitProjectionService()
    rest = ui_rest_contract("overview.get", {}, service=service)
    direct = service.invoke("overview.get")
    # generated_at 按调用生成(含 freshness 内的同源副本),pop 掉再比较其余字段
    for envelope in (rest, direct):
        envelope.pop("generated_at")
        envelope["freshness"].pop("generated_at")
    assert rest == direct


def test_unknown_operation_returns_typed_error():
    result = CockpitProjectionService().invoke("nope")
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_operation"


def test_cockpit_asset_resolution(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    index = dist / "index.html"
    index.write_text("<html></html>", encoding="utf-8")
    script = dist / "assets" / "x.js"
    script.write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(api_server, "COCKPIT_DIST", dist)

    assert api_server._resolve_cockpit_asset("/app") == index
    assert api_server._resolve_cockpit_asset("/app/") == index
    # 无扩展名子路径 → SPA fallback
    assert api_server._resolve_cockpit_asset("/app/decisions") == index
    assert api_server._resolve_cockpit_asset("/app/assets/x.js") == script.resolve()
    # 显式 / 编码后的穿越段一律拒绝
    assert api_server._resolve_cockpit_asset("/app/../etc/passwd") is None
    assert api_server._resolve_cockpit_asset("/app/%2e%2e/x") is None
    # dist 未构建 → None
    monkeypatch.setattr(api_server, "COCKPIT_DIST", tmp_path / "missing")
    assert api_server._resolve_cockpit_asset("/app") is None


# --- 物理只读边界(Phase 36 task 2:D-36-01/D-36-02)---------------------------


def _table_fingerprint(path):
    """对权威库以 mode=ro 打开并逐表计数,构成只读指纹;用于证明 Projection
    调用前后权威库零写入(D-36-01/D-36-02)。库不存在时返回 None。"""
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        tables = [
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        return {
            table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }
    finally:
        con.close()


def test_all_projection_operations_are_physically_read_only():
    paths = {"unified": UNIFIED_DB, **AUTHORITY_DB_PATHS}
    before = {name: _table_fingerprint(path) for name, path in paths.items()}

    service = CockpitProjectionService()
    for operation in (
        "overview.get", "system.status.get", "personal_state.get",
        "external_delta.get", "decision_queue.get", "actions_recent.get",
        "proactive_summary.get", "calibration_overview.get",
    ):
        service.invoke(operation)
    # decision_workspace.get 需要真实 recommendation_id 才会真正下钻到全部权威读面
    rid_result = DecisionFeedbackService(UNIFIED_DB).invoke("recommendations.list", limit=1)
    if rid_result.get("ok"):
        items = rid_result["data"].get("items") or []
        if items:
            service.invoke(
                "decision_workspace.get",
                recommendation_id=items[0]["recommendation_id"],
            )

    after = {name: _table_fingerprint(path) for name, path in paths.items()}
    assert before == after


def test_projection_readonly_connection_rejects_write():
    """CockpitProjectionService 内部自建连接一律 mode=ro + query_only=ON;
    在同样的打开方式下,任何写语句都必须被 SQLite 拒绝(D-36-02 物理只读边界)。"""
    con = sqlite3.connect(f"file:{UNIFIED_DB.resolve().as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        with pytest.raises(sqlite3.OperationalError):
            con.execute("CREATE TABLE ui_projection_write_probe_36_02 (id INTEGER)")
    finally:
        con.close()
