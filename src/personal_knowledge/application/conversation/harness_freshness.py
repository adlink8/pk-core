"""Plan 61-05 dual-watermark harness freshness projection (HARNESS-04).

Two independently truthful freshness legs, never collapsed into one number:

    source -> AgentsView    (source probe + AgentsView watermark/backlog)
    AgentsView -> canonical (canonical watermark/backlog)

A scalar ``current``/``complete`` claim is forbidden whenever either leg lacks
proof. stale / unknown / missing watermark / nonzero backlog each carry their
own status value and a limitation string so the UI never renders a stale or
unproven leg as current. The projection is metadata-only: no message body,
credential or private diagnostic is ever present.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from personal_knowledge.adapters.agentsview import SourceProbe  # noqa: F401  (type reference)

LEG_SOURCE_TO_AGENTSVIEW = "source_to_agentsview"
LEG_AGENTSVIEW_TO_CANONICAL = "agentsview_to_canonical"

STATUSES = ("current", "stale", "unknown", "missing_watermark", "backlog_pending")


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _classify(probe_ok: bool, watermark: str | None, backlog: int, now: str, stale_after_seconds: int) -> str:
    """Classify one leg deterministically: probe -> watermark -> backlog -> age."""
    if not probe_ok:
        return "unknown"
    if watermark is None:
        return "missing_watermark"
    if backlog > 0:
        return "backlog_pending"
    if (_parse_ts(now) - _parse_ts(watermark)).total_seconds() > stale_after_seconds:
        return "stale"
    return "current"


def _limitation(status: str, backlog: int) -> str:
    """UI copy limitation that names the state so stale/unknown/backlog stay distinct."""
    if status == "unknown":
        return "unknown: the freshness probe did not pass the schema/integrity gate"
    if status == "missing_watermark":
        return "missing watermark: no synchronization watermark has been recorded"
    if status == "backlog_pending":
        return f"backlog pending: {backlog} uncommitted items have not reached canonical"
    if status == "stale":
        return "stale: the latest synchronized data is older than the freshness horizon"
    return "current"


@dataclass(frozen=True)
class FreshnessLeg:
    """One typed freshness leg; never a scalar, always carries its own identity."""

    leg: str
    status: str
    watermark: str | None
    observed_at: str
    backlog: int
    limitation: str

    def to_dict(self) -> dict:
        return {
            "leg": self.leg,
            "status": self.status,
            "watermark": self.watermark,
            "observed_at": self.observed_at,
            "backlog": self.backlog,
            "limitation": self.limitation,
        }


@dataclass(frozen=True)
class DualFreshness:
    """Two typed legs plus an overall status that never hides a missing proof."""

    source_to_agentsview: FreshnessLeg
    agentsview_to_canonical: FreshnessLeg
    as_of: str

    @property
    def overall_status(self) -> str:
        statuses = [self.source_to_agentsview.status, self.agentsview_to_canonical.status]
        if all(status == "current" for status in statuses):
            return "current"
        if "unknown" in statuses:
            return "unknown"
        if "missing_watermark" in statuses:
            return "missing_watermark"
        if "backlog_pending" in statuses:
            return "backlog_pending"
        return "stale"

    def to_dict(self) -> dict:
        return {
            LEG_SOURCE_TO_AGENTSVIEW: self.source_to_agentsview.to_dict(),
            LEG_AGENTSVIEW_TO_CANONICAL: self.agentsview_to_canonical.to_dict(),
            "overall_status": self.overall_status,
            "as_of": self.as_of,
        }


def project_freshness(
    *,
    source_probe,
    source_watermark: str | None,
    source_backlog: int,
    canonical_watermark: str | None,
    canonical_backlog: int,
    now: str,
    stale_after_seconds: int = 3600,
    canonical_probe=None,
) -> DualFreshness:
    """Project two independent freshness legs and derive an overall status.

    ``canonical_probe`` is optional. When the caller does not supply a separate
    canonical probe the canonical authority is assumed healthy so its leg is
    classified from watermark/backlog alone; a caller that probed the canonical
    authority can pass it to surface a failed canonical probe as ``unknown``.
    """
    source_ok = bool(getattr(source_probe, "ok", False))
    canonical_ok = True if canonical_probe is None else bool(getattr(canonical_probe, "ok", False))

    source_status = _classify(source_ok, source_watermark, source_backlog, now, stale_after_seconds)
    canonical_status = _classify(canonical_ok, canonical_watermark, canonical_backlog, now, stale_after_seconds)

    source_leg = FreshnessLeg(
        leg=LEG_SOURCE_TO_AGENTSVIEW,
        status=source_status,
        watermark=source_watermark,
        observed_at=now,
        backlog=source_backlog,
        limitation=_limitation(source_status, source_backlog),
    )
    canonical_leg = FreshnessLeg(
        leg=LEG_AGENTSVIEW_TO_CANONICAL,
        status=canonical_status,
        watermark=canonical_watermark,
        observed_at=now,
        backlog=canonical_backlog,
        limitation=_limitation(canonical_status, canonical_backlog),
    )
    return DualFreshness(
        source_to_agentsview=source_leg,
        agentsview_to_canonical=canonical_leg,
        as_of=now,
    )


__all__ = [
    "DualFreshness",
    "FreshnessLeg",
    "LEG_AGENTSVIEW_TO_CANONICAL",
    "LEG_SOURCE_TO_AGENTSVIEW",
    "STATUSES",
    "project_freshness",
]
