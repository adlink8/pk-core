"""Compact, privacy-bounded success and error envelopes for Agent transports."""
from __future__ import annotations

import json
from typing import Any, Mapping


SCHEMA_VERSION = "agent_compact_envelope_v1"
DEFAULT_BUDGET_BYTES = 16 * 1024
SENSITIVE_KEYS = frozenset({
    "confirmation_token", "token", "secret", "credentials", "provider_body",
    "request_body", "response_body", "raw_evidence", "content_rich", "password",
})
ID_KEYS = frozenset({
    "source_id", "snapshot_id", "fact_id", "run_id", "candidate_id", "claim_id",
    "case_id", "recommendation_id", "protocol_id", "proposal_id", "session_id", "event_id",
})


ERROR_CATALOG = {
    "not_found": (False, ("verify_id", "list_available"), "The requested record was not found."),
    "conflict": (False, ("resume_session", "use_original_idempotency_key", "manual_review"), "The request conflicts with an existing immutable record."),
    "stale": (True, ("resume_session", "prepare_fresh_preview"), "The request is stale; inspect current state before preparing a new preview."),
    "confirmation": (True, ("resume_session", "prepare_fresh_preview", "confirm_again"), "The explicit confirmation is missing, expired, consumed, or does not match this preview."),
    "sequence": (True, ("resume_session", "prepare_fresh_preview"), "The expected sequence or transition does not match current state."),
    "risk": (False, ("reduce_scope", "manual_review"), "The requested scope is outside the allowed low-risk project boundary."),
    "integrity": (False, ("inspect_authority", "manual_review"), "Authority integrity validation failed."),
    "runtime": (True, ("check_runtime", "retry_when_ready"), "A required local runtime component is unavailable."),
    "unknown_outcome": (False, ("resume_session", "inspect_provider_reservation", "manual_review"), "Provider outcome is unknown; automatic retry is unsafe."),
}


def _category(code: str) -> str:
    value = code.lower()
    if value == "provider_outcome_unknown":
        return "unknown_outcome"
    if any(term in value for term in ("database_missing", "service_unavailable", "runtime", "schema_not_applied", "confirmation_secret_unavailable")):
        return "runtime"
    if any(term in value for term in ("not_found", "_missing", "missing_")):
        return "not_found"
    if any(term in value for term in ("idempotency", "conflict", "already_")):
        return "conflict"
    if "stale" in value:
        return "stale"
    if any(term in value for term in ("confirmation", "explicit_confirm")):
        return "confirmation"
    if any(term in value for term in ("sequence", "illegal_transition", "state_drift")):
        return "sequence"
    if any(term in value for term in ("risk", "domain", "external_action", "forbidden")):
        return "risk"
    if any(term in value for term in ("checksum", "integrity", "tamper", "drift", "chain_invalid", "binding_hash")):
        return "integrity"
    return "runtime"


def _safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _safe(item)
            for key, item in value.items()
            if str(key).lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def _collect_ids(value: Any, result: list[str]) -> None:
    if len(result) >= 100:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in ID_KEYS and isinstance(item, str) and item and item not in result:
                result.append(item)
            _collect_ids(item, result)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_ids(item, result)


def _authority(operation: str, key: str) -> str:
    if operation.startswith("external.") or key in {"source_id", "fact_id", "snapshot_id"}:
        return "external"
    if operation.startswith("analysis.") or key in {"run_id", "candidate_id", "claim_id"}:
        return "analysis"
    if operation.startswith("pilot.") or key in {"case_id", "recommendation_id"}:
        return "pilot"
    if operation.startswith("calibration.") or key in {"protocol_id", "proposal_id"}:
        return "calibration"
    return "orchestration"


def _drill_down(authority: str) -> str:
    return {
        "external": "external.get", "analysis": "analysis.get",
        "pilot": "pilot.get", "calibration": "calibration.get",
        "orchestration": "session.explain",
    }[authority]


