from __future__ import annotations

from personal_knowledge.services.api_server import agent_read_rest_contract
from personal_knowledge.services.mcp_server import agent_read_tool_contract


class StubReadService:
    @staticmethod
    def invoke(operation, **_params):
        if operation == "analysis.get":
            return {
                "schema_version": "decision_intelligence_read_v1", "operation": operation,
                "ok": True, "status": "success",
                "data": {"run_id": "dar_contract", "run_checksum": "a" * 64, "provider_body": "must-not-surface"},
            }
        return {
            "schema_version": "decision_intelligence_read_v1", "operation": operation,
            "ok": False, "status": "error", "error": {"code": "run_not_found", "detail": "unsafe internal path"},
        }

    @staticmethod
    def _error(operation, code, detail=""):
        return {"operation": operation, "ok": False, "error": {"code": code, "detail": detail}}


def test_rest_and_stdio_return_identical_compact_success() -> None:
    service = StubReadService()
    rest = agent_read_rest_contract("analysis.get", {"run_id": "dar_contract"}, service=service)
    mcp = agent_read_tool_contract("decision_analysis_get", {"run_id": "dar_contract"}, service=service)
    assert rest == mcp
    assert rest["summary"] and rest["ids"] == ["dar_contract"]
    assert rest["evidence_links"][0]["checksum"] == "a" * 64
    assert "must-not-surface" not in str(rest)


def test_typed_failure_has_safe_recovery_contract() -> None:
    service = StubReadService()
    rest = agent_read_rest_contract("analysis.list", {}, service=service)
    mcp = agent_read_tool_contract("decision_analysis_list", {}, service=service)
    assert rest == mcp
    assert rest["error"] == {
        "code": "run_not_found", "category": "not_found",
        "message": "The requested record was not found.", "retryable": False,
        "recovery_actions": ["verify_id", "list_available"],
    }
    assert "unsafe internal path" not in str(rest)
