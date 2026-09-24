"""Same-origin, metadata-only Pi runtime projection for the Cockpit.

The REST process is not the Pi authority.  It reads the loopback Kernel HTTP
contract and returns a deliberately smaller UI envelope.  When the Kernel is
unreachable the projection reports ``offline`` instead of manufacturing a
ready state or mutating an in-memory copy of a task.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

PI_COCKPIT_SCHEMA = "pi_cockpit_event_v1"
SAFE_STATES = {"queued", "claimed", "running", "cancel_requested", "succeeded", "failed", "outcome_unknown", "offline", "stale"}
_DEFAULT_KERNEL_URL = "http://127.0.0.1:8790"
_TIMEOUT_SECONDS = 2.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _kernel_url() -> str:
    raw = str(os.environ.get("PI_KERNEL_URL") or _DEFAULT_KERNEL_URL).rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("kernel_url_must_be_loopback")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("kernel_url_invalid")
    return raw


def _request_json(method: str, path: str, payload: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"{_kernel_url()}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read(256 * 1024)
            parsed = json.loads(raw.decode("utf-8") or "{}")
            return int(response.status), parsed if isinstance(parsed, dict) else {}
    except HTTPError as error:
        try:
            raw = error.read(32 * 1024)
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            parsed = {}
        return int(error.code), parsed if isinstance(parsed, dict) else {}
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return 0, {}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _progress(state: str) -> int:
    return {"queued": 0, "claimed": 25, "running": 50, "cancel_requested": 75, "succeeded": 100, "failed": 100, "outcome_unknown": 100}.get(state, 0)


def _recovery_action(state: str) -> str:
    return {"outcome_unknown": "reconcile_outcome", "failed": "inspect_error", "offline": "restart_kernel", "stale": "inspect_status"}.get(state, "none")


def safe_event(event: Mapping[str, Any]) -> dict[str, Any]:
    state = str(event.get("state") or "unknown")
    if state not in SAFE_STATES:
        state = "stale"
    refs = event.get("evidence_refs")
    return {
        "schema_version": PI_COCKPIT_SCHEMA,
        "event_id": str(event.get("event_id") or ""),
        "task_id": str(event.get("task_id") or ""),
        "session_id": str(event.get("session_id") or ""),
        "state": state,
        "version": _safe_int(event.get("version")),
        "progress": max(0, min(100, _safe_int(event.get("progress"), _progress(state)))),
        "tool_label": str(event.get("tool_label") or ""),
        "evidence_refs": list(refs)[:20] if isinstance(refs, list) else [],
        "recovery_action": str(event.get("recovery_action") or _recovery_action(state)),
        "observed_at": str(event.get("observed_at") or _now()),
    }


def kernel_status() -> dict[str, Any]:
    observed = _now()
    try:
        status, payload = _request_json("GET", "/ready")
        port = _safe_int(urlparse(_kernel_url()).port, 8790)
        if status == 0:
            return {
                "schema_version": PI_COCKPIT_SCHEMA,
                "service": "pi-kernel",
                "state": "offline",
                "host": "127.0.0.1",
                "port": port,
                "provider_calls": 0,
                "observed_at": observed,
                "recovery_action": "restart_kernel",
            }
        ready = status == 200 and payload.get("ready") is True and payload.get("ok") is True
        return {
            "schema_version": PI_COCKPIT_SCHEMA,
            "service": "pi-kernel",
            "state": "ready" if ready else "degraded",
            "host": "127.0.0.1",
            "port": port,
            "provider_calls": _safe_int(payload.get("provider_calls")),
            "observed_at": observed,
            "recovery_action": "none" if ready else "inspect_readiness",
        }
    except (ValueError, OSError):
        return {
            "schema_version": PI_COCKPIT_SCHEMA,
            "service": "pi-kernel",
            "state": "offline",
            "host": "127.0.0.1",
            "port": 8790,
            "provider_calls": 0,
            "observed_at": observed,
            "recovery_action": "restart_kernel",
        }


def _project_task(task: Mapping[str, Any]) -> dict[str, Any]:
    state = str(task.get("state") or "stale")
    return safe_event({
        "event_id": task.get("event_ref") or f"task:{task.get('task_id') or ''}",
        "task_id": task.get("task_id"),
        "state": state,
        "version": task.get("version"),
        "progress": _progress(state),
        "tool_label": "pi-kernel task",
        "evidence_refs": [],
        "recovery_action": _recovery_action(state),
        "observed_at": task.get("updated_at") or task.get("created_at"),
    })


def task_list() -> list[dict[str, Any]]:
    status, payload = _request_json("GET", "/v1/tasks")
    if status != 200 or payload.get("ok") is not True or not isinstance(payload.get("tasks"), list):
        return []
    return [_project_task(task) for task in payload["tasks"] if isinstance(task, Mapping)]


def open_event_stream(last_event_id: str | None = None):
    """Open the internal Kernel SSE cursor for the same-origin API proxy."""
    headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
    if last_event_id:
        headers["Last-Event-ID"] = str(last_event_id)
    request = Request(f"{_kernel_url()}/v1/events/stream", headers=headers, method="GET")
    return urlopen(request, timeout=None)


def mutate_task(action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    task_id = str(payload.get("task_id") or "")
    if action not in {"cancel", "resume"} or not task_id or not payload.get("idempotency_key"):
        return {"ok": False, "error": {"code": "task_identity_required"}}
    status, response = _request_json(
        "POST",
        f"/v1/tasks/{task_id}/{action}",
        {key: value for key, value in payload.items() if key in {"task_id", "expected_version", "idempotency_key", "state", "output_checksum", "error_code"}},
    )
    if status == 200 and response.get("ok") is True and isinstance(response.get("task"), Mapping):
        return {"ok": True, "data": _project_task(response["task"])}
    code = ((response.get("error") or {}).get("code") if isinstance(response.get("error"), Mapping) else None) or ("kernel_offline" if status == 0 else "kernel_mutation_failed")
    return {"ok": False, "error": {"code": str(code)}}


__all__ = ["PI_COCKPIT_SCHEMA", "kernel_status", "safe_event", "task_list", "mutate_task", "open_event_stream"]