def _collect_links(value: Any, operation: str, result: list[dict[str, Any]]) -> None:
    if len(result) >= 100:
        return
    if isinstance(value, Mapping):
        checksum_candidates = {
            key: item for key, item in value.items()
            if isinstance(item, str) and (key.endswith("_checksum") or key.endswith("_hash"))
        }
        for key, item in value.items():
            if key in ID_KEYS and isinstance(item, str) and item:
                authority = _authority(operation, key)
                stem = key[:-3]
                digest = checksum_candidates.get(stem + "_checksum") or checksum_candidates.get("payload_checksum")
                link = {
                    "authority": authority, "record_type": stem,
                    "record_id": item, "checksum": digest,
                    "drill_down": _drill_down(authority),
                }
                if link not in result:
                    result.append(link)
            _collect_links(item, operation, result)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_links(item, operation, result)


def _next_actions(operation: str, data: Any) -> list[dict[str, Any]]:
    if isinstance(data, Mapping) and data.get("next_operation"):
        return [{"operation": "session.preview", "params": {"transition": data["next_operation"]}}]
    if operation.endswith(".list"):
        return [{"operation": operation.rsplit(".", 1)[0] + ".get", "requires": ["stable_id"]}]
    if operation.endswith(".get"):
        return [{"operation": operation.rsplit(".", 1)[0] + ".explain", "requires": ["stable_id"]}]
    if operation == "session.prepare":
        return [{"operation": "session.confirm", "requires": ["preview", "confirmed", "idempotency_key"]}]
    return []


def compact_envelope(
    result: Mapping[str, Any], *, operation: str | None = None,
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
) -> dict[str, Any]:
    """Project a service result into a deterministic, budgeted Agent envelope."""
    op = str(operation or result.get("operation") or "unknown")
    ok = result.get("ok") is True
    limitations = list(dict.fromkeys(str(item) for item in (
        list(result.get("limitations") or [])
        + (["verified metadata only", "explicit drill-down required for full evidence"] if ok else [])
    )))[:20]
    if not ok:
        raw = result.get("error") or {}
        code = str(raw.get("code") if isinstance(raw, Mapping) else raw or "runtime_error")
        category = _category(code)
        retryable, recovery, message = ERROR_CATALOG[category]
        envelope = {
            "schema_version": SCHEMA_VERSION, "operation": op, "ok": False,
            "status": "error", "summary": message,
            "ids": [], "limitations": limitations,
            "next_actions": [{"operation": item} for item in recovery],
            "evidence_links": [], "data": None, "truncated": False,
            "error": {"code": code, "category": category, "message": message,
                      "retryable": retryable, "recovery_actions": list(recovery)},
            "budget": {"limit_bytes": budget_bytes, "used_bytes": 0},
        }
    else:
        data = _safe(result.get("data"))
        ids: list[str] = []
        links: list[dict[str, Any]] = []
        _collect_ids(data, ids)
        _collect_links(data, op, links)
        verb = op.rsplit(".", 1)[-1].replace("_", " ")
        envelope = {
            "schema_version": SCHEMA_VERSION, "operation": op, "ok": True,
            "status": "success", "summary": f"{verb.capitalize()} completed; {len(ids)} stable reference(s) available.",
            "ids": ids, "limitations": limitations,
            "next_actions": _next_actions(op, data), "evidence_links": links,
            "data": data, "truncated": False,
            "budget": {"limit_bytes": budget_bytes, "used_bytes": 0},
        }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) > budget_bytes:
        envelope["data"] = None
        envelope["truncated"] = True
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    envelope["budget"]["used_bytes"] = len(encoded)
    final = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    envelope["budget"]["used_bytes"] = len(final)
    if len(final) > budget_bytes:
        envelope["evidence_links"] = envelope["evidence_links"][:20]
        envelope["ids"] = envelope["ids"][:20]
        envelope["budget"]["used_bytes"] = len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
    return envelope


__all__ = ["DEFAULT_BUDGET_BYTES", "ERROR_CATALOG", "SCHEMA_VERSION", "compact_envelope"]
