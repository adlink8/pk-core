"""Pure Wiki-domain contract primitives.

This module deliberately has no authority, database, provider, HTTP, or index
dependencies.  It only defines the stable identifiers, the canonical TopicKey,
and the read-envelope shape that later Wiki adapters and the projection service
may consume.  ``services.topic_projection`` re-exports these symbols so the
historical import path keeps working.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Callable, Mapping
from urllib.parse import unquote
import re


WIKI_SCHEMA_VERSION = "personal_wiki_projection_v1"
WIKI_OPERATIONS = frozenset({"topic.list", "topic.get", "topic.backlinks", "topic.resolve"})
WIKI_REASON_CODES = frozenset({
    "invalid_topic_key",
    "unsupported_topic_type",
    "topic_not_found",
    "authority_unavailable",
    "authority_binding_missing",
    "evidence_unavailable",
    "privacy_sealed",
    "projection_partial",
    "projection_record_missing",
    "snapshot_mismatch",
    "serving_snapshot_changed",
    "personal_snapshot_changed",
    "external_snapshot_changed",
    "decision_sequence_changed",
    "dependency_missing",
    "dependency_lifecycle_changed",
    "dependency_checksum_mismatch",
})

_HEX_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_REPEATED_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class TopicProjectionError(ValueError):
    """A safe, non-data-bearing contract error."""

    def __init__(self, reason_code: str):
        if reason_code not in WIKI_REASON_CODES:
            reason_code = "invalid_topic_key"
        self.reason_code = reason_code
        super().__init__(reason_code)


def _validate_segment(segment: str) -> None:
    if not segment or segment != segment.strip():
        raise TopicProjectionError("invalid_topic_key")
    if _CONTROL.search(segment) or any(separator in segment for separator in (":", "/", "\\")):
        raise TopicProjectionError("invalid_topic_key")
    # A percent escape remaining after one decode would make the key's
    # canonical representation ambiguous (and permit a second decode).
    if _REPEATED_ESCAPE.search(segment):
        raise TopicProjectionError("invalid_topic_key")


@dataclass(frozen=True, slots=True)
class TopicKey:
    """Immutable canonical Wiki topic identifier.

    Supported forms are exactly::

        project:{scope}
        goal:{domain}:{scope}:{predicate}
        decision:{recommendation_id}
        subject:{name}

    ``subject`` topics are the Phase 4 consolidated pages derived from
    knowledge-unit buckets; they have no personal/decision authority backing
    and resolve through the page store instead.
    """

    topic_type: str
    parts: tuple[str, ...]

    def __post_init__(self) -> None:
        expected = {"project": 1, "goal": 3, "decision": 1, "subject": 1}
        if self.topic_type not in expected or len(self.parts) != expected[self.topic_type]:
            raise TopicProjectionError("unsupported_topic_type" if self.topic_type not in expected else "invalid_topic_key")
        for part in self.parts:
            _validate_segment(part)

    @property
    def canonical(self) -> str:
        return ":".join((self.topic_type, *self.parts))

    @classmethod
    def parse(cls, value: str) -> "TopicKey":
        return parse_topic_key(value)


def parse_topic_key(value: str) -> TopicKey:
    """Parse one URL-decoded, canonical TopicKey.

    Decoding is performed exactly once.  Errors expose only a stable reason
    code and never echo the supplied key or any private data.
    """
    if not isinstance(value, str) or not value or _CONTROL.search(value):
        raise TopicProjectionError("invalid_topic_key")
    if _HEX_ESCAPE.search(value):
        raise TopicProjectionError("invalid_topic_key")
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise TopicProjectionError("invalid_topic_key") from None
    if not decoded or _CONTROL.search(decoded) or _REPEATED_ESCAPE.search(decoded):
        raise TopicProjectionError("invalid_topic_key")

    segments = decoded.split(":")
    topic_type = segments[0]
    if topic_type not in {"project", "goal", "decision", "subject"}:
        raise TopicProjectionError("unsupported_topic_type")
    expected_parts = {"project": 1, "goal": 3, "decision": 1, "subject": 1}[topic_type]
    if len(segments) != expected_parts + 1:
        raise TopicProjectionError("invalid_topic_key")
    parts = tuple(segments[1:])
    try:
        key = TopicKey(topic_type, parts)
    except TopicProjectionError:
        raise
    if key.canonical != decoded:
        raise TopicProjectionError("invalid_topic_key")
    return key


def make_wiki_envelope(
    operation: str,
    *,
    ok: bool,
    data: Any = None,
    generated_at: str | None = None,
    snapshot_bindings: Mapping[str, Any] | None = None,
    freshness: Mapping[str, Any] | None = None,
    authorities: Mapping[str, Any] | None = None,
    partial: bool = False,
    limitations: tuple[str, ...] | list[str] = (),
    error: str | None = None,
    projection_checksum: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Build the common read-only Wiki response envelope.

    The function validates the operation and keeps the outer contract stable;
    it does not query or infer any authority state.
    """
    if operation not in WIKI_OPERATIONS:
        raise ValueError("unsupported Wiki operation")
    if error is not None and error not in WIKI_REASON_CODES:
        raise ValueError("invalid Wiki reason code")
    if not ok and error is None:
        error = "projection_partial" if partial else "authority_unavailable"
    if ok and error is not None:
        raise ValueError("successful envelope cannot contain an error")
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": WIKI_SCHEMA_VERSION,
        "operation": operation,
        "ok": bool(ok),
        "generated_at": generated_at,
        "snapshot_bindings": dict(snapshot_bindings or {}),
        "freshness": dict(freshness or {}),
        "authorities": dict(authorities or {}),
        "partial": bool(partial),
        "limitations": list(limitations),
        "data": data,
        "error": error,
        "projection_checksum": projection_checksum,
        "status": status or ("partial" if partial else ("success" if ok else "unavailable")),
    }


def safe_reason_code(reason_code: str) -> str:
    """Return a public Wiki reason code without exposing arbitrary detail."""
    return reason_code if reason_code in WIKI_REASON_CODES else "projection_partial"


def opaque_topic_id(key: TopicKey) -> str:
    """Create a stable, non-reversible server-owned topic identifier."""
    digest = sha256(key.canonical.encode("utf-8")).hexdigest()
    return f"topic_{digest[:32]}"


__all__ = [
    "TopicKey",
    "TopicProjectionError",
    "WIKI_OPERATIONS",
    "WIKI_REASON_CODES",
    "WIKI_SCHEMA_VERSION",
    "make_wiki_envelope",
    "opaque_topic_id",
    "parse_topic_key",
    "safe_reason_code",
]
