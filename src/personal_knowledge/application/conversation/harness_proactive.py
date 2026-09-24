"""Plan 61-07 deterministic proactive projection and append-only feedback.

The proactive adapter consumes only declared deterministic event metadata
(committed ``conversation.delta.committed`` bindings, D-18/D-23) and projects
review cards through global/project x category (``同步`` / ``简报`` /
``反思候选``) controls and quiet hours. One evidence cluster yields exactly one
card with a merged count and source/time/receipt/support/conflict drilldown.

Dismissal/undo is append-only feedback: an exact idempotent retry appends
nothing, undo appends a new entry and never mutates the original, and projected
cards keep a quiet anchor without rewriting manual message order. The adapter
never schedules a delivery, performs an external action, changes permissions,
values or authority, and never accepts a body, prompt, credential or SQL
statement.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping

CONTROL_CATEGORIES = frozenset({"同步", "简报", "反思候选"})
DECLARED_EVENT_TYPES = frozenset({"conversation.delta.committed"})

_QUIET_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ProactiveError(ValueError):
    """Fail-closed control error with a stable machine code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _checksum(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Quiet hours (deterministic, boundary-pinned by the Task 1 fixtures)
# ---------------------------------------------------------------------------

def _parse_quiet_time(value: Any) -> tuple[int, int]:
    match = _QUIET_TIME_RE.match(str(value))
    if not match:
        raise ValueError("invalid_time")
    return int(match.group(1)), int(match.group(2))


def _now_minutes(value: Any) -> tuple[int, int]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ProactiveError("quiet_hours_invalid", f"now:{value}") from exc
    return parsed.hour, parsed.minute


def _in_quiet_window(now: tuple[int, int], start: tuple[int, int], end: tuple[int, int]) -> bool:
    now_value = now[0] * 60 + now[1]
    start_value = start[0] * 60 + start[1]
    end_value = end[0] * 60 + end[1]
    if start_value <= end_value:
        return start_value <= now_value < end_value
    # Overnight window (e.g. 22:00 -> 07:00): active outside [start, end).
    return now_value >= start_value or now_value < end_value


def _resolve_quiet(quiet_hours: Any, now: Any) -> tuple[bool, str | None]:
    if not isinstance(quiet_hours, Mapping):
        raise ProactiveError("quiet_hours_invalid", "quiet_hours_required")
    if not quiet_hours.get("enabled"):
        return True, None
    try:
        start = _parse_quiet_time(quiet_hours.get("start"))
        end = _parse_quiet_time(quiet_hours.get("end"))
    except (TypeError, ValueError):
        raise ProactiveError("quiet_hours_invalid", "start/end must be HH:MM")
    now_min = _now_minutes(now)
    if _in_quiet_window(now_min, start, end):
        return False, str(quiet_hours.get("end"))
    return True, None


# ---------------------------------------------------------------------------
# Controls (global/project x category) and deterministic projection
# ---------------------------------------------------------------------------

def _control_enabled(controls: Any, scope: str, category: str) -> bool:
    rows = tuple(item for item in (controls or ()) if isinstance(item, Mapping))
    exact = next(
        (row for row in rows if str(row.get("scope")) == scope and str(row.get("category")) == category),
        None,
    )
    if exact is not None:
        return bool(exact.get("enabled"))
    if scope != "global":
        fallback = next(
            (row for row in rows if str(row.get("scope")) == "global" and str(row.get("category")) == category),
            None,
        )
        if fallback is not None:
            return bool(fallback.get("enabled"))
    return True


def _drilldown(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": item.get("event_id"),
        "source": item.get("source"),
        "occurred_at": item.get("occurred_at"),
        "receipt_checksum": item.get("receipt_checksum"),
        "support_refs": tuple(item.get("support_refs") or ()),
        "conflict_refs": tuple(item.get("conflict_refs") or ()),
        "canonical_checksum": item.get("canonical_checksum"),
        "watermark": item.get("watermark"),
    }


def project_proactive_state(
    *,
    events: Any,
    controls: Any,
    quiet_hours: Any,
    now: str,
    manual_order: Any,
) -> dict[str, Any]:
    """Project deterministic proactive cards from declared committed-delta events.

    Only ``conversation.delta.committed`` events with a declared category may
    project. Disabled categories/projects produce no card; quiet hours expose
    ``active`` / ``quiet_until`` without scheduling any delivery. One evidence
    cluster renders exactly one card with a merged count and drilldown.
    """
    active, quiet_until = _resolve_quiet(quiet_hours, now)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            raise ProactiveError("declared_event", "event_required")
        if event.get("type") not in DECLARED_EVENT_TYPES:
            raise ProactiveError("declared_event", str(event.get("type")))
        category = event.get("category")
        if category not in CONTROL_CATEGORIES:
            raise ProactiveError("declared_category", str(category))
        scope = event.get("scope")
        cluster_key = event.get("cluster_key")
        if not scope or not cluster_key:
            raise ProactiveError("declared_event", "scope/cluster_key required")
        if not _control_enabled(controls, str(scope), category):
            continue
        grouped.setdefault(str(cluster_key), []).append(event)

    manual = tuple(manual_order)
    anchor = manual[0] if manual else None
    cards: list[dict[str, Any]] = []
    for cluster_key in sorted(grouped):
        items = grouped[cluster_key]
        support = tuple(sorted({ref for item in items for ref in (item.get("support_refs") or ())}))
        conflict = tuple(sorted({ref for item in items for ref in (item.get("conflict_refs") or ())}))
        first = items[0]
        cards.append(
            {
                "cluster_key": cluster_key,
                "category": first.get("category"),
                "scope": first.get("scope"),
                "merged_count": len(items),
                "merged_evidence": tuple(_drilldown(item) for item in items),
                "support_refs": support,
                "conflict_refs": conflict,
                "canonical_checksum": first.get("canonical_checksum"),
                "watermark": first.get("watermark"),
                "rule_version": first.get("rule_version"),
                "anchor_before": anchor,
            }
        )

    return {
        "active": active,
        "quiet_until": quiet_until,
        "cards": tuple(cards),
        "manual_order": manual,
    }


# ---------------------------------------------------------------------------
# Append-only dismissal / undo feedback
# ---------------------------------------------------------------------------

def _dismissal_receipt(entry: Mapping[str, Any], log_len: int) -> dict[str, Any]:
    return {
        "operation": "dismiss",
        "feedback_id": entry.get("feedback_id"),
        "cluster_key": entry.get("cluster_key"),
        "actor_identity_hash": entry.get("actor_identity_hash"),
        "idempotency_key": entry.get("idempotency_key"),
        "dismissed_at": entry.get("dismissed_at"),
        "receipt_checksum": entry.get("receipt_checksum"),
        "feedback_count": log_len,
        "metadata_only": True,
    }


def _undo_receipt(entry: Mapping[str, Any], log_len: int) -> dict[str, Any]:
    return {
        "operation": "undo_dismissal",
        "dismissal_feedback_id": entry.get("dismissal_feedback_id"),
        "feedback_id": entry.get("feedback_id"),
        "actor_identity_hash": entry.get("actor_identity_hash"),
        "idempotency_key": entry.get("idempotency_key"),
        "undone_at": entry.get("undone_at"),
        "receipt_checksum": entry.get("receipt_checksum"),
        "feedback_count": log_len,
        "metadata_only": True,
    }


def apply_dismissal(
    *,
    feedback_log: Any,
    cluster_key: str,
    feedback_id: str,
    actor_identity_hash: str,
    idempotency_key: str,
    now: str,
) -> dict[str, Any]:
    """Append exactly one dismissal entry; an exact idempotent retry appends nothing."""
    log = tuple(feedback_log)
    existing = next(
        (entry for entry in log if isinstance(entry, Mapping)
         and entry.get("actor_identity_hash") == actor_identity_hash
         and entry.get("idempotency_key") == idempotency_key),
        None,
    )
    if existing is not None:
        return {
            "existing": True,
            "feedback_log": log,
            "receipt": _dismissal_receipt(existing, len(log)),
        }
    core = {
        "operation": "dismiss",
        "cluster_key": cluster_key,
        "feedback_id": feedback_id,
        "actor_identity_hash": actor_identity_hash,
        "idempotency_key": idempotency_key,
        "dismissed_at": now,
    }
    entry = {**core, "receipt_checksum": _checksum(core)}
    new_log = log + (entry,)
    return {
        "existing": False,
        "feedback_log": new_log,
        "receipt": _dismissal_receipt(entry, len(new_log)),
    }


def undo_dismissal(
    *,
    feedback_log: Any,
    dismissal_feedback_id: str,
    feedback_id: str,
    actor_identity_hash: str,
    idempotency_key: str,
    now: str,
) -> dict[str, Any]:
    """Append a new undo entry; never deletes or mutates the dismissal entry."""
    log = tuple(feedback_log)
    existing = next(
        (entry for entry in log if isinstance(entry, Mapping)
         and entry.get("actor_identity_hash") == actor_identity_hash
         and entry.get("idempotency_key") == idempotency_key),
        None,
    )
    if existing is not None:
        return {
            "existing": True,
            "feedback_log": log,
            "receipt": _undo_receipt(existing, len(log)),
        }
    target = next(
        (entry for entry in log if isinstance(entry, Mapping)
         and entry.get("operation") == "dismiss"
         and entry.get("feedback_id") == dismissal_feedback_id),
        None,
    )
    if target is None:
        raise ProactiveError("dismissal_not_found", dismissal_feedback_id)
    core = {
        "operation": "undo_dismissal",
        "dismissal_feedback_id": dismissal_feedback_id,
        "feedback_id": feedback_id,
        "actor_identity_hash": actor_identity_hash,
        "idempotency_key": idempotency_key,
        "undone_at": now,
    }
    entry = {**core, "receipt_checksum": _checksum(core)}
    new_log = log + (entry,)
    return {
        "existing": False,
        "feedback_log": new_log,
        "receipt": _undo_receipt(entry, len(new_log)),
    }


__all__ = [
    "CONTROL_CATEGORIES",
    "DECLARED_EVENT_TYPES",
    "ProactiveError",
    "apply_dismissal",
    "project_proactive_state",
    "undo_dismissal",
]
