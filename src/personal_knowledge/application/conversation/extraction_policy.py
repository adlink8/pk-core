"""Phase 62-05: versioned replaceable extraction policy.

Phase 62 CONTEXT D-22..D-24: extraction ordering is owned by a versioned
``ExtractionPolicy``, never hard-coded into adapters or event identity. This
module owns ONLY scheduling policy:

  - :class:`PriorityBand` — an ordered band with its allowed view types
  - :class:`ExtractionPolicy` — a versioned, data-driven contract: bands,
    fidelity threshold, freshness/novelty weighting, dedup/supersession,
    budgets and abstain/block reasons
  - :class:`PolicyCandidate` — a scheduled view carrying ``derived_from_view``
    lineage and stable ``evidence_event_refs`` (D-24)
  - :func:`schedule_candidates` — deterministic queue construction
  - :func:`policy_digest` / :func:`band_digest` — deterministic policy identity

Initial policy locks CompactionWindow first (dense navigation signals, never
self-authenticating truth), then native trace/episode (incl. turn), session,
topic, cross-session. Changing trace priority is a policy-only operation:
raw artifact/event/view identities never change, only queue ranks and the
policy digest (D-22). Compaction priority can never override missing evidence
refs or low fidelity (D-23/D-24).

No I/O, no network, no provider calls (D-31).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping

from personal_knowledge.core.conversation_events import (
    FidelityDimension,
    FidelityLevel,
    FidelityProfile,
)
from personal_knowledge.application.conversation.extraction_views import (
    ViewBuildResult,
    ViewType,
)

# Deterministic reference clock when the caller supplies none.
_DEFAULT_NOW = "2026-08-12T00:00:00Z"

_LEVEL_ORDER: dict[FidelityLevel, int] = {
    FidelityLevel.UNAVAILABLE: 0,
    FidelityLevel.UNKNOWN: 1,
    FidelityLevel.PARTIAL: 2,
    FidelityLevel.COMPLETE: 3,
}


class BlockReason(str, Enum):
    """Deterministic abstain/block reasons for unscheduled views (D-23/D-27)."""

    VIEW_TYPE_DISALLOWED = "block:view_type_disallowed"
    ABSTAIN_NO_EVIDENCE = "abstain:no_evidence"
    ABSTAIN_LOW_FIDELITY = "abstain:low_fidelity"
    EVIDENCE_SUPERSEDED = "block:evidence_superseded"
    BUDGET_EXCEEDED = "block:budget_exceeded"


@dataclass(frozen=True)
class PriorityBand:
    """One ordered band and the view types it may schedule."""

    order: int
    allowed_view_types: tuple[ViewType, ...]

    def __post_init__(self) -> None:
        if self.order < 1:
            raise ValueError("priority band order must be >= 1")
        for vt in self.allowed_view_types:
            if not isinstance(vt, ViewType):
                raise ValueError(f"invalid view type in band: {vt!r}")


@dataclass(frozen=True)
class FreshnessConfig:
    """Recency weighting: score decays after ``max_age_hours``."""

    max_age_hours: float = 24 * 7
    decay_half_life_hours: float = 24 * 7


@dataclass(frozen=True)
class NoveltyConfig:
    """Weighting for evidence not yet covered by an earlier candidate."""

    enabled: bool = True


@dataclass(frozen=True)
class DedupConfig:
    """Supersession: exact duplicate (view type + evidence set) is dropped."""

    enabled: bool = True


@dataclass(frozen=True)
class BudgetConfig:
    """Per-band scheduling budget."""

    max_candidates_per_band: int = 20


@dataclass(frozen=True)
class ExtractionPolicy:
    """A versioned, data-driven extraction scheduling contract (D-22)."""

    policy_id: str
    version: str
    priority_bands: tuple[PriorityBand, ...]
    fidelity_threshold: FidelityLevel = FidelityLevel.PARTIAL
    require_evidence: bool = True
    freshness: FreshnessConfig = FreshnessConfig()
    novelty: NoveltyConfig = NoveltyConfig()
    dedup: DedupConfig = DedupConfig()
    budget: BudgetConfig = BudgetConfig()

    def __post_init__(self) -> None:
        if not self.policy_id or not self.version:
            raise ValueError("policy requires an id and a version")
        orders = [b.order for b in self.priority_bands]
        if orders != sorted(orders):
            raise ValueError("priority bands must be strictly ordered")
        if self.fidelity_threshold not in FidelityLevel:
            raise ValueError(f"invalid fidelity threshold: {self.fidelity_threshold!r}")

    @property
    def digest(self) -> str:
        return policy_digest(self)

    def band_for(self, view_type: ViewType) -> PriorityBand | None:
        for band in self.priority_bands:
            if view_type in band.allowed_view_types:
                return band
        return None

    def rank_for(self, view_type: ViewType) -> int | None:
        band = self.band_for(view_type)
        return band.order if band else None


@dataclass(frozen=True)
class PolicyCandidate:
    """A scheduled view; never a truth claim (D-23/D-24)."""

    candidate_id: str
    derived_from_view: str
    view_type: ViewType
    evidence_event_refs: tuple[str, ...]
    fidelity: FidelityProfile
    freshness: float
    novelty: float
    rank: int
    policy_digest: str


@dataclass(frozen=True)
class BlockedView:
    """A view that abstained or was blocked, with the deterministic reason."""

    view_id: str
    view_type: ViewType
    reason: str


@dataclass(frozen=True)
class SchedulingOutput:
    """Deterministic queue plus abstain/block ledger for one policy."""

    policy_id: str
    policy_digest: str
    candidates: tuple[PolicyCandidate, ...]
    blocked: tuple[BlockedView, ...]
    digest: str


# ---------------------------------------------------------------- digest

def band_digest(bands: tuple[PriorityBand, ...]) -> str:
    payload = [
        (b.order, [vt.value for vt in b.allowed_view_types])
        for b in sorted(bands, key=lambda b: b.order)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def policy_digest(policy: ExtractionPolicy) -> str:
    """Deterministic identity of the whole scheduling contract."""
    payload = {
        "policy_id": policy.policy_id,
        "version": policy.version,
        "bands": band_digest(policy.priority_bands),
        "fidelity_threshold": policy.fidelity_threshold.value,
        "require_evidence": policy.require_evidence,
        "freshness": {
            "max_age_hours": policy.freshness.max_age_hours,
            "decay_half_life_hours": policy.freshness.decay_half_life_hours,
        },
        "novelty": {"enabled": policy.novelty.enabled},
        "dedup": {"enabled": policy.dedup.enabled},
        "budget": {"max_candidates_per_band": policy.budget.max_candidates_per_band},
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------- scoring

def _worst_level(profile: FidelityProfile) -> FidelityLevel:
    return min(
        (lvl for lvl in profile.levels),
        key=lambda lvl: _LEVEL_ORDER[lvl],
    )


def _fidelity_score(profile: FidelityProfile) -> int:
    """Deterministic numeric score: count of complete dimensions."""
    return sum(1 for lvl in profile.levels if lvl is FidelityLevel.COMPLETE)


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _freshness_score(
    occurred_at: str | None, now: datetime, cfg: FreshnessConfig
) -> float:
    if not occurred_at:
        return 0.0
    try:
        age_hours = (now - _parse_iso(occurred_at)).total_seconds() / 3600.0
    except ValueError:
        return 0.0
    if age_hours <= cfg.max_age_hours:
        return 1.0
    overflow = age_hours - cfg.max_age_hours
    half_life = cfg.decay_half_life_hours or 1.0
    return max(0.0, 0.5 ** (overflow / half_life))


def _novelty_ratio(refs: tuple[str, ...], seen_evidence: set[str]) -> float:
    """Fraction of evidence refs not yet covered by an earlier candidate."""
    ref_set = set(refs)
    if not ref_set:
        return 0.0
    return len(ref_set - seen_evidence) / len(ref_set)


# ------------------------------------------------------------ scheduling

def schedule_candidates(
    policy: ExtractionPolicy,
    view_result: ViewBuildResult,
    now: str | None = None,
    *,
    events: Mapping[str, str | None] | None = None,
) -> SchedulingOutput:
    """Build the deterministic candidate queue for one policy (D-22/D-24).

    ``now`` is the ISO reference clock; it only affects freshness metadata, so
    the policy digest and the scheduled view set never depend on it. ``events``
    maps event ids to their ``occurred_at`` (optional; freshness is 0 when
    absent).
    """
    now_dt = _parse_iso(now or _DEFAULT_NOW)
    digest_value = policy.digest
    occurred_at = dict(events or {})
    threshold = _LEVEL_ORDER[
        _worst_level(
            FidelityProfile.from_levels(
                {d: policy.fidelity_threshold for d in FidelityDimension}
            )
        )
    ]

    def _freshness_of(view) -> float:
        return _freshness_score(
            _latest_occurred_at(view, occurred_at), now_dt, policy.freshness
        )

    schedulable, blocked = _classify_views(policy, view_result, threshold)
    ordered = _order_schedulable(schedulable, _freshness_of)
    candidates, final_blocked = _finalize(
        policy, ordered, blocked, _freshness_of, digest_value
    )

    output = SchedulingOutput(
        policy_id=policy.policy_id,
        policy_digest=digest_value,
        candidates=tuple(candidates),
        blocked=tuple(sorted(final_blocked, key=lambda b: (b.view_id, b.reason))),
        digest="",
    )
    return SchedulingOutput(
        policy_id=policy.policy_id,
        policy_digest=digest_value,
        candidates=output.candidates,
        blocked=output.blocked,
        digest=_output_digest(output),
    )


def _classify_views(
    policy: ExtractionPolicy,
    view_result: ViewBuildResult,
    threshold: int,
) -> tuple[list[tuple[PriorityBand, object]], list[BlockedView]]:
    """Partition views into schedulable (band, view) or blocked with reasons."""
    schedulable: list[tuple[PriorityBand, object]] = []
    blocked: list[BlockedView] = []
    for view in view_result.views:
        band = policy.band_for(view.view_type)
        if band is None:
            blocked.append(
                BlockedView(view.view_id, view.view_type,
                            BlockReason.VIEW_TYPE_DISALLOWED.value)
            )
            continue
        if policy.require_evidence and not view.evidence_event_refs:
            blocked.append(
                BlockedView(view.view_id, view.view_type,
                            BlockReason.ABSTAIN_NO_EVIDENCE.value)
            )
            continue
        if _LEVEL_ORDER[_worst_level(view.fidelity)] < threshold:
            blocked.append(
                BlockedView(view.view_id, view.view_type,
                            BlockReason.ABSTAIN_LOW_FIDELITY.value)
            )
            continue
        schedulable.append((band, view))
    return schedulable, blocked


def _order_schedulable(
    schedulable: list[tuple[PriorityBand, object]],
    freshness_of,
) -> list[tuple[PriorityBand, object, float]]:
    """Deterministic order: band, fidelity, freshness, greedy novelty, id."""
    ordered: list[tuple[PriorityBand, object, float]] = []
    seen_for_novelty: set[str] = set()
    for band, view in sorted(
        schedulable,
        key=lambda bv: (
            bv[0].order,
            -_fidelity_score(bv[1].fidelity),
            -freshness_of(bv[1]),
            bv[1].view_id,
        ),
    ):
        greedy_novelty = _novelty_ratio(view.evidence_event_refs, seen_for_novelty)
        ordered.append((band, view, greedy_novelty))
        seen_for_novelty.update(view.evidence_event_refs)
    return ordered


def _finalize(
    policy: ExtractionPolicy,
    ordered: list[tuple[PriorityBand, object, float]],
    blocked: list[BlockedView],
    freshness_of,
    digest_value: str,
) -> tuple[list[PolicyCandidate], list[BlockedView]]:
    """Apply supersession, budget, ranks and novelty metadata."""
    candidates: list[PolicyCandidate] = []
    final_blocked: list[BlockedView] = list(blocked)
    seen_sets: set[tuple[ViewType, frozenset[str]]] = set()
    seen_evidence: set[str] = set()
    band_counts: dict[int, int] = {}

    for band, view, _greedy_novelty in sorted(
        ordered,
        key=lambda bv: (
            bv[0].order,
            -_fidelity_score(bv[1].fidelity),
            -freshness_of(bv[1]),
            -bv[2],
            bv[1].view_id,
        ),
    ):
        if policy.dedup.enabled:
            key = (view.view_type, frozenset(view.evidence_event_refs))
            if key in seen_sets:
                final_blocked.append(
                    BlockedView(view.view_id, view.view_type,
                                BlockReason.EVIDENCE_SUPERSEDED.value)
                )
                continue
            seen_sets.add(key)
        if band_counts.get(band.order, 0) >= policy.budget.max_candidates_per_band:
            final_blocked.append(
                BlockedView(view.view_id, view.view_type,
                            BlockReason.BUDGET_EXCEEDED.value)
            )
            continue
        novelty = _novelty_ratio(view.evidence_event_refs, seen_evidence)
        seen_evidence.update(view.evidence_event_refs)
        band_counts[band.order] = band_counts.get(band.order, 0) + 1
        candidates.append(
            PolicyCandidate(
                candidate_id=_candidate_id(digest_value, view.view_id),
                derived_from_view=view.view_id,
                view_type=view.view_type,
                evidence_event_refs=view.evidence_event_refs,
                fidelity=view.fidelity,
                freshness=freshness_of(view),
                novelty=novelty,
                rank=len(candidates) + 1,
                policy_digest=digest_value,
            )
        )
    return candidates, final_blocked


def _latest_occurred_at(view, index: dict[str, str | None]) -> str | None:
    candidates = [index[eid] for eid in view.evidence_event_refs if eid in index]
    candidates = [c for c in candidates if c is not None]
    return max(candidates) if candidates else None


def _candidate_id(policy_digest_value: str, derived_from_view: str) -> str:
    payload = "|".join([policy_digest_value, derived_from_view])
    return "candidate:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _output_digest(output: SchedulingOutput) -> str:
    payload = {
        "policy_digest": output.policy_digest,
        "candidates": [
            (
                c.derived_from_view,
                c.rank,
                c.view_type.value,
                sorted(c.evidence_event_refs),
            )
            for c in output.candidates
        ],
        "blocked": sorted((b.view_id, b.reason) for b in output.blocked),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------- initial policy

DEFAULT_POLICY = ExtractionPolicy(
    policy_id="policy-initial-1",
    version="1",
    priority_bands=(
        # band 1: dense navigation signals, never self-authenticating truth
        PriorityBand(order=1, allowed_view_types=(ViewType.COMPACTION_WINDOW,)),
        # band 2: source-native bounded execution episodes (trace/turn/episode)
        PriorityBand(
            order=2,
            allowed_view_types=(
                ViewType.TURN,
                ViewType.NATIVE_TRACE,
                ViewType.EPISODE,
            ),
        ),
        PriorityBand(order=3, allowed_view_types=(ViewType.SESSION,)),
        PriorityBand(order=4, allowed_view_types=(ViewType.TOPIC,)),
        PriorityBand(order=5, allowed_view_types=(ViewType.CROSS_SESSION,)),
    ),
)


__all__ = [
    "BlockReason",
    "BlockedView",
    "BudgetConfig",
    "DEFAULT_POLICY",
    "DedupConfig",
    "ExtractionPolicy",
    "FreshnessConfig",
    "NoveltyConfig",
    "PolicyCandidate",
    "PriorityBand",
    "SchedulingOutput",
    "band_digest",
    "policy_digest",
    "schedule_candidates",
]
