"""Shared candidate review vocabulary, identity and validation rules."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping
from personal_knowledge.core.project_paths import ROOT

REVIEW_ACTIONS = frozenset({"accept", "edit", "ignore", "undo"})
REVIEW_OUTCOMES = frozenset({
    "reviewed",
    "duplicate",
    "confirmation_required",
    "stale_version",
    "conflict_disposition_required",
    "rejected",
    "outcome_unknown",
})
CONFLICT_DISPOSITIONS = frozenset({
    "keep_existing",
    "replace_existing",
    "coexist_by_context",
    "defer_judgment",
})
CONFLICT_DISPOSITION_LABELS = {
    "keep_existing": "保留旧结论",
    "replace_existing": "用新结论取代",
    "coexist_by_context": "按情境共存",
    "defer_judgment": "暂不判断",
}
CONFLICT_DISPOSITION_CONSEQUENCES = {
    "keep_existing": "保留旧结论：新候选仅记录为未采纳的参考，不改变当前认知。",
    "replace_existing": "用新结论取代：旧结论转入历史版本，后续投影基于新结论生成。",
    "coexist_by_context": "按情境共存：新旧结论按适用情境并存，投影时按情境选择。",
    "defer_judgment": "暂不判断：候选保持待评审状态，不进入任何投影。",
}

# A fixed default ledger location for the gateway-provider path. The adapter
# itself never discovers or writes canonical/promotion/watermark/permission/
# value state; the ledger holds only stable review identities and receipts.
DEFAULT_CANDIDATE_REVIEW_DB = ROOT / "var" / "db" / "candidate_review.sqlite"

# User-editable derived metadata only. Identity, evidence, provenance and
# authority fields are immutable and are never accepted into this table.
EDITABLE_CANDIDATE_FIELDS = frozenset({
    "subject", "scope", "observed_at", "valid_from", "valid_to",
    "confidence", "uncertainty", "conclusion",
})


class CandidateReviewError(Exception):
    """Fail-closed validation error with a stable machine code."""

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
    """Exact Task 1 fixture formula: canonical JSON -> sha256 hex."""
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _idempotency_identity(candidate_id: str, action: str, idempotency_key: Any, feedback_id: Any) -> str:
    """Stable idempotency identity, deliberately excluding expected_version.

    Idempotency must dedupe before version validation: an exact replay with a
    stale ``expected_version`` still returns ``duplicate``.
    """
    return hashlib.sha256(json.dumps(
        {
            "candidate_id": candidate_id,
            "action": action,
            "idempotency_key": "" if idempotency_key is None else str(idempotency_key),
            "feedback_id": None if feedback_id is None else str(feedback_id),
        },
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _request_checksum(request: Mapping[str, Any]) -> str:
    return _checksum({key: request.get(key) for key in (
        "edited_payload", "edited_payload_checksum", "explicit_confirmation",
        "confirmation_token", "conflict_disposition", "feedback_id",
    )})


def validate_edit_fields(fields: Any) -> None:
    if not isinstance(fields, Mapping) or not fields or set(fields) - EDITABLE_CANDIDATE_FIELDS:
        raise CandidateReviewError("edited_payload_fields_invalid")
    for key, value in fields.items():
        if key == "confidence":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise CandidateReviewError("edited_confidence_invalid")
        elif key == "valid_to" and value is None:
            continue
        elif not isinstance(value, str) or not value.strip() or len(value) > 2048:
            raise CandidateReviewError("edited_value_invalid", key)


def effective_review_events(candidate_id: str, rows) -> list:
    """Undo removes its referenced gesture; undoing an undo restores that gesture."""
    history = [row for row in rows if str(row.get("candidate_id") or "") == candidate_id]
    suppressed: set[str] = set()
    active = []
    for row in reversed(history):
        if str(row.get("feedback_id") or "") in suppressed:
            continue
        if row.get("action") == "undo":
            suppressed.add(str(row.get("referenced_feedback_id") or ""))
        else:
            active.append(row)
    return list(reversed(active))

