from __future__ import annotations

import json

import pytest

from personal_knowledge.services.agent_contract import DEFAULT_BUDGET_BYTES, compact_envelope


def test_success_contains_compact_navigation_and_evidence_links() -> None:
    result = compact_envelope({
        "operation": "analysis.get", "ok": True,
        "data": {
            "run_id": "dar_test", "run_checksum": "a" * 64,
            "candidate_id": "dac_test", "candidate_checksum": "b" * 64,
        },
    })
    assert result["schema_version"] == "agent_compact_envelope_v1"
    assert result["ids"] == ["dar_test", "dac_test"]
    assert result["next_actions"] == [{"operation": "analysis.explain", "requires": ["stable_id"]}]
    assert result["evidence_links"][0] == {
        "authority": "analysis", "record_type": "run", "record_id": "dar_test",
        "checksum": "a" * 64, "drill_down": "analysis.get",
    }
    assert result["truncated"] is False


def test_large_and_sensitive_data_is_omitted_with_hard_budget() -> None:
    result = compact_envelope({
        "operation": "analysis.get", "ok": True,
        "data": {
            "run_id": "dar_large", "provider_body": "secret-provider-text",
            "confirmation_token": "bearer-capability", "content_rich": "private" * 20_000,
            "safe_large": "x" * 30_000,
        },
    })
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= DEFAULT_BUDGET_BYTES
    assert result["truncated"] is True and result["data"] is None
    assert "secret-provider-text" not in encoded.decode()
    assert "bearer-capability" not in encoded.decode()


@pytest.mark.parametrize(
    ("code", "category", "retryable", "action"),
    [
        ("run_not_found", "not_found", False, "verify_id"),
        ("idempotency_conflict", "conflict", False, "resume_session"),
        ("stale_expected_sequence", "stale", True, "resume_session"),
        ("confirmation_expired", "confirmation", True, "confirm_again"),
        ("illegal_transition", "sequence", True, "prepare_fresh_preview"),
        ("domain_not_allowed", "risk", False, "reduce_scope"),
        ("event_chain_invalid", "integrity", False, "inspect_authority"),
        ("database_missing", "runtime", True, "check_runtime"),
        ("provider_outcome_unknown", "unknown_outcome", False, "manual_review"),
    ],
)
def test_error_taxonomy_is_stable_and_recovery_is_allowlisted(code, category, retryable, action) -> None:
    result = compact_envelope({"operation": "session.execute", "ok": False, "error": {"code": code}})
    assert result["error"]["category"] == category
    assert result["error"]["retryable"] is retryable
    assert action in result["error"]["recovery_actions"]
    assert "traceback" not in json.dumps(result).lower()
