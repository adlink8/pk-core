from personal_knowledge.services.api_server import agent_read_rest_contract
from personal_knowledge.services.decision_intelligence_reads import DecisionIntelligenceReadService
from personal_knowledge.services.mcp_server import agent_read_tool_contract


TOOL_OPERATION = {
    "external_context_list": "external.list",
    "decision_analysis_list": "analysis.list",
    "project_pilot_list": "pilot.list",
    "recommendation_calibration_list": "calibration.list",
}


def test_rest_and_stdio_mcp_delegate_to_identical_shared_contract():
    service = DecisionIntelligenceReadService()
    for tool, operation in TOOL_OPERATION.items():
        rest = agent_read_rest_contract(operation, {"limit": "5"}, service=service)
        mcp = agent_read_tool_contract(tool, {"limit": 5}, service=service)
        assert rest == mcp
        assert rest["ok"] is True


def test_detail_and_explain_transport_parity_for_live_cohort():
    service = DecisionIntelligenceReadService()
    analysis = service.invoke("analysis.list", limit=1)["data"]["items"][0]["run_id"]
    pilot = service.invoke("pilot.list", limit=1)["data"]["items"][0]["case_id"]
    calibration = service.invoke("calibration.list", limit=1)["data"]["items"][0]["protocol_id"]

    cases = [
        ("analysis.get", "decision_analysis_get", {"run_id": analysis}),
        ("pilot.explain", "project_pilot_explain", {"case_id": pilot}),
        ("calibration.explain", "recommendation_calibration_explain", {"protocol_id": calibration}),
    ]
    for operation, tool, args in cases:
        assert agent_read_rest_contract(operation, args, service=service) == agent_read_tool_contract(tool, args, service=service)


def test_limits_and_unknown_tools_return_typed_errors():
    service = DecisionIntelligenceReadService()
    rest = agent_read_rest_contract("analysis.list", {"limit": "0"}, service=service)
    mcp = agent_read_tool_contract("decision_analysis_list", {"limit": 0}, service=service)
    assert rest == mcp
    assert rest["error"]["code"] == "invalid_limit"
    assert agent_read_tool_contract("agent_write", {}, service=service)["error"]["code"] == "unknown_operation"
