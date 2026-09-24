"""Phase 62 F12: shared timestamp normalization seam.

Family adapters receive native timestamps in heterogeneous shapes — ISO-8601
strings, epoch milliseconds (int or digit string), or ``None``.  Before those
values flow into ``occurred_at`` / ``started_at`` / ``ended_at`` (``ce_events``
and the compatibility projection), they MUST be normalized to one canonical
shape: UTC ISO-8601 with ``Z`` suffix.

This module owns ONLY timestamp normalization (one module = one primary reason
to change, per engineering contract).  It never parses family formats and never
touches native payloads beyond the timestamp value.
"""

from __future__ import annotations

from datetime import datetime, timezone

# A 13-digit epoch-millisecond value is far above any plausible 10-digit
# epoch-seconds value; anything strictly greater than this floor is treated
# as milliseconds.
_EPOCH_MS_FLOOR = 1_000_000_000_000


def _is_epoch_millis(value) -> bool:
    """True when the value is a 13-digit epoch-millisecond timestamp."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > _EPOCH_MS_FLOOR
    if isinstance(value, float):
        return value > _EPOCH_MS_FLOOR
    if isinstance(value, str):
        s = value.strip()
        if not s.isdigit():
            return False
        try:
            return int(s) > _EPOCH_MS_FLOOR
        except ValueError:
            return False
    return False


def _epoch_millis_to_iso(millis: int) -> str:
    seconds, remainder = divmod(millis, 1000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    base = dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    if remainder:
        base = base[:-1] + f".{remainder:03d}Z"
    return base


def _iso_to_utc_z(value: str) -> str | None:
    """Parse an ISO-8601 string to canonical UTC ``...Z`` with ms precision."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    if dt.microsecond:
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_timestamp(value) -> str | None:
    """Normalize a native timestamp to UTC ISO-8601 with a ``Z`` suffix.

    Accepts:
      - ``None``                              -> ``None``
      - ``int``/``float`` epoch milliseconds  -> ``...Z``
      - digit-string epoch milliseconds       -> ``...Z``
      - ISO-8601 strings (with or without TZ) -> UTC ``...Z`` (ms precision)
      - anything else                         -> ``str(value)`` unchanged
        (preserved verbatim rather than guessed)

    The returned string is stable across families so that ``ce_events`` /
    projection layers always see one canonical shape.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return str(value)
    if _is_epoch_millis(value):
        return _epoch_millis_to_iso(int(value))
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            return s
        return _iso_to_utc_z(s)
    return str(value)


__all__ = ["normalize_timestamp", "_is_epoch_millis"]
