"""Metadata-only projection of the single Pi Kernel operation control plane."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from personal_knowledge.services.pi_runtime_projection import _request_json

PI_OPERATION_PROJECTION_SCHEMA = "pi_operation_projection_v1"
OPERATION_KINDS = {"kernel_task", "kernel_session", "kernel_skill", "domain_tool", "provider", "authority_transaction"}
OPERATION_STATES = {"queued", "running", "cancel_requested", "cancelled", "succeeded", "failed", "outcome_unknown", "reconciling", "resumable", "compensated", "manual_review"}
SAFE_ACTIONS = {"cancel", "resume", "reconcile", "inspect", "compensate"}
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_FORBIDDEN = re.compile(r"(?:body|content|prompt|completion|credential|secret|token|password|path|input|output|result)", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token(value: Any) -> str:
    text = str(value or "")
    return text if _TOKEN.fullmatch(text) else ""


def _refs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:20]:
        if not isinstance(item, Mapping):
            continue
        ref, checksum = _token(item.get("ref")), str(item.get("checksum") or "")
        if ref and _SHA256.fullmatch(checksum):
            result.append({"ref": ref, "checksum": checksum})
    return result


def _budget(value: Any) -> dict[str, int]:
    value = value if isinstance(value, Mapping) else {}
    return {key: max(0, int(value.get(key) or 0)) if str(value.get(key) or "0").lstrip("-").isdigit() else 0 for key in ("token_limit", "cost_limit", "timeout_ms", "token_used", "cost_used")}


def _assert_metadata(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _FORBIDDEN.search(str(key)):
                raise ValueError("forbidden_inline_field")
            _assert_metadata(child)
    elif isinstance(value, list):
        for child in value:
            _assert_metadata(child)


def safe_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
    state = str(operation.get("state") or "stale")
    if state not in OPERATION_STATES:
        state = "manual_review"
    kind = str(operation.get("operation_kind") or "")
    if kind not in OPERATION_KINDS:
        kind = "domain_tool"
    actions = [str(action) for action in operation.get("recovery_actions", []) if str(action) in SAFE_ACTIONS][:10]
    return {
        "schema_version": PI_OPERATION_PROJECTION_SCHEMA,
        "operation_id": _token(operation.get("operation_id")),
        "operation_kind": kind,
        "task_id": _token(operation.get("task_id")),
        "session_id": _token(operation.get("session_id")),
        "correlation_id": _token(operation.get("correlation_id")),
        "authority_class": _token(operation.get("authority_class")),
        "side_effect_class": str(operation.get("side_effect_class") or "none"),
        "snapshot_id": _token(operation.get("snapshot_id")),
        "state": state,
        "version": max(0, int(operation.get("version") or 0)),
        "attempt": max(0, int(operation.get("attempt") or 0)),
        "budget": _budget(operation.get("budget")),
        "receipt_refs": _refs(operation.get("receipt_refs")),
        "fingerprint_refs": _refs(operation.get("fingerprint_refs")),
        "allowed_actions": actions,
        "reason": str(operation.get("reason") or "")[:256],
        "created_at": str(operation.get("created_at") or ""),
        "updated_at": str(operation.get("updated_at") or ""),
    }


def _offline(action: str) -> dict[str, Any]:
    return {"schema_version": PI_OPERATION_PROJECTION_SCHEMA, "ok": False, "state": "offline", "operations": [], "error": {"code": "kernel_offline", "action": action}, "observed_at": _now(), "recovery_action": "restart_kernel"}


def operation_list() -> dict[str, Any]:
    status, payload = _request_json("GET", "/v1/operations")
    if status != 200 or payload.get("ok") is not True or not isinstance(payload.get("operations"), list):
        return _offline("list") if status == 0 else {"schema_version": PI_OPERATION_PROJECTION_SCHEMA, "ok": False, "state": "degraded", "operations": [], "error": {"code": "kernel_operation_read_failed"}, "observed_at": _now(), "recovery_action": "inspect_status"}
    return {"schema_version": PI_OPERATION_PROJECTION_SCHEMA, "ok": True, "state": "ready", "operations": [safe_operation(item) for item in payload["operations"] if isinstance(item, Mapping)], "observed_at": _now(), "recovery_action": "none"}


def operation_get(operation_id: str) -> dict[str, Any]:
    operation_id = _token(operation_id)
    if not operation_id:
        return {"schema_version": PI_OPERATION_PROJECTION_SCHEMA, "ok": False, "error": {"code": "operation_identity_required"}}
    status, payload = _request_json("GET", f"/v1/operations/{operation_id}")
    if status == 0:
        return _offline("get")
    if status != 200 or payload.get("ok") is not True or not isinstance(payload.get("operation"), Mapping):
        return {"schema_version": PI_OPERATION_PROJECTION_SCHEMA, "ok": False, "error": {"code": "operation_not_available"}}
    return {"schema_version": PI_OPERATION_PROJECTION_SCHEMA, "ok": True, "operation": safe_operation(payload["operation"]), "observed_at": _now(), "recovery_action": "none"}


def mutate_operation(action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if action not in {"cancel", "resume", "reconcile"}:
        return {"schema_version": PI_OPERATION_PROJECTION_SCHEMA, "ok": False, "error": {"code": "operation_action_forbidden"}}
    operation_id = _token(payload.get("operation_id"))
    if not operation_id or not payload.get("idempotency_key") or not isinstance(payload.get("expected_version"), int):
        return {"schema_version": PI_OPERATION_PROJECTION_SCHEMA, "ok": False, "error": {"code": "operation_identity_required"}}
    allowed = {"operation_id", "expected_version", "idempotency_key", "receipt_refs", "fingerprint_refs", "receipt_status"}
    body = {key: payload[key] for key in allowed if key in payload}
    status, response = _request_json("POST", f"/v1/operations/{operation_id}/{action}", body)
    if status == 0:
        return _offline(action)
    if status == 200 and response.get("ok") is True and isinstance(response.get("operation"), Mapping):
        return {"schema_version": PI_OPERATION_PROJECTION_SCHEMA, "ok": True, "operation": safe_operation(response["operation"]), "action": str(response.get("action") or action), "retry_allowed": response.get("retry_allowed") is True, "reconciled_before_retry": response.get("reconciled_before_retry") is True}
    code = ((response.get("error") or {}).get("code") if isinstance(response.get("error"), Mapping) else None) or "kernel_operation_mutation_failed"
    return {"schema_version": PI_OPERATION_PROJECTION_SCHEMA, "ok": False, "error": {"code": str(code)}}


__all__ = ["PI_OPERATION_PROJECTION_SCHEMA", "safe_operation", "operation_list", "operation_get", "mutate_operation"]
