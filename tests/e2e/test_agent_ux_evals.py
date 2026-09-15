from __future__ import annotations

import json

import pytest

from personal_knowledge.services.agent_contract import compact_envelope


@pytest.mark.parametrize(
    ("operation", "data", "expected_operation"),
    [
        ("analysis.list", {"items": [{"run_id": "dar_1"}]}, "analysis.get"),
        ("analysis.get", {"run_id": "dar_1"}, "analysis.explain"),
        ("pilot.list", {"items": [{"case_id": "ppc_1"}]}, "pilot.get"),
        ("calibration.get", {"protocol_id": "calp_1"}, "calibration.explain"),
        ("session.resume", {"session_id": "ors_1", "state": "generated", "next_operation": "publish"}, "session.preview"),
    ],
)
def test_success_tool_selection_eval(operation, data, expected_operation) -> None:
    result = compact_envelope({"operation": operation, "ok": True, "data": data})
    assert result["next_actions"][0]["operation"] == expected_operation
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 16 * 1024


@pytest.mark.parametrize(
    ("code", "expected_action", "retryable"),
    [
        ("run_not_found", "list_available", False),
        ("stale_expected_sequence", "resume_session", True),
        ("confirmation_expired", "prepare_fresh_preview", True),
        ("illegal_transition", "resume_session", True),
        ("domain_not_allowed", "manual_review", False),
        ("provider_outcome_unknown", "inspect_provider_reservation", False),
        ("event_chain_invalid", "inspect_authority", False),
    ],
)
def test_failure_recovery_eval(code, expected_action, retryable) -> None:
    result = compact_envelope({
        "operation": "session.execute", "ok": False,
        "error": {"code": code, "detail": "internal detail must not guide recovery"},
    })
    assert expected_action in result["error"]["recovery_actions"]
    assert result["error"]["retryable"] is retryable
    assert "internal detail" not in json.dumps(result)
    assert not ({"bypass_confirmation", "retry_provider", "auto_promote"} & set(result["error"]["recovery_actions"]))
